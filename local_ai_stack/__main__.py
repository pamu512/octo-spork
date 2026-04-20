from __future__ import annotations

import argparse
import configparser
import importlib.util
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.local"
EXAMPLE_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.example"
OVERLAY_SOURCE = ROOT / "overlays" / "agenticseek" / "sources" / "grounded_review.py"


def _print(message: str) -> None:
    print(message, flush=True)


def _run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _print(f"+ {' '.join(args)}")
    return subprocess.run(
        args,
        env=env,
        cwd=str(cwd or ROOT),
        check=check,
        text=True,
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def _ensure_env_file(env_file: Path) -> None:
    if env_file.exists():
        return
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(EXAMPLE_ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")


def _seed_env_secrets(env_file: Path) -> None:
    lines = env_file.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    for line in lines:
        if line.startswith("SEARXNG_SECRET_KEY=") and line.endswith("replace-me-with-a-random-secret"):
            updated.append(f"SEARXNG_SECRET_KEY={secrets.token_hex(24)}")
        elif line.startswith("N8N_ENCRYPTION_KEY=") and line.endswith("replace-me-with-a-random-secret"):
            updated.append(f"N8N_ENCRYPTION_KEY={secrets.token_hex(24)}")
        else:
            updated.append(line)
    env_file.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _resolve_agenticseek_path(env: dict[str, str]) -> Path:
    raw = env.get("AGENTICSEEK_DIR", "")
    if not raw:
        raise RuntimeError("AGENTICSEEK_DIR is empty in env file")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _normalize_dockerfile_backend(agenticseek_path: Path, use_native_arm64: bool) -> None:
    dockerfile = agenticseek_path / "Dockerfile.backend"
    if not dockerfile.exists():
        return
    content = dockerfile.read_text(encoding="utf-8")
    updated = re.sub(
        r"^FROM\s+--platform=linux/amd64\s+(.+)$",
        r"FROM \1",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    host_arch = platform.machine().lower()
    if host_arch in {"arm64", "aarch64"} and use_native_arm64:
        replacements = {
            "/linux64/chrome-linux64.zip": "/linux-arm64/chrome-linux-arm64.zip",
            "/opt/chrome-linux64/chrome": "/opt/chrome-linux-arm64/chrome",
            "/linux64/chromedriver-linux64.zip": "/linux-arm64/chromedriver-linux-arm64.zip",
            "/tmp/chromedriver-linux64/chromedriver": "/tmp/chromedriver-linux-arm64/chromedriver",
        }
    else:
        replacements = {
            "/linux-arm64/chrome-linux-arm64.zip": "/linux64/chrome-linux64.zip",
            "/opt/chrome-linux-arm64/chrome": "/opt/chrome-linux64/chrome",
            "/linux-arm64/chromedriver-linux-arm64.zip": "/linux64/chromedriver-linux64.zip",
            "/tmp/chromedriver-linux-arm64/chromedriver": "/tmp/chromedriver-linux64/chromedriver",
        }

    for old, new in replacements.items():
        updated = updated.replace(old, new)

    if updated != content:
        dockerfile.write_text(updated, encoding="utf-8")


def _patch_agenticseek_api(agenticseek_path: Path) -> None:
    api_path = agenticseek_path / "api.py"
    if not api_path.exists():
        return

    content = api_path.read_text(encoding="utf-8")
    import_line = "from sources.grounded_review import grounded_repo_review, should_use_grounded_review\n"
    anchor_import = "from sources.schemas import QueryRequest, QueryResponse\n"
    if import_line not in content and anchor_import in content:
        content = content.replace(anchor_import, anchor_import + import_line, 1)

    marker = "if should_use_grounded_review(request.query):"
    if marker not in content:
        anchor = "    if is_generating:\n"
        grounded_block = """    if should_use_grounded_review(request.query):
        grounded_result = grounded_repo_review(
            request.query,
            model=config["MAIN"]["provider_model"],
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        )
        query_resp.done = "true"
        query_resp.answer = grounded_result.get("answer", "")
        query_resp.reasoning = grounded_result.get("reasoning", "")
        query_resp.agent_name = "GroundedReview"
        query_resp.success = "true" if grounded_result.get("success", False) else "false"
        query_resp.blocks = {"sources": grounded_result.get("sources", [])}
        query_resp.status = "Grounded review completed"

        query_resp_history.append(
            {
                "done": query_resp.done,
                "answer": query_resp.answer,
                "agent_name": query_resp.agent_name,
                "success": query_resp.success,
                "blocks": query_resp.blocks,
                "status": query_resp.status,
                "uid": query_resp.uid,
            }
        )
        logger.info("Grounded query processed successfully")
        return JSONResponse(status_code=200, content=query_resp.jsonify())

"""
        if anchor in content:
            content = content.replace(anchor, grounded_block + anchor, 1)
    api_path.write_text(content, encoding="utf-8")


def _patch_agenticseek_browser_timeout(agenticseek_path: Path) -> None:
    browser_path = agenticseek_path / "sources" / "browser.py"
    if not browser_path.exists():
        return
    content = browser_path.read_text(encoding="utf-8")
    if "driver.command_executor.set_timeout" in content:
        return

    needle_stealth = "        driver = create_undetected_chromedriver(service, chrome_options)\n"
    insertion_stealth = """        driver = create_undetected_chromedriver(service, chrome_options)
        try:
            driver.command_executor.set_timeout(int(os.getenv("BROWSER_COMMAND_TIMEOUT", "300")))
        except Exception:
            pass
"""
    if needle_stealth in content:
        content = content.replace(needle_stealth, insertion_stealth, 1)

    needle_non_stealth = "        return webdriver.Chrome(service=service, options=chrome_options)\n"
    insertion_non_stealth = """        driver = webdriver.Chrome(service=service, options=chrome_options)
        try:
            driver.command_executor.set_timeout(int(os.getenv("BROWSER_COMMAND_TIMEOUT", "300")))
        except Exception:
            pass
        return driver
"""
    if needle_non_stealth in content:
        content = content.replace(needle_non_stealth, insertion_non_stealth, 1)

    browser_path.write_text(content, encoding="utf-8")


def _configure_agenticseek_ini(agenticseek_path: Path, env: dict[str, str]) -> None:
    config_path = agenticseek_path / "config.ini"
    if not config_path.exists():
        return
    ollama_base = env.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    ollama_model = env.get("OLLAMA_MODEL", "qwen2.5:14b")
    provider_address = ollama_base.replace("http://", "").replace("https://", "").rstrip("/")

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    if "MAIN" not in config:
        config["MAIN"] = {}
    if "BROWSER" not in config:
        config["BROWSER"] = {}
    config["MAIN"]["is_local"] = "True"
    config["MAIN"]["provider_name"] = "ollama"
    config["MAIN"]["provider_model"] = ollama_model
    config["MAIN"]["provider_server_address"] = provider_address
    config["BROWSER"]["headless_browser"] = "True"

    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)


def _compose_base_args(env_file: Path, agenticseek_path: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        "octo-spork-local-ai",
        "--env-file",
        str(env_file),
        "-f",
        str(agenticseek_path / "docker-compose.yml"),
        "-f",
        str(ROOT / "deploy" / "local-ai" / "docker-compose.addons.yml"),
    ]


def _merge_process_env(env_file_values: dict[str, str]) -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(env_file_values)
    # Do not set DOCKER_DEFAULT_PLATFORM here: it applies to every compose service and can
    # break pulls (e.g. Valkey) when a wrong-arch image is cached. AgenticSeek backend +
    # frontend use `platform: linux/amd64` in docker-compose.addons.yml on Apple Silicon.
    return merged


def _http_ok(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def _start_ollama_if_needed(env: dict[str, str], process_env: dict[str, str]) -> None:
    ollama_local_url = env.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
    probe_url = f"{ollama_local_url}/api/tags"
    if _http_ok(probe_url, timeout=3):
        return

    ollama_host = env.get("OLLAMA_HOST", "0.0.0.0:11434")
    _print(f"Starting Ollama on host ({ollama_host})...")
    start_env = process_env.copy()
    start_env["OLLAMA_HOST"] = ollama_host
    log_path = Path(tempfile.gettempdir()) / "octo-spork-ollama.log"
    with log_path.open("a", encoding="utf-8") as log_handle:
        subprocess.Popen(
            ["ollama", "serve"],
            env=start_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    time.sleep(3)
    if not _http_ok(probe_url, timeout=5):
        raise RuntimeError("Ollama did not become healthy after startup attempt")


def _wait_for_http(url: str, label: str, attempts: int = 60, sleep_seconds: int = 2) -> None:
    for _ in range(attempts):
        if _http_ok(url, timeout=5):
            _print(f"{label}: ok")
            return
        time.sleep(sleep_seconds)
    raise RuntimeError(f"{label}: failed after {attempts} attempts ({url})")


def bootstrap(env_file: Path) -> tuple[dict[str, str], Path]:
    _ensure_env_file(env_file)
    _seed_env_secrets(env_file)
    env_values = _parse_env_file(env_file)

    agenticseek_path = _resolve_agenticseek_path(env_values)
    agenticseek_path.parent.mkdir(parents=True, exist_ok=True)
    ref = env_values.get("AGENTICSEEK_REF", "main")
    if (agenticseek_path / ".git").is_dir():
        _run(["git", "-C", str(agenticseek_path), "fetch", "--depth", "1", "origin", ref])
        _run(["git", "-C", str(agenticseek_path), "checkout", "-f", ref])
        _run(["git", "-C", str(agenticseek_path), "pull", "--ff-only", "origin", ref])
    else:
        _run(["git", "clone", "--depth", "1", "--branch", ref, "https://github.com/Fosowl/agenticSeek.git", str(agenticseek_path)])

    if OVERLAY_SOURCE.exists():
        target = agenticseek_path / "sources" / "grounded_review.py"
        target.write_text(OVERLAY_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")

    use_native_arm64 = env_values.get("AGENTICSEEK_NATIVE_ARM64", "false").lower() == "true"
    _normalize_dockerfile_backend(agenticseek_path, use_native_arm64)
    _patch_agenticseek_api(agenticseek_path)
    _patch_agenticseek_browser_timeout(agenticseek_path)
    _configure_agenticseek_ini(agenticseek_path, env_values)

    _print(f"AgenticSeek is ready at: {agenticseek_path}")
    _print(f"Environment file: {env_file}")
    return env_values, agenticseek_path


def command_up(env_file: Path) -> None:
    env_values, agenticseek_path = bootstrap(env_file)
    process_env = _merge_process_env(env_values)
    _start_ollama_if_needed(env_values, process_env)
    model = env_values.get("OLLAMA_MODEL", "qwen2.5:14b")
    _run(["ollama", "pull", model], env=process_env)

    compose_cmd = _compose_base_args(env_file, agenticseek_path) + ["--profile", "full", "--profile", "addons", "up", "-d", "--build"]
    _run(compose_cmd, env=process_env)

    _print("Stack started.")
    _print(f"AgenticSeek UI: http://localhost:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}")
    _print(f"AgenticSeek API: http://localhost:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health")
    _print(f"Open WebUI: http://localhost:{env_values.get('OPEN_WEBUI_PORT', '3001')}")
    _print(f"n8n: http://localhost:{env_values.get('N8N_PORT', '5678')}")
    _print(f"SearXNG: http://localhost:{env_values.get('SEARXNG_PORT', '8080')}")


def command_verify(env_file: Path) -> None:
    env_values = _parse_env_file(env_file)
    agenticseek_path = _resolve_agenticseek_path(env_values)
    process_env = _merge_process_env(env_values)
    # Use IPv4 loopback: on some hosts `localhost` resolves to ::1 first while Docker
    # publishes ports on IPv4 only, which yields connection reset during health checks.
    h = "127.0.0.1"

    _print("Checking service endpoints...")
    ollama_local_url = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
    _wait_for_http(f"{ollama_local_url}/api/tags", "ollama")
    _wait_for_http(f"http://{h}:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health", "agenticseek-backend")
    _wait_for_http(f"http://{h}:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}", "agenticseek-frontend")
    _wait_for_http(f"http://{h}:{env_values.get('OPEN_WEBUI_PORT', '3001')}", "open-webui")
    _wait_for_http(f"http://{h}:{env_values.get('N8N_PORT', '5678')}/healthz", "n8n")
    _wait_for_http(f"http://{h}:{env_values.get('SEARXNG_PORT', '8080')}", "searxng")

    _print("Verifying n8n container can reach Ollama...")
    check_script = (
        "fetch(process.env.OLLAMA_BASE_URL + '/api/tags')"
        ".then((r)=>{if(!r.ok){throw new Error('HTTP '+r.status)};return r.text();})"
        ".then((t)=>{console.log(t.slice(0, 160));})"
        ".catch((e)=>{console.error(e);process.exit(1);});"
    )
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + ["exec", "-T", "n8n", "node", "-e", check_script]
    _run(compose_cmd, env=process_env)
    _print("All verification checks passed.")


def command_down(env_file: Path) -> None:
    env_values = _parse_env_file(env_file)
    agenticseek_path = _resolve_agenticseek_path(env_values)
    process_env = _merge_process_env(env_values)
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + ["--profile", "full", "--profile", "addons", "down"]
    _run(compose_cmd, env=process_env)


def _env_file_from_arg(raw_path: str | None) -> Path:
    if not raw_path:
        return DEFAULT_ENV_FILE
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _load_grounded_review():
    spec = importlib.util.spec_from_file_location("grounded_review", OVERLAY_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load grounded_review module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo_path_from_arg(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def command_diff_preview(repo: str, base: str, head: str, query: str) -> None:
    gr = _load_grounded_review()
    repo_path = _repo_path_from_arg(repo)
    snapshot = gr.fetch_local_diff_snapshot(repo_path, query, base, head)
    if snapshot is None:
        raise RuntimeError(
            "Could not build a diff snapshot. Use a git checkout where base and head resolve."
        )
    _print(gr.format_diff_preview_markdown(snapshot, base, head))


def command_review_diff(env_file: Path, repo: str, base: str, head: str, query: str) -> None:
    _ensure_env_file(env_file)
    env_values = _parse_env_file(env_file)
    ollama_url = (env_values.get("OLLAMA_LOCAL_URL") or env_values.get("OLLAMA_BASE_URL") or "").strip()
    if not ollama_url:
        ollama_url = "http://127.0.0.1:11434"
    ollama_url = ollama_url.rstrip("/")
    model = env_values.get("OLLAMA_MODEL", "qwen2.5:14b").strip() or "qwen2.5:14b"

    gr = _load_grounded_review()
    repo_path = _repo_path_from_arg(repo)
    result = gr.grounded_local_diff_review(query, model, ollama_url, repo_path, base, head)
    if not result.get("success"):
        raise RuntimeError(str(result.get("answer", "Grounded diff review failed")))
    _print(str(result.get("answer", "")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform local AI stack runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "up", "verify", "down"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")

    diff_prev = subparsers.add_parser(
        "diff-preview",
        help="Print a markdown diff triage preview (no Ollama; safe for GitHub-hosted CI)",
    )
    diff_prev.add_argument("--repo", default=".", help="Path to git repository root")
    diff_prev.add_argument("--base", required=True, help="Git base ref (e.g. origin/main or SHA)")
    diff_prev.add_argument("--head", default="HEAD", help="Git head ref (default HEAD)")
    diff_prev.add_argument(
        "--query",
        default="Review the changed files for security, correctness, and regression risks.",
        help="Triage query passed to file selection",
    )

    rev_diff = subparsers.add_parser(
        "review-diff",
        help="Run grounded LLM review over files touched between base and head (requires Ollama)",
    )
    rev_diff.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    rev_diff.add_argument("--repo", default=".", help="Path to git repository root")
    rev_diff.add_argument("--base", required=True, help="Git base ref")
    rev_diff.add_argument("--head", default="HEAD", help="Git head ref (default HEAD)")
    rev_diff.add_argument(
        "--query",
        default="Perform a grounded code review of the diff; focus on security and regressions.",
        help="Review prompt",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    env_file = _env_file_from_arg(getattr(args, "env_file", None))

    try:
        if args.command == "bootstrap":
            bootstrap(env_file)
        elif args.command == "up":
            command_up(env_file)
        elif args.command == "verify":
            command_verify(env_file)
        elif args.command == "down":
            command_down(env_file)
        elif args.command == "diff-preview":
            command_diff_preview(args.repo, args.base, args.head, args.query)
        elif args.command == "review-diff":
            command_review_diff(env_file, args.repo, args.base, args.head, args.query)
        else:
            parser.error(f"Unknown command: {args.command}")
    except subprocess.CalledProcessError as exc:
        _print(f"Command failed with exit code {exc.returncode}")
        return exc.returncode or 1
    except Exception as exc:  # noqa: BLE001
        _print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

