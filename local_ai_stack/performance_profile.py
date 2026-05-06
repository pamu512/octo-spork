"""Ollama model benchmarking → ``performance_profile.json`` + background PR review model selection."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

PROFILE_FILENAME = "performance_profile.json"
_DEFAULT_PROMPT_TARGET_TOKENS = 100
_DEFAULT_COMPLETION_TOKENS = 256


def _parse_env_file_local(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        env[key] = value
    return env

# Higher = better: favors throughput, penalizes slow TTFT and high VRAM (stability / headroom).
_STABILITY_SCORE_DOC = (
    "stability_score = (tokens_per_second + 1e-6) / "
    "((time_to_first_token_sec + 0.2) * (peak_vram_mib_or_neutral + 512))"
)


def _repo_root() -> Path:
    """Directory containing the ``local_ai_stack`` package (octo-spork repo root)."""
    return Path(__file__).resolve().parents[1]


def default_tooling_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _repo_root()


def profile_json_path(tooling_root: Path | None = None) -> Path:
    override = (os.environ.get("OCTO_PERF_PROFILE_PATH") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = tooling_root or default_tooling_root()
    return (root / PROFILE_FILENAME).resolve()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_standard_prompt(*, target_tokens: int = _DEFAULT_PROMPT_TARGET_TOKENS) -> str:
    """Repeat filler until the rough token estimate reaches *target_tokens* (chars÷4)."""
    from claude_bridge.token_governor import estimate_tokens_python

    seed = (
        "Octo-spork standardized latency probe. Reply with a single short acknowledgement sentence. "
    )
    text = seed
    while estimate_tokens_python(text) < target_tokens:
        text += "tok "
    return text.strip()


def _sample_vram_peak(stop: threading.Event, out: dict[str, float | None]) -> None:
    try:
        from observability.performance_tracker import sample_vram_nvidia
    except ImportError:
        return
    while not stop.wait(0.08):
        s = sample_vram_nvidia()
        u = s.get("used_mib")
        if u is None:
            continue
        cur = out.get("peak")
        if cur is None or float(u) > float(cur):
            out["peak"] = float(u)


def _normalize_name(tag: str) -> str:
    return (tag or "").strip()


def model_available_locally(model_name: str, local_tags: list[str]) -> bool:
    """Match Ollama tag against ``/api/tags`` names (exact or same base name)."""
    want = _normalize_name(model_name)
    if not want:
        return False
    local = [_normalize_name(x) for x in local_tags if x]
    if want in local:
        return True
    base = want.split(":")[0].lower()
    for t in local:
        if t.split(":")[0].lower() == base:
            return True
    return False


def compute_stability_score(row: dict[str, Any]) -> float | None:
    """Return a scalar score for ranking (higher = more stable for unattended runs)."""
    if not row.get("success"):
        return None
    try:
        ttft = float(row.get("time_to_first_token_sec") or 0.0)
        tps = float(row.get("tokens_per_second") or 0.0)
    except (TypeError, ValueError):
        return None
    raw_peak = row.get("peak_vram_mib")
    if raw_peak is None:
        neutral = float(os.environ.get("OCTO_PERF_STABILITY_VRAM_NEUTRAL_MIB", "4096") or "4096")
        peak = max(256.0, neutral)
    else:
        peak = max(256.0, float(raw_peak))
    return (tps + 1e-6) / ((ttft + 0.2) * (peak + 512.0))


def pick_most_stable_model(rows: list[dict[str, Any]]) -> tuple[str | None, float | None]:
    best_name: str | None = None
    best_score: float | None = None
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        sc = compute_stability_score(row)
        if sc is None:
            continue
        if best_score is None or sc > best_score:
            best_score = sc
            best_name = name.strip()
    return best_name, best_score


def bench_one_model(
    ollama_base_url: str,
    model: str,
    prompt: str,
    *,
    completion_tokens: int = _DEFAULT_COMPLETION_TOKENS,
    timeout_sec: float = 420.0,
) -> dict[str, Any]:
    """Stream ``/api/generate``; measure TTFT, TPS (from eval counters), peak VRAM."""
    import httpx

    base = ollama_base_url.rstrip("/")
    url = f"{base}/api/generate"
    stop_sampler = threading.Event()
    vram_peak: dict[str, float | None] = {"peak": None}
    sampler = threading.Thread(
        target=_sample_vram_peak,
        args=(stop_sampler, vram_peak),
        name="vram-peak-sampler",
        daemon=True,
    )
    sampler.start()

    t0 = time.perf_counter()
    ttft_s: float | None = None
    eval_count = 0
    eval_duration_ns = 0
    err: str | None = None

    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"num_predict": int(completion_tokens), "temperature": 0.1},
    }

    try:
        with httpx.Client(timeout=timeout_sec) as client:
            with client.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    if not chunk:
                        continue
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj: dict[str, Any] = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        piece = obj.get("response")
                        if ttft_s is None and isinstance(piece, str) and piece.strip():
                            ttft_s = time.perf_counter() - t0
                        if obj.get("done"):
                            try:
                                eval_count = int(obj.get("eval_count") or 0)
                            except (TypeError, ValueError):
                                eval_count = 0
                            try:
                                eval_duration_ns = int(obj.get("eval_duration") or 0)
                            except (TypeError, ValueError):
                                eval_duration_ns = 0
                # trailing
                if buf.strip():
                    try:
                        obj = json.loads(buf.strip())
                        if ttft_s is None and isinstance(obj.get("response"), str) and str(obj.get("response")).strip():
                            ttft_s = time.perf_counter() - t0
                        if obj.get("done"):
                            eval_count = int(obj.get("eval_count") or 0)
                            eval_duration_ns = int(obj.get("eval_duration") or 0)
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        _LOG.warning("benchmark %s: %s", model, err)
    finally:
        stop_sampler.set()
        sampler.join(timeout=3.0)

    tps = 0.0
    if eval_count > 0 and eval_duration_ns > 0:
        tps = eval_count / (eval_duration_ns / 1e9)
    elif eval_count > 0 and ttft_s is not None:
        wall = max(1e-6, time.perf_counter() - t0 - ttft_s)
        tps = eval_count / wall

    ok = err is None and ttft_s is not None
    peak_v = vram_peak.get("peak")

    row: dict[str, Any] = {
        "name": model,
        "time_to_first_token_sec": float(ttft_s) if ttft_s is not None else None,
        "tokens_per_second": round(tps, 4),
        "peak_vram_mib": round(float(peak_v), 2) if peak_v is not None else None,
        "eval_count": int(eval_count),
        "eval_duration_ns": int(eval_duration_ns),
        "success": ok,
        "error": err,
    }
    sc = compute_stability_score(row)
    row["stability_score"] = round(sc, 8) if sc is not None else None
    return row


def run_benchmark_suite(
    ollama_base_url: str,
    *,
    tooling_root: Path | None = None,
    prompt_target_tokens: int = _DEFAULT_PROMPT_TARGET_TOKENS,
    completion_tokens: int = _DEFAULT_COMPLETION_TOKENS,
    models_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Benchmark every locally listed Ollama model (or *models_filter* if set)."""
    from local_ai_stack.model_fallback import list_local_ollama_model_names

    base = ollama_base_url.rstrip("/")
    names = list_local_ollama_model_names(base)
    if models_filter:
        want = {_normalize_name(m) for m in models_filter if m.strip()}
        names = [n for n in names if _normalize_name(n) in want]
    prompt = build_standard_prompt(target_tokens=prompt_target_tokens)
    from claude_bridge.token_governor import estimate_tokens_python

    approx_prompt_tokens = estimate_tokens_python(prompt)

    rows: list[dict[str, Any]] = []
    for name in sorted(names, key=lambda x: x.lower()):
        _LOG.info("benchmark-models: running %s", name)
        rows.append(
            bench_one_model(
                base,
                name,
                prompt,
                completion_tokens=completion_tokens,
            )
        )

    winner, win_score = pick_most_stable_model(rows)
    out: dict[str, Any] = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ollama_base_url": base,
        "tooling_root": str((tooling_root or default_tooling_root()).resolve()),
        "prompt_target_tokens": int(prompt_target_tokens),
        "approx_prompt_tokens": int(approx_prompt_tokens),
        "completion_tokens_cap": int(completion_tokens),
        "selection_formula": _STABILITY_SCORE_DOC,
        "models": rows,
        "most_stable_model": winner,
        "most_stable_score": win_score,
    }
    return out


