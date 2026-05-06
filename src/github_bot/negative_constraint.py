"""Negative constraint agent: adversarial risk scoring (1–10) for each fix implied by the primary review."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

_LOG = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


NC_SYSTEM_PROMPT = """You are a hostile security reviewer (red team). The user message contains an automated code review that may propose remediations, refactors, or hardening steps.

Your ONLY job:
1. Enumerate every DISTINCT proposed or implied **code change** (fixes, new checks, dependency moves, config changes, API usage changes). Merge duplicates.
2. For each item, explain how that change could be **misused**, **bypassed**, or could **introduce new weaknesses** if applied carelessly (not whether the original bug was real).

Output requirements:
- Respond with **one JSON object only** (no markdown fences, no commentary before or after).
- Schema: {"items":[{"change_summary":"short label","risk_score":7,"exploit_scenarios":"one or two sentences"},{"change_summary":"...","risk_score":3,"exploit_scenarios":"..."}]}
- "risk_score" MUST be an integer from **1** (low concern) to **10** (very dangerous if applied naïvely).
- Include **at least one** item. If the review does not propose concrete code-related actions, use one item summarizing that fact with risk_score 1.
- Maximum **20** items; prioritize the most specific, actionable proposals first.
"""


def negative_constraint_enabled() -> bool:
    return os.environ.get("OCTO_NEGATIVE_CONSTRAINT_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _max_review_chars() -> int:
    raw = (os.environ.get("OCTO_NEGATIVE_CONSTRAINT_MAX_REVIEW_CHARS") or "").strip()
    if raw.isdigit():
        return max(2000, int(raw))
    return 28_000


def _timeout_sec() -> float:
    raw = (os.environ.get("OCTO_NEGATIVE_CONSTRAINT_TIMEOUT_SEC") or "").strip()
    if raw:
        try:
            return max(15.0, float(raw))
        except ValueError:
            pass
    return 120.0


def _ollama_chat(base_url: str, model: str, system: str, user: str, *, timeout: float) -> str:
    import httpx

    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.15, "num_predict": 3072},
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return str(msg["content"]).strip()
    return str(data.get("response") or "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    t = text.strip()
    for m in _JSON_FENCE_RE.finditer(t):
        block = m.group(1).strip()
        if block.startswith("{"):
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start : end + 1])
    raise ValueError("no JSON object found in model output")


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    items_in = raw.get("items")
    if not isinstance(items_in, list):
        items_in = []
    out_items: list[dict[str, Any]] = []
    for it in items_in[:20]:
        if not isinstance(it, dict):
            continue
        summ = str(it.get("change_summary") or it.get("summary") or "").strip() or "(unspecified)"
        scen = str(it.get("exploit_scenarios") or it.get("rationale") or "").strip() or "—"
        try:
            score = int(it.get("risk_score"))
        except (TypeError, ValueError):
            score = 5
        score = max(1, min(10, score))
        out_items.append({"change_summary": summ, "risk_score": score, "exploit_scenarios": scen})
    if not out_items:
        out_items.append(
            {
                "change_summary": "(no structured items parsed)",
                "risk_score": 5,
                "exploit_scenarios": "Model output could not be normalized; treat scores as uncertain.",
            }
        )
    return {"items": out_items}


def format_risk_analysis_markdown(payload: dict[str, Any]) -> str:
    """Turn normalized payload into a GitHub-flavored markdown section."""
    items = payload.get("items") or []
    lines = [
        "### Negative constraint — risk analysis (proposed fixes)",
        "",
        "_Adversarial triage: higher **risk** means greater danger if the suggestion is applied without extra safeguards._",
        "",
        "| # | Proposed change | Risk (1–10) | Exploit / misuse angles |",
        "|---|-----------------|------------|-------------------------|",
    ]
    for i, it in enumerate(items, 1):
        summ = str(it.get("change_summary", "")).replace("|", "\\|").replace("\n", " ")[:220]
        scen = str(it.get("exploit_scenarios", "")).replace("|", "\\|").replace("\n", " ")[:900]
        score = int(it.get("risk_score", 1))
        score = max(1, min(10, score))
        lines.append(f"| {i} | {summ} | {score} | {scen} |")
    lines.append("")
    return "\n".join(lines)


def build_negative_constraint_section(
    primary_review: str,
    *,
    pr_context: str = "",
    ollama_base_url: str,
    model: str | None = None,
) -> str:
    """Call Ollama to score risks; return markdown section or empty if disabled.

    When the call fails, returns a short failure notice (so operators see that NC ran).
    """
    if not negative_constraint_enabled():
        return ""

    review = (primary_review or "").strip()
    if len(review) < 40:
        return (
            "\n\n### Negative constraint — risk analysis\n\n"
            "_Skipped: primary review text too short for meaningful adversarial analysis._\n"
        )

    mdl = (model or os.environ.get("OCTO_NEGATIVE_CONSTRAINT_MODEL") or "").strip()
    if not mdl:
        mdl = (os.environ.get("OLLAMA_MODEL") or "llama3.2").strip() or "llama3.2"

    cap = _max_review_chars()
    if len(review) > cap:
        review = review[: cap - 80] + "\n\n… [truncated for negative constraint context]\n"

    ctx = (pr_context or "").strip()
    if len(ctx) > 8000:
        ctx = ctx[:7900] + "\n…"

    user_parts = [
        "## Primary automated review (analyze implied fixes)\n\n",
        review,
    ]
    if ctx:
        user_parts.extend(["\n\n## Extra PR context\n\n", ctx])
    user_body = "".join(user_parts)

    try:
        try:
            from observability.privacy_filter import redact_for_llm

            user_body, priv_map = redact_for_llm(user_body)
        except ImportError:
            priv_map = {}

        sys_prompt = NC_SYSTEM_PROMPT
        degraded = (os.environ.get("OCTO_DEGRADED_TASK_INSTRUCTION") or "").strip()
        if degraded:
            sys_prompt = f"{degraded}\n\n{sys_prompt}"
        raw_text = _ollama_chat(
            ollama_base_url,
            mdl,
            sys_prompt,
            user_body,
            timeout=_timeout_sec(),
        )
        if priv_map:
            try:
                from observability.privacy_filter import unredact_response

                raw_text = unredact_response(raw_text, priv_map)
            except ImportError:
                pass

        parsed = _extract_json_object(raw_text)
        normalized = _normalize_payload(parsed)
        return "\n\n" + format_risk_analysis_markdown(normalized).strip() + "\n"
    except Exception as exc:
        _LOG.warning("negative_constraint: failed: %s", exc)
        return (
            "\n\n### Negative constraint — risk analysis\n\n"
            f"_Unavailable:_ `{type(exc).__name__}: {exc}`\n"
        )
