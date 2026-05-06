"""Launch cloudflared or ngrok for the local webhook server and optionally sync GitHub App webhook URL."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import IO, Any, Literal

from dotenv import load_dotenv

from github_bot.auth import GitHubAuth

load_dotenv()

_LOG = logging.getLogger("github_bot.tunnel")

# Quick Tunnel public hostname (cloudflared).
_CLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com/?")

_GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")


def _which_or_raise(cmd: str) -> str:
    path = shutil.which(cmd)
    if not path:
        raise FileNotFoundError(f"{cmd!r} not found on PATH; install it or choose another --provider")
    return path


def _discover_provider(requested: Literal["auto", "cloudflared", "ngrok"]) -> Literal["cloudflared", "ngrok"]:
    has_cf = shutil.which("cloudflared") is not None
    has_ng = shutil.which("ngrok") is not None
    if requested == "cloudflared":
        _which_or_raise("cloudflared")
        return "cloudflared"
    if requested == "ngrok":
        _which_or_raise("ngrok")
        return "ngrok"
    if has_cf:
        _LOG.info("Using cloudflared (found on PATH)")
        return "cloudflared"
    if has_ng:
        _LOG.info("cloudflared not found; using ngrok")
        return "ngrok"
    raise FileNotFoundError(
        "Neither cloudflared nor ngrok is on PATH. Install one or pass --provider explicitly."
    )


def _extract_trycloudflare_url(line: str) -> str | None:
    m = _CLOUDFLARE_URL_RE.search(line)
    if m:
        return m.group(0).rstrip("/")
    return None


def _pump_stream(pipe: IO[str], label: str, result_q: queue.Queue[str]) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            text = line.rstrip()
            if text:
                _LOG.info("[%s] %s", label, text)
            url = _extract_trycloudflare_url(line)
            if url:
                result_q.put(url)
    finally:
        pipe.close()


def _wait_cloudflared_public_url(proc: subprocess.Popen[str], deadline_sec: float = 120.0) -> str:
    """Read cloudflared stderr/stdout until a trycloudflare.com URL appears."""
    result_q: queue.Queue[str] = queue.Queue()
    threads = [
        threading.Thread(target=_pump_stream, args=(proc.stdout, "cloudflared-out", result_q), daemon=True),
        threading.Thread(target=_pump_stream, args=(proc.stderr, "cloudflared-err", result_q), daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        try:
            url = result_q.get(timeout=0.5)
            return url
        except queue.Empty:
            rc = proc.poll()
            if rc is not None and result_q.empty():
                raise RuntimeError(f"cloudflared exited early with code {rc}") from None
    raise TimeoutError("Timed out waiting for a trycloudflare.com URL from cloudflared")


def _wait_ngrok_public_url(
    api_base: str,
    deadline_sec: float = 90.0,
    poll_interval: float = 0.4,
) -> str:
    """Poll ngrok local API until an https tunnel public URL is available."""
    api = api_base.rstrip("/") + "/api/tunnels"
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(api, headers={"User-Agent": "octo-spork-tunnel-runner"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            _LOG.debug("ngrok API not ready yet: %s", exc)
            time.sleep(poll_interval)
            continue
        tunnels = data.get("tunnels") or []
        for t in tunnels:
            pub = str(t.get("public_url") or "")
            if pub.startswith("https://"):
                return pub.rstrip("/")
        time.sleep(poll_interval)
    raise TimeoutError(f"No https tunnel found via ngrok API at {api}")


def _github_request(
    method: str,
    url: str,
    *,
    bearer: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | None]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            "User-Agent": "octo-spork-tunnel-runner",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
            if not raw.strip():
                return code, None
            return code, json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail[:2500]}") from exc


def update_github_app_webhook_url(public_base_url: str, webhook_path: str = "/webhook") -> None:
    """Set the GitHub App webhook URL via REST (requires App JWT — see docs).

    ``GH_ADMIN_TOKEN`` must be set (non-empty) to opt in to mutating GitHub settings.
    Authentication uses :class:`GitHubAuth` (``GITHUB_APP_ID`` + ``GITHUB_APP_PRIVATE_KEY_PATH``):
    GitHub's ``PATCH /app/hook/config`` endpoint requires a GitHub App JWT, not a PAT.
    """
    admin_gate = os.environ.get("GH_ADMIN_TOKEN")
    if admin_gate is None or not str(admin_gate).strip():
        _LOG.info("GH_ADMIN_TOKEN not set; skipping GitHub App webhook URL update.")
        return

    app_id = os.environ.get("GITHUB_APP_ID")
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if not app_id or not str(app_id).strip() or not key_path or not str(key_path).strip():
        _LOG.warning(
            "GH_ADMIN_TOKEN is set but GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH are missing; "
            "cannot mint a GitHub App JWT for PATCH /app/hook/config."
        )
        return

    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    auth = GitHubAuth(
        app_id=str(app_id).strip(),
        private_key_path=str(key_path).strip(),
        api_base_url=api_base,
    )
    jwt_token = auth.create_jwt()

    full_url = public_base_url.rstrip("/") + webhook_path
    _LOG.info("Updating GitHub App webhook URL to %s (via GitHub REST)", full_url)

    code, current = _github_request("GET", f"{api_base}/app/hook/config", bearer=jwt_token)
    if code != 200 or not isinstance(current, dict):
        raise RuntimeError("Unexpected GET /app/hook/config response")

    patch_body: dict[str, Any] = {"url": full_url}
    ct = current.get("content_type")
    if ct:
        patch_body["content_type"] = ct
    ssl_flag = current.get("insecure_ssl")
    if ssl_flag is not None:
        patch_body["insecure_ssl"] = ssl_flag
    secret = current.get("secret")
    if isinstance(secret, str) and secret and not secret.startswith("*"):
        patch_body["secret"] = secret

    patch_code, _patch_body = _github_request(
        "PATCH", f"{api_base}/app/hook/config", bearer=jwt_token, body=patch_body
    )
    if patch_code != 200:
        raise RuntimeError(f"PATCH /app/hook/config failed with HTTP {patch_code}")
    _LOG.info("GitHub App webhook URL updated successfully.")


def run_tunnel(
    *,
    port: int,
    provider: Literal["auto", "cloudflared", "ngrok"],
    ngrok_api_base: str,
    webhook_path: str,
) -> int:
    """Start tunnel process, capture URL, optionally PATCH GitHub App webhook, block until interrupted."""
    kind = _discover_provider(provider)
    local_url = f"http://127.0.0.1:{port}"

    proc: subprocess.Popen[str]
    if kind == "cloudflared":
        exe = _which_or_raise("cloudflared")
        _LOG.info("Starting cloudflared Quick Tunnel -> %s", local_url)
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            public = _wait_cloudflared_public_url(proc)
        except Exception:
            proc.terminate()
            raise
    else:
        exe = _which_or_raise("ngrok")
        _LOG.info("Starting ngrok -> %s (API %s)", local_url, ngrok_api_base)
        proc = subprocess.Popen(
            [exe, "http", str(port), "--log=stdout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None

        def log_ngrok_stdout() -> None:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                _LOG.info("[ngrok] %s", line.rstrip())

        threading.Thread(target=log_ngrok_stdout, daemon=True).start()
        try:
            public = _wait_ngrok_public_url(ngrok_api_base)
        except Exception:
            proc.terminate()
            raise

    _LOG.info("Public tunnel URL: %s", public)

    try:
        update_github_app_webhook_url(public, webhook_path=webhook_path)
    except Exception as exc:
        _LOG.error("GitHub webhook update failed: %s", exc)

    _LOG.info("Tunnel is running (Ctrl+C to stop). Forwarding traffic to %s", local_url)

    def handle_sigint(*_args: object) -> None:
        _LOG.info("Stopping tunnel...")
        proc.terminate()

    signal.signal(signal.SIGINT, handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_sigint)

    rc = proc.wait()
    _LOG.info("Tunnel process exited with code %s", rc)
    return int(rc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Expose the local webhook port via cloudflared or ngrok, optionally PATCH the "
            "GitHub App webhook URL when GH_ADMIN_TOKEN and App credentials are set."
        )
    )
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8000")), metavar="N")
    p.add_argument(
        "--provider",
        choices=("auto", "cloudflared", "ngrok"),
        default="auto",
        help="Tunnel backend (default: auto = prefer cloudflared)",
    )
    p.add_argument(
        "--ngrok-api",
        default=os.environ.get("NGROK_API_URL", "http://127.0.0.1:4040"),
        help="ngrok local API base URL (default: http://127.0.0.1:4040)",
    )
    p.add_argument(
        "--webhook-path",
        default=os.environ.get("GITHUB_WEBHOOK_PATH", "/webhook"),
        help="Path appended to the public URL for GitHub (default: /webhook)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run_tunnel(
            port=args.port,
            provider=args.provider,
            ngrok_api_base=args.ngrok_api,
            webhook_path=args.webhook_path,
        )
    except (FileNotFoundError, TimeoutError, RuntimeError) as exc:
        _LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
