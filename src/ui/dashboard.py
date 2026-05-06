"""Fopoon live dashboard — CPU/GPU metrics, Docker status, tracer \"thought\" tail, graceful agent kill.

Run::

    PYTHONPATH=src python -m ui.dashboard

Prerequisites:

- Set ``OCTO_TUI_TRACE_LOG`` (optional; defaults to ``.local/octo_fopoon_trace.jsonl``) **before** starting
  instrumented agents so :mod:`observability.tracer` can append thought lines.
- ``Kill`` writes ``OCTO_AGENT_STOP_FLAG`` (default ``.local/octo_agent_stop_request``); cooperating loops
  (e.g. grounded review before each Ollama call) exit without flushing abusive Redis writes — normal Redis
  clients keep working; session TTL logic is unchanged.

Requires ``textual``, ``psutil``, and optionally ``pynvml`` (``nvidia-ml-py``) for NVIDIA GPUs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_SRC = Path(__file__).resolve().parents[1]


def ensure_src_on_path() -> None:
    if str(_REPO_SRC) not in sys.path:
        sys.path.insert(0, str(_REPO_SRC))


def _format_trace_line(obj: dict[str, Any]) -> str:
    ts = obj.get("ts", "")
    kind = obj.get("kind", "?")
    thought = (obj.get("thought") or "").replace("\n", " ").strip()
    err = obj.get("error")
    if kind == "llm":
        m = obj.get("model", "")
        pt = obj.get("prompt_tokens")
        ct = obj.get("completion_tokens")
        ms = obj.get("latency_ms")
        bits = [f"[LLM] {m}", f"{ms}ms"]
        if pt is not None:
            bits.append(f"pt={pt}")
        if ct is not None:
            bits.append(f"ct={ct}")
        if thought:
            bits.append(thought[:240])
        if err:
            bits.append(f"ERR:{err[:120]}")
        return " | ".join(bits)
    if kind == "tool":
        tool = obj.get("tool", "")
        ms = obj.get("latency_ms")
        bits = [f"[tool:{tool}]", f"{ms}ms"]
        if thought:
            bits.append(thought[:200])
        if err:
            bits.append(f"ERR:{err[:120]}")
        return " | ".join(bits)
    if kind == "control":
        return f"[control] {thought}"
    if kind == "dashboard":
        return f"[dash] {thought}"
    return json.dumps(obj, ensure_ascii=False, default=str)[:400]


_NVML_INITED = False


def _gpu_snapshot() -> tuple[str, bool]:
    global _NVML_INITED
    try:
        import pynvml

        if not _NVML_INITED:
            pynvml.nvmlInit()
            _NVML_INITED = True
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        pct = util.gpu
        mem_pct = round(100.0 * mem.used / mem.total, 1) if mem.total else 0.0
        line = (
            f"GPU {name[:40]} | util {pct}% | VRAM {mem.used // (1024**2)} / "
            f"{mem.total // (1024**2)} MiB ({mem_pct}%)"
        )
        return line, True
    except Exception as exc:
        return f"GPU: unavailable ({exc.__class__.__name__})", False


def _nvml_shutdown_safe() -> None:
    global _NVML_INITED
    if not _NVML_INITED:
        return
    try:
        import pynvml

        pynvml.nvmlShutdown()
    except Exception:
        pass
    finally:
        _NVML_INITED = False


def _cpu_ram_snapshot() -> str:
    import psutil

    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    return f"CPU {cpu:.1f}% | RAM {vm.percent:.1f}% used ({vm.used // (1024**3)} / {vm.total // (1024**3)} GiB)"


def _docker_ps_lines() -> list[str]:
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.Image}}",
            ],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return [f"docker ps failed: {err[:200]}"]
        out = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line:
                out.append(line)
        return out or ["(no running containers)"]
    except FileNotFoundError:
        return ["docker: not found on PATH"]
    except subprocess.TimeoutExpired:
        return ["docker ps: timeout"]


def build_app_class():
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.widgets import Button, Footer, Header, RichLog, Static

    class FopoonDashboard(App[None]):
        """Real-time fopoon: resources, thought log, docker, kill."""

        CSS = """
        Screen { background: $surface; }
        #title { text-align: center; text-style: bold; padding: 0 1; }
        #resources { height: 3; border: round $primary; padding: 0 1; background: $panel; }
        #main_row { height: 1fr; min-height: 10; }
        #thought { width: 2fr; min-width: 20; border: round $accent; }
        #docker_col { width: 1fr; min-width: 24; border: round $primary; }
        #docker_body { padding: 0 1 1 1; }
        #actions { dock: bottom; height: auto; padding: 1 2; background: $panel; }
        RichLog { height: 1fr; padding: 0 1; }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("k", "kill_action", "Kill"),
        ]

        def __init__(self, **kwargs: Any) -> None:
            ensure_src_on_path()
            super().__init__(**kwargs)
            self._tail_state: dict[str, Any] = {"pos": 0}

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("Fopoon — turntable polish cadence (live)", id="title")
            yield Static("", id="resources")
            with Horizontal(id="main_row"):
                with Vertical(id="thought"):
                    yield Static("[thought trace]", classes="title")
                    yield RichLog(highlight=True, markup=True, max_lines=4000, id="thought_log")
                with VerticalScroll(id="docker_col"):
                    yield Static("[docker]", id="docker_title")
                    yield Static("", id="docker_body")
            with Horizontal(id="actions"):
                yield Button("Kill agent loop (graceful)", id="btn_kill", variant="error")
                yield Button("Clear stop flag", id="btn_clear", variant="primary")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Fopoon"
            self.set_interval(0.9, self.refresh_resources)
            self.set_interval(0.4, self.refresh_thought_tail)
            self.set_interval(2.0, self.refresh_docker)
            self.set_timer(0.05, self.refresh_resources)
            self.set_timer(0.06, self.refresh_docker)
            self._prime_cpu()

        def _prime_cpu(self) -> None:
            import psutil

            psutil.cpu_percent(interval=0.15)

        def refresh_resources(self) -> None:
            res = self.query_one("#resources", Static)
            try:
                cpu_line = _cpu_ram_snapshot()
            except Exception as exc:
                cpu_line = f"metrics error: {exc}"
            gpu_line, _ = _gpu_snapshot()
            log_hint = os.environ.get("OCTO_TUI_TRACE_LOG") or "(default .local/octo_fopoon_trace.jsonl)"
            stop_hint = os.environ.get("OCTO_AGENT_STOP_FLAG") or "(default .local/octo_agent_stop_request)"
            flag_on = False
            try:
                from observability.tui_bridge import agent_stop_requested

                flag_on = agent_stop_requested()
            except ImportError:
                pass
            flag_s = "STOP-PENDING" if flag_on else "running"
            res.update(
                f"{cpu_line}\n{gpu_line}\n"
                f"trace log: {log_hint} | stop flag: {stop_hint} | agent: {flag_s}"
            )

        def refresh_thought_tail(self) -> None:
            try:
                from observability.tui_bridge import trace_log_path

                path = trace_log_path()
            except ImportError:
                return
            log_w = self.query_one("#thought_log", RichLog)
            try:
                if not path.is_file():
                    self._tail_state["pos"] = 0
                    return
                size = path.stat().st_size
                pos = int(self._tail_state.get("pos", 0))
                if pos > size:
                    pos = 0
                    self._tail_state["pos"] = 0
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    self._tail_state["pos"] = fh.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        log_w.write(_format_trace_line(obj))
                    except json.JSONDecodeError:
                        log_w.write(line[:500])
            except OSError:
                pass

        def refresh_docker(self) -> None:
            body = self.query_one("#docker_body", Static)
            lines = _docker_ps_lines()
            body.update("\n".join(lines[:40]))

        def action_kill_action(self) -> None:
            self._do_kill()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn_kill":
                self._do_kill()
            elif event.button.id == "btn_clear":
                self._do_clear()

        def _do_kill(self) -> None:
            try:
                from observability.tui_bridge import request_agent_stop

                path = request_agent_stop(reason="fopoon_dashboard")
                self.notify(f"Graceful stop requested → {path}", severity="warning", timeout=8)
            except ImportError:
                self.notify("observability.tui_bridge not available", severity="error")

        def _do_clear(self) -> None:
            try:
                from observability.tui_bridge import clear_agent_stop

                clear_agent_stop()
                self.notify("Stop flag cleared.", severity="information")
            except ImportError:
                self.notify("observability.tui_bridge not available", severity="error")

    return FopoonDashboard


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    ensure_src_on_path()
    cls = build_app_class()
    app = cls()
    try:
        app.run()
    finally:
        _nvml_shutdown_safe()


if __name__ == "__main__":
    main()