def save_performance_profile(data: dict[str, Any], tooling_root: Path | None = None) -> Path:
    path = profile_json_path(tooling_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    _LOG.info("wrote %s", path)
    return path


def load_performance_profile(tooling_root: Path | None = None) -> dict[str, Any] | None:
    path = profile_json_path(tooling_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("could not load performance profile %s: %s", path, exc)
        return None


def resolve_background_review_model(
    *,
    tooling_root: Path | None = None,
    ollama_base_url: str | None = None,
) -> str:
    """Model for unattended PR reviews: explicit env → profile winner → ``OLLAMA_MODEL`` fallback.

    On macOS, :class:`infra.vram_pressure_monitor.VRAMPressureMonitor` may downgrade 14B/32B-class
    picks to ``qwen2.5-coder:7b`` (override via ``OCTO_UNIFIED_MEMORY_PRESSURE_MODEL``) when
    ``system_profiler SPDisplaysDataType`` reports **Unified Memory** pressure **High**.
    """
    base = (
        (ollama_base_url or "").strip()
        or (os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
        or (os.environ.get("OLLAMA_BASE_URL") or "").strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")

    try:
        from local_ai_stack.model_fallback import list_local_ollama_model_names

        local = list_local_ollama_model_names(base, timeout=8.0)
    except Exception:
        local = []

    explicit = (os.environ.get("OCTO_BACKGROUND_REVIEW_MODEL") or "").strip()
    default_model = (os.environ.get("OLLAMA_MODEL") or "qwen2.5:14b").strip() or "qwen2.5:14b"

    if explicit:
        candidate = explicit
    elif _truthy("OCTO_PERF_PROFILE_DISABLE"):
        candidate = default_model
    else:
        data = load_performance_profile(tooling_root)
        if not isinstance(data, dict):
            candidate = default_model
        else:
            winner = data.get("most_stable_model")
            if not isinstance(winner, str) or not winner.strip():
                candidate = default_model
            elif model_available_locally(winner.strip(), local):
                candidate = winner.strip()
            else:
                _LOG.warning(
                    "performance profile most_stable_model %r not in local Ollama tags; using OLLAMA_MODEL",
                    winner,
                )
                candidate = default_model

    try:
        from infra.vram_pressure_monitor import apply_unified_memory_pressure_override

        out, _reason = apply_unified_memory_pressure_override(candidate, local)
        return out
    except ImportError:
        return candidate


def configure_benchmark_models_args(p: Any) -> None:
    """Attach ``benchmark-models`` CLI flags to *p* (ArgumentParser or subparser)."""
    p.add_argument(
        "--env-file",
        default=None,
        help="Optional .env.local for OLLAMA_LOCAL_URL (default: deploy/local-ai/.env.local)",
    )
    p.add_argument(
        "--ollama-url",
        default=None,
        help="Override Ollama base URL",
    )
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help=f"JSON output path (default: {PROFILE_FILENAME} under repo root)",
    )
    p.add_argument(
        "--prompt-tokens",
        type=int,
        default=_DEFAULT_PROMPT_TARGET_TOKENS,
        metavar="N",
        help=f"Target approximate prompt size in tokens (default {_DEFAULT_PROMPT_TARGET_TOKENS})",
    )
    p.add_argument(
        "--completion-tokens",
        type=int,
        default=_DEFAULT_COMPLETION_TOKENS,
        metavar="N",
        help=f"Max completion tokens per model (default {_DEFAULT_COMPLETION_TOKENS})",
    )
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model tags to benchmark (default: all local tags)",
    )


def run_benchmark_models_with_namespace(args: Any) -> int:
    """Execute benchmark from an argparse Namespace (from standalone or nested CLI)."""
    repo = _repo_root()
    env_file = Path(args.env_file).expanduser().resolve() if getattr(args, "env_file", None) else repo / "deploy" / "local-ai" / ".env.local"
    ollama_url = (getattr(args, "ollama_url", None) or "").strip()
    if not ollama_url and env_file.is_file():
        ev = _parse_env_file_local(env_file)
        ollama_url = (ev.get("OLLAMA_LOCAL_URL") or ev.get("OLLAMA_BASE_URL") or "").strip()
    if not ollama_url:
        ollama_url = (
            (os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "").strip()
            or "http://127.0.0.1:11434"
        )

    filt = None
    raw_models = getattr(args, "models", None)
    if raw_models:
        filt = [x.strip() for x in str(raw_models).split(",") if x.strip()]

    src = repo / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

    data = run_benchmark_suite(
        ollama_url,
        tooling_root=repo,
        prompt_target_tokens=int(getattr(args, "prompt_tokens", _DEFAULT_PROMPT_TARGET_TOKENS)),
        completion_tokens=int(getattr(args, "completion_tokens", _DEFAULT_COMPLETION_TOKENS)),
        models_filter=filt,
    )
    out_raw = getattr(args, "output", None)
    out_path = Path(out_raw).expanduser().resolve() if out_raw else profile_json_path(repo)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"wrote": str(out_path), "most_stable_model": data.get("most_stable_model")}, indent=2))
    if not data.get("models"):
        print("No Ollama models reported by /api/tags.", file=sys.stderr)
        return 1
    return 0


def run_benchmark_models_main(argv: list[str] | None = None) -> int:
    """CLI entry for ``python -m local_ai_stack benchmark-models`` (standalone parser)."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run a standardized ~100-token prompt through each locally installed Ollama model; "
            f"write {PROFILE_FILENAME} with TTFT, TPS, peak VRAM, and most-stable selection."
        ),
    )
    configure_benchmark_models_args(parser)
    args = parser.parse_args(argv)
    return run_benchmark_models_with_namespace(args)
