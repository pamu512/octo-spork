"""HTTP surface for nightly audit status + optional trigger (n8n / Hermes)."""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status

from observability.audit_status import audit_status_payload

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/octo/audit", tags=["audit"])

_ENV_KEY = "OCTO_AUDIT_API_KEY"
_run_lock = threading.Lock()
_run_state: dict[str, Any] = {"running": False, "last_exit_code": None, "last_error": None}


def _expected_api_key() -> str:
    return (os.environ.get(_ENV_KEY) or "").strip()


def _require_api_key(x_api_key: str | None, authorization: str | None) -> None:
    expected = _expected_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_ENV_KEY} is not set — refuse audit trigger/status auth.",
        )
    got = (x_api_key or "").strip()
    if not got and authorization and authorization.lower().startswith("bearer "):
        got = authorization[7:].strip()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def _repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # github_bot/ → src/ → repo
    return Path(__file__).resolve().parents[2]


@router.get("/status")
def get_audit_status(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    require_key: bool = Query(
        default=False,
        description="When true, require OCTO_AUDIT_API_KEY even if unset would allow open GET",
    ),
) -> dict[str, Any]:
    """Return latest nightly log tail + metrics.db summary.

    If ``OCTO_AUDIT_API_KEY`` is set, the key is required. If unset, GET is open (local-dev).
    """
    expected = _expected_api_key()
    if expected or require_key:
        _require_api_key(x_api_key, authorization)
    payload = audit_status_payload()
    payload["trigger"] = dict(_run_state)
    return payload


def _run_nightly_audit_job(repo: str) -> None:
    root = _repo_root()
    env = os.environ.copy()
    env["OCTO_SPORK_REPO_ROOT"] = str(root)
    env["OCTO_AUDIT_REPO"] = repo
    env.setdefault("PYTHONPATH", str(root / "src"))
    if str(root / "src") not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            ["python3", "-m", "local_ai_stack", "nightly-audit", "--repo", repo],
            cwd=str(root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("OCTO_AUDIT_TRIGGER_TIMEOUT_SEC", "3600")),
        )
        with _run_lock:
            _run_state["last_exit_code"] = int(completed.returncode)
            _run_state["last_error"] = None
            if completed.returncode != 0:
                err = (completed.stderr or completed.stdout or "")[:2000]
                _run_state["last_error"] = err or f"exit {completed.returncode}"
            _run_state["running"] = False
    except Exception as exc:
        _LOG.exception("nightly-audit trigger failed")
        with _run_lock:
            _run_state["last_exit_code"] = -1
            _run_state["last_error"] = str(exc)[:2000]
            _run_state["running"] = False


@router.post("/run")
def trigger_nightly_audit(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    repo: str = Query(default=".", description="Repo path for OCTO_AUDIT_REPO"),
) -> dict[str, Any]:
    """Start ``nightly-audit`` in a background thread (requires ``OCTO_AUDIT_API_KEY``)."""

    _require_api_key(x_api_key, authorization)
    with _run_lock:
        if _run_state.get("running"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="nightly-audit already running",
            )
        _run_state["running"] = True
        _run_state["last_error"] = None
    thread = threading.Thread(
        target=_run_nightly_audit_job,
        args=(repo,),
        name="octo-nightly-audit",
        daemon=True,
    )
    thread.start()
    return {"accepted": True, "repo": repo, "status_path": "/octo/audit/status"}
