"""Interactive follow-up REPL: last grounded review session + Ollama (same stack as AgenticSeek reviews)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _parse_env_merge(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
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


def _build_system_message(state: dict[str, Any]) -> str:
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    extras = state.get("extras") if isinstance(state.get("extras"), dict) else {}
    owner = meta.get("owner") or "?"
    repo = meta.get("repo") or "?"
    rev = meta.get("revision_sha") or ""
    q = str(state.get("query") or "")
    prompt = str(state.get("prompt") or "")
    answer = str(state.get("answer") or "")
    sec = str(extras.get("security_context_block") or "")
    codeql = str(extras.get("codeql_evidence_block") or "")
    dep = str(extras.get("dependency_audit_block") or "")

    blocks = [
        "## Instructions",
        "You are assisting with follow-up questions about a completed **grounded repository review**.",
        "Answer using ONLY the evidence below (full model prompt, prior review output, and scanner excerpts).",
        "If asked about a line or file, locate it in the CONTEXT. If absent, say you cannot see it in this session.",
        "Be concise. Use markdown when helpful.",
        "",
        f"## Session metadata\n- Repository: `{owner}/{repo}`\n- Revision (hint): `{rev}`\n- Original review query:\n{q}",
        "",
        "## CONTEXT — full prompt sent to the review model (evidence package)",
        prompt or "_(no prompt capture — empty)_",
        "",
        "## CONTEXT — model review output (answer)",
        answer or "_(empty)_",
    ]
    if sec.strip():
        blocks.extend(["", "## CONTEXT — Trivy / security scanner block (excerpt)", sec])
    if codeql.strip():
        blocks.extend(["", "## CONTEXT — CodeQL evidence block (excerpt)", codeql])
    if dep.strip():
        blocks.extend(["", "## CONTEXT — dependency audit block (excerpt)", dep])
    return "\n".join(blocks)


def _ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: float,
) -> str:
    import httpx

    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2048},
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"].strip()
    return str(data.get("response") or "").strip()


def run_octo_chat(repo_root: Path, *, env_file: Path | None) -> int:
    """Start stdin REPL. Returns exit code."""
    repo_root = repo_root.expanduser().resolve()
    os.environ.setdefault("OCTO_SPORK_REPO_ROOT", str(repo_root))

    octo_src = Path(__file__).resolve().parents[1] / "src"
    if octo_src.is_dir() and str(octo_src) not in sys.path:
        sys.path.insert(0, str(octo_src))

    from observability.review_session_store import load_last_review_session

    state = load_last_review_session(repo_root)
    if not state:
        p = repo_root / ".octo" / "review_session" / "last_review.json"
        print(
            f"No saved review session at {p}\n"
            "Run a grounded review first (e.g. `python -m local_ai_stack review-diff ...`) "
            "from this repository root so the last prompt and answer are captured.",
            file=sys.stderr,
        )
        return 1

    if env_file and env_file.is_file():
        for k, v in _parse_env_merge(env_file).items():
            if k not in os.environ or not str(os.environ.get(k, "")).strip():
                os.environ[k] = v

    base = (
        os.environ.get("OLLAMA_LOCAL_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or str(state.get("ollama_base_url") or "")
        or "http://127.0.0.1:11434"
    ).strip().rstrip("/")
    model = (
        (os.environ.get("OLLAMA_MODEL") or "").strip()
        or str(state.get("model") or "")
        or "llama3.2"
    ).strip() or "llama3.2"

    priv_map: dict[str, str] = {}
    try:
        from observability.privacy_filter import redact_for_llm

        sys_full = _build_system_message(state)
        sys_redacted, priv_map = redact_for_llm(sys_full)
    except ImportError:
        sys_redacted = _build_system_message(state)

    cap = int(os.environ.get("OCTO_CHAT_SYSTEM_MAX_CHARS", "120000"))
    if len(sys_redacted) > cap:
        sys_redacted = (
            sys_redacted[: cap - 120]
            + "\n\n… [system context truncated; raise OCTO_CHAT_SYSTEM_MAX_CHARS]\n"
        )

    messages: list[dict[str, str]] = [{"role": "system", "content": sys_redacted}]

    timeout = float(os.environ.get("OCTO_CHAT_TIMEOUT_SEC", "180") or "180")

    print(
        "Octo chat — follow-up on last grounded review (Ollama / AgenticSeek stack). "
        "Commands: /exit /quit, /path (session file), /context (sizes). "
        f"Model: {model} @ {base}\n",
        flush=True,
    )

    while True:
        try:
            line = input("octo> ")
        except EOFError:
            print()
            break
        raw = line.strip()
        if not raw:
            continue
        low = raw.lower()
        if low in ("/exit", "/quit", "exit", "quit"):
            break
        if low == "/path":
            from observability.review_session_store import review_session_path

            print(review_session_path(repo_root), flush=True)
            continue
        if low == "/context":
            print(f"[system message: {len(sys_redacted)} chars]", flush=True)
            continue

        send_user = raw
        turn_maps = dict(priv_map)
        try:
            from observability.privacy_filter import redact_for_llm, unredact_response

            send_user, umap = redact_for_llm(raw)
            turn_maps.update(umap)
        except ImportError:
            pass

        turn_msgs = list(messages)
        turn_msgs.append({"role": "user", "content": send_user})
        try:
            reply = _ollama_chat(base, model, turn_msgs, timeout=timeout)
            if turn_maps:
                try:
                    from observability.privacy_filter import unredact_response

                    reply = unredact_response(reply, turn_maps)
                except ImportError:
                    pass
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
            continue

        print(reply + "\n", flush=True)
        messages.append({"role": "user", "content": send_user})
        messages.append({"role": "assistant", "content": reply})

    return 0


def command_octo_chat(env_file: Path | None, repo: str) -> int:
    root = Path(repo).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return run_octo_chat(root.resolve(), env_file=env_file)
