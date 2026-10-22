"""Stack orchestration for ``local_ai_stack up``.

Pure move from ``__main__`` (after #10 env_validation + #11 port_plan): the
``StackOrchestrator`` class body is byte-identical to its previous definition
in ``__main__``, which re-exports it for backward compatibility.

This module does not import ``local_ai_stack.__main__`` (even lazily). Helpers
that still live in the CLI module are bound onto this module by ``__main__``
after they are defined.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from local_ai_stack.env_validation import (
    _ensure_env_file,
    _parse_env_file,
)
from local_ai_stack.port_plan import (
    _compute_port_fix_updates,
    _find_alternative_host_port,
    _http_get_status,
    _ollama_tags_probe_url_from_env,
    _rewrite_env_file_string_values,
    _stack_port_plan,
    _tcp_port_is_in_use,
)

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_CODE_DIR = ROOT / "deploy" / "claude-code"


def _print(message: str) -> None:
    print(message, flush=True)


class StackOrchestrator:
    """Staged rollout for `local_ai_stack up`: Docker, ports, bootstrap, compose, DeepProbe readiness."""

    def __init__(
        self,
        root: Path,
        env_file: Path,
        logger: logging.Logger,
        *,
        rewrite_conflicting_ports: bool = True,
    ) -> None:
        if root is None:
            raise ValueError("root is required")
        if env_file is None:
            raise ValueError("env_file is required")
        self._root = Path(root).resolve()
        self._env_file = Path(env_file).resolve()
        self._logger = logger
        self._rewrite_conflicting_ports = bool(rewrite_conflicting_ports)

    def _verify_docker_daemon(self) -> None:
        self._logger.info("Stage 1: verifying Docker daemon is active")
        try:
            completed = subprocess.run(
                ["docker", "info"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            self._logger.exception("Docker CLI not found on PATH")
            raise RuntimeError(
                "Docker CLI was not found. Install Docker Desktop or the Docker Engine "
                "and ensure `docker` is on your PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            self._logger.exception("Docker daemon did not respond to `docker info` in time")
            raise RuntimeError(
                "Docker daemon did not respond in time. Start Docker and retry."
            ) from exc
        except OSError as exc:
            self._logger.exception("OS error while invoking docker info")
            raise RuntimeError(f"Could not execute Docker: {exc}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            self._logger.error(
                "Docker daemon check failed rc=%s stderr=%s stdout=%s",
                completed.returncode,
                stderr,
                stdout,
            )
            raise RuntimeError(
                "Docker daemon is not running or `docker info` failed. "
                "Start Docker Desktop (or the Docker service) and retry."
            )

    def _ollama_tags_probe_url(self, env_values: dict[str, str]) -> str:
        return _ollama_tags_probe_url_from_env(env_values)

    def _verify_stack_ports_raise_on_conflict(self, env_values: dict[str, str]) -> None:
        self._logger.info("Stage 2: checking host ports for compose services")
        bind_host = "127.0.0.1"
        probe_url = _ollama_tags_probe_url_from_env(env_values)
        lines: list[str] = []
        try:
            entries = _stack_port_plan(env_values)
        except (TypeError, ValueError) as exc:
            self._logger.exception("Invalid port configuration in env")
            raise RuntimeError(f"Invalid port configuration: {exc}") from exc
        for label, port in entries:
            try:
                in_use = _tcp_port_is_in_use(bind_host, port)
            except (OSError, ValueError) as exc:
                self._logger.exception("TCP check failed for %s:%s", label, port)
                raise RuntimeError(f"Could not check whether port {port} ({label}) is in use.") from exc
            if not in_use:
                continue
            if label == "OLLAMA_LOCAL_URL port":
                try:
                    status = _http_get_status(probe_url, timeout=5.0)
                except ValueError as exc:
                    self._logger.exception("Invalid probe URL for Ollama")
                    raise RuntimeError(f"Invalid Ollama probe URL built from env: {exc}") from exc
                if status == 200:
                    self._logger.info(
                        "Port %s is in use but responds with HTTP %s for Ollama probe; continuing",
                        port,
                        status,
                    )
                    continue
                try:
                    alt = _find_alternative_host_port(bind_host, port + 1)
                except RuntimeError as exc:
                    self._logger.exception("Could not find alternative Ollama port")
                    raise RuntimeError(
                        "Ollama port is occupied by a non-Ollama service and no alternative "
                        "port was found. Free the port or adjust OLLAMA_LOCAL_URL / OLLAMA_HOST."
                    ) from exc
                lines.append(
                    f"- Port {port} (Ollama): in use and Ollama probe returned HTTP {status} "
                    f"instead of 200. Consider stopping the conflicting process or using a free "
                    f"port such as {alt} (update OLLAMA_LOCAL_URL and OLLAMA_HOST accordingly)."
                )
                self._logger.error(
                    "Ollama port conflict: probe %s returned status=%s",
                    probe_url,
                    status,
                )
                continue
            try:
                alt = _find_alternative_host_port(bind_host, port + 1)
            except RuntimeError as exc:
                self._logger.exception("Could not find alternative port for %s", label)
                raise RuntimeError(
                    f"Port {port} ({label}) is in use and no alternative port was found nearby."
                ) from exc
            lines.append(
                f"- Port {port} ({label}) is already bound on {bind_host}. "
                f"Set {label}={alt} in {self._env_file} (or another free port) and retry."
            )
            self._logger.error("Port conflict: %s=%s is in use; suggested alternative %s", label, port, alt)
        if lines:
            msg = "Host port conflicts detected:\n" + "\n".join(lines)
            self._logger.error(msg)
            raise RuntimeError(msg)

    def _rewrite_stack_ports_with_auto_fix(self, env_preview: dict[str, str]) -> dict[str, str]:
        current: dict[str, str] = dict(env_preview)
        max_outer_rounds = 24
        for round_idx in range(max_outer_rounds):
            try:
                updates = _compute_port_fix_updates(current)
            except RuntimeError:
                raise
            except Exception as exc:
                self._logger.exception("Unexpected error while computing port fix updates")
                raise RuntimeError("Automatic port conflict resolution failed unexpectedly.") from exc
            if not updates:
                try:
                    self._verify_stack_ports_raise_on_conflict(current)
                except RuntimeError:
                    raise
                return current
            try:
                _rewrite_env_file_string_values(self._env_file, updates)
            except RuntimeError:
                raise
            except OSError as exc:
                self._logger.exception("Failed to rewrite env file with adjusted ports")
                raise RuntimeError(f"Could not rewrite env file: {self._env_file}") from exc
            summary = ", ".join(f"{key}={value}" for key, value in sorted(updates.items()))
            self._logger.warning(
                "Rewrote %s to resolve host port conflicts (round %s): %s",
                self._env_file,
                round_idx + 1,
                summary,
            )
            _print(f"Resolved host port conflicts by updating {self._env_file}: {summary}")
            try:
                current = _parse_env_file(self._env_file)
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.exception("Could not re-parse env file after automatic port rewrite")
                raise RuntimeError(
                    f"Could not re-read env file after port rewrite: {self._env_file}"
                ) from exc
        try:
            self._verify_stack_ports_raise_on_conflict(current)
        except RuntimeError:
            raise
        return current

    def _run_compose_up(
        self,
        env_file: Path,
        agenticseek_path: Path,
        process_env: dict[str, str],
    ) -> None:
        self._logger.info("Stage 4: starting AgenticSeek docker compose stack")
        try:
            from local_ai_stack.resource_hardener import ensure_compose_resource_override

            ensure_compose_resource_override(self._root, logger=self._logger)
        except Exception as exc:
            self._logger.warning("ResourceHardener could not refresh compose override: %s", exc)
        compose_cmd = _compose_base_args(env_file, agenticseek_path) + [
            "--profile",
            "full",
            "--profile",
            "addons",
            "up",
            "-d",
            "--build",
        ]
        try:
            _run(compose_cmd, env=process_env)
        except subprocess.CalledProcessError as exc:
            self._logger.exception(
                "docker compose up failed with exit code %s",
                getattr(exc, "returncode", "unknown"),
            )
            raise

    def execute(self) -> None:
        try:
            _ensure_env_file(self._env_file)
        except OSError as exc:
            self._logger.exception("Could not ensure env file exists")
            raise RuntimeError(f"Could not prepare env file at {self._env_file}") from exc
        try:
            env_preview = _parse_env_file(self._env_file)
        except (OSError, UnicodeDecodeError) as exc:
            self._logger.exception("Could not read env file")
            raise RuntimeError(f"Could not read env file: {self._env_file}") from exc
        try:
            self._verify_docker_daemon()
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during Docker daemon verification")
            raise RuntimeError("Docker verification failed due to an unexpected error.") from exc
        try:
            from local_ai_stack.port_sentinel import run_port_sentinel

            self._logger.info("Stage 1.5: PortSentinel (TCP 11434, 5432, 6379, 8080)")
            env_preview = run_port_sentinel(
                self._root,
                self._env_file,
                env_preview,
                logger=self._logger,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during PortSentinel")
            raise RuntimeError("Port sentinel failed due to an unexpected error.") from exc
        try:
            if self._rewrite_conflicting_ports:
                env_preview = self._rewrite_stack_ports_with_auto_fix(env_preview)
            else:
                self._verify_stack_ports_raise_on_conflict(env_preview)
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during port conflict checks")
            raise RuntimeError("Port conflict checking failed due to an unexpected error.") from exc
        try:
            env_values, agenticseek_path = bootstrap(self._env_file)
        except Exception as exc:
            self._logger.exception("Bootstrap failed")
            raise
        process_env = _merge_process_env(env_values)
        try:
            _start_ollama_if_needed(env_values, process_env)
        except Exception as exc:
            self._logger.exception("Starting Ollama on the host failed")
            raise
        try:
            from local_ai_stack.model_fallback import run_ollama_pull_with_model_fallback

            env_values = run_ollama_pull_with_model_fallback(
                self._root,
                self._env_file,
                env_values,
                process_env,
                self._logger,
                agenticseek_path,
            )
            process_env = _merge_process_env(env_values)
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("ollama pull / ModelFallback failed")
            raise RuntimeError("Model pull or fallback handling failed unexpectedly.") from exc
        if _claude_agent_stack_available():
            self._logger.info("Including Claude Agent service (deploy/claude-code present)")
            _print(
                "Claude Agent: building and starting ``claude-agent`` "
                f"(image from {CLAUDE_CODE_DIR.relative_to(ROOT)})…"
            )
        process_env["OCTO_SPORK_ROOT"] = str(self._root)
        try:
            self._run_compose_up(self._env_file, agenticseek_path, process_env)
        except subprocess.CalledProcessError:
            raise
        try:
            from local_ai_stack.deep_probe import run_deep_probe_until_ready

            self._logger.info(
                "Stage 5: DeepProbe — Ollama /api/tags, Postgres SELECT 1, Redis PING "
                "(backoff 1–8s, deadline 60s)"
            )
            run_deep_probe_until_ready(
                env_values,
                logger=self._logger,
                announce=_print,
                timeout_sec=60.0,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during DeepProbe")
            raise RuntimeError("DeepProbe readiness check failed unexpectedly.") from exc
        try:
            from local_ai_stack import privacy_monitor as _privacy_monitor

            _privacy_monitor.maybe_arm_after_up(self._env_file, self._logger)
        except Exception as exc:
            self._logger.warning("Privacy monitor could not arm: %s", exc)
        _print("Stack started.")
        _print(f"AgenticSeek UI: http://localhost:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}")
        _print(f"AgenticSeek API: http://localhost:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health")
        _print(f"Open WebUI: http://localhost:{env_values.get('OPEN_WEBUI_PORT', '3001')}")
        _print(f"n8n: http://localhost:{env_values.get('N8N_PORT', '5678')}")
        _print(f"SearXNG: http://localhost:{env_values.get('SEARXNG_PORT', '8080')}")
        if _claude_agent_stack_available():
            _print(
                "Claude Agent: container ``local-ai-claude-agent`` on ``agentic-seek-net`` "
                "(ANTHROPIC_BASE_URL=http://ollama:11434 → host Ollama via extra_hosts). "
                "Mount config at ./.local/claude_config"
            )

