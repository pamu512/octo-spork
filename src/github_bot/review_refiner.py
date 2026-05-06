"""Review Refiner: send AI-generated PR review text through local Claude Code to de-noise before GitHub."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

_LOG = logging.getLogger(__name__)

_FENCED_MD_RE = re.compile(
    r"^\s*```(?:markdown|md)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


REFINER_TASK_PROMPT_TEMPLATE = """You are refining an automated pull-request code review before it is posted on GitHub.

## Your task
1. **Remove hallucinations** — drop claims not supported by the evidence in the review text or the supplied PR context.
2. **Make every remaining point actionable** — each item should tell the author what to change or verify (no vague praise-only fluff unless it cites concrete code).
3. **Require citations** — each finding must reference a **repository-relative file path** and either a **line range**, a **short verbatim quote** from the context, or an explicit note when only the diff excerpt supports it.
4. **Consolidate duplicate** observations about the same file/area.
5. Preserve severity / importance ordering when obvious; drop **severity** labels if they were invented without basis.

## Output format
Respond with **only** the refined review as GitHub-flavored Markdown (no preamble, no “Certainly!”, no JSON).
Use clear `###` sections per theme or per file as appropriate. If nothing substantive remains after filtering, output exactly:

_(No actionable, cited findings remain after refinement.)_

---

## PR context (for grounding; may be truncated)

{pr_context}

---

## Draft review to refine

{draft}
"""


def refinement_enabled() -> bool:
    """True when ``OCTO_REVIEW_REFINER_ENABLED`` is set to a truthy value."""
    return os.environ.get("OCTO_REVIEW_REFINER_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def refinement_strict_withhold_raw() -> bool:
    """When True (default), failed refinement must not fall back to posting the raw draft."""
    return os.environ.get("OCTO_REVIEW_REFINER_STRICT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def resolve_refiner_workspace(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _truncate(text: str, max_chars: int) -> str:
    t = text.strip()
    if len(t) <= max_chars:
        return t
    head = max_chars // 2
    tail = max_chars - head - 80
    return t[:head] + "\n\n… [truncated] …\n\n" + t[-tail:]


def strip_markdown_fence(text: str) -> str:
    """If the model wrapped output in a single fenced block, unwrap it."""
    m = _FENCED_MD_RE.match(text.strip())
    if m:
        return m.group("body").strip()
    return text.strip()


def build_refiner_prompt(draft: str, pr_context: str) -> str:
    max_ctx = int(os.environ.get("OCTO_REVIEW_REFINER_MAX_CONTEXT_CHARS", "24000"))
    max_draft = int(os.environ.get("OCTO_REVIEW_REFINER_MAX_DRAFT_CHARS", "120000"))
    style_extra = ""
    try:
        from github_bot.style_prefs import format_style_guide_block_for_review

        sg = format_style_guide_block_for_review()
        if sg:
            style_extra = "\n\n---\n\n" + sg
    except ImportError:
        pass
    body = REFINER_TASK_PROMPT_TEMPLATE.format(
        pr_context=_truncate(pr_context, max_ctx),
        draft=_truncate(draft, max_draft),
    )
    body += style_extra
    cap = int(os.environ.get("OCTO_REVIEW_REFINER_MAX_PROMPT_CHARS", "200000"))
    return _truncate(body, cap) if len(body) > cap else body


def run_claude_refine(
    workspace: Path,
    prompt: str,
    *,
    no_add_dir: bool = False,
) -> tuple[int, str, str]:
    """Invoke local ``claude`` via :func:`claude_bridge.run_agent.assemble_claude_command`."""
    from claude_bridge.run_agent import assemble_claude_command

    timeout_raw = os.environ.get("OCTO_REVIEW_REFINER_TIMEOUT_SEC", "").strip()
    timeout_sec = float(timeout_raw) if timeout_raw else None

    cmd = assemble_claude_command(
        workspace=workspace,
        claude_argv=["-p", prompt],
        no_add_dir=no_add_dir,
    )
    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def refine_review_markdown(
    draft: str,
    *,
    pr_context: str = "",
    workspace: Path | None = None,
) -> str | None:
    """Return refined markdown, or ``None`` if refinement failed and strict mode applies.

    When :func:`refinement_enabled` is False, returns ``draft`` unchanged (caller should skip calling).
    """
    if not refinement_enabled():
        raise RuntimeError("refine_review_markdown called while refinement is disabled")

    ws = resolve_refiner_workspace(workspace)
    prompt = build_refiner_prompt(draft, pr_context)
    code, out, err = run_claude_refine(
        ws,
        prompt,
        no_add_dir=os.environ.get("OCTO_REVIEW_REFINER_NO_ADD_DIR", "").strip() == "1",
    )
    if code != 0:
        _LOG.warning(
            "Review refiner: claude exited %s stderr=%s",
            code,
            (err or "")[:2000],
        )
        return None

    refined = strip_markdown_fence(out)
    if not refined.strip():
        _LOG.warning("Review refiner: empty stdout from claude")
        return None
    return refined


def refine_review_or_original(
    draft: str,
    *,
    pr_context: str = "",
    workspace: Path | None = None,
) -> str:
    """If refinement is disabled, return ``draft``. If enabled, refine or fall back per env strictness."""
    if not refinement_enabled():
        return draft
    refined = refine_review_markdown(draft, pr_context=pr_context, workspace=workspace)
    if refined is not None:
        return refined
    if refinement_strict_withhold_raw():
        return (
            "## Review refinement unavailable\n\n"
            "_Automated refinement did not return usable output (timeout, CLI error, or empty response). "
            "The raw AI review was **not** posted per `OCTO_REVIEW_REFINER_STRICT`._"
        )
    return draft


def maybe_refine_ai_section_for_integrator(
    review_markdown: str,
    *,
    pr_context: str,
    repo_path: Path,
) -> str:
    """Used by ``github_integrator``: refine the grounded AI section before wrapping in evidence comment."""
    if not refinement_enabled():
        return review_markdown
    refined = refine_review_markdown(
        review_markdown,
        pr_context=pr_context,
        workspace=repo_path,
    )
    if refined is not None:
        return refined
    if refinement_strict_withhold_raw():
        return (
            "## Review refinement unavailable\n\n"
            "_Local Claude refinement failed or produced no output; raw grounded review withheld._"
        )
    return review_markdown


def parse_cli(argv: list[str] | None = None) -> int:
    """CLI: read draft from stdin or file, write refined markdown to stdout."""
    import argparse

    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="De-noise AI PR review text via local Claude Code.")
    parser.add_argument("--workspace", type=Path, default=None, help="Octo-spork repo root for sidecar")
    parser.add_argument("--context-file", type=Path, default=None, help="PR context markdown/text file")
    parser.add_argument("--draft-file", type=Path, default=None, help="Draft review file (default: stdin)")
    args = parser.parse_args(argv)

    ctx = ""
    if args.context_file is not None:
        ctx = args.context_file.read_text(encoding="utf-8", errors="replace")

    if args.draft_file is not None:
        draft = args.draft_file.read_text(encoding="utf-8", errors="replace")
    else:
        draft = sys.stdin.read()

    os.environ.setdefault("OCTO_REVIEW_REFINER_ENABLED", "1")
    ws = resolve_refiner_workspace(args.workspace)
    refined = refine_review_markdown(draft, pr_context=ctx, workspace=ws)
    if refined is None:
        sys.stderr.write("[review-refiner] Refinement failed.\n")
        return 1
    sys.stdout.write(refined)
    if not refined.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(parse_cli())
