"""Export grounded-review markdown into structured JSON for Jira Cloud / Linear issue creation.

Parses **High** severity bullets from typical review markdown and enriches tickets with
scanner receipts from an optional review ``snapshot`` dict (same shape as ``grounded_review``).

This module does not call remote APIs; it writes a portable JSON document your automation can POST.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HIGH_SECTION_RE = re.compile(
    r"(?ms)^#{1,4}\s*(?:\d+\.\)\s*)?High\b[^\n]*\n(?P<section>.+?)(?=^#{1,4}\s*(?:Critical|Medium|Low)\b|^#{1,3}\s*\d+\)\s|^#{1,4}\s*(?:Hardening|QA strategy|Top\s*\d)|\Z)",
)
HARDENING_SECTION_RE = re.compile(
    r"(?ms)^#{1,4}\s*(?:\d+\.\)\s*)?(?:Hardening|Short-term)[^\n]*\n(?P<section>.+?)(?=^#{1,4}\s|\Z)",
)
PATH_IN_BACKTICKS_RE = re.compile(r"`([^`]+\.(?:py|ts|tsx|js|jsx|go|rb|java|rs|yaml|yml|toml|json|md))`")
SOURCE_URI_RE = re.compile(r"source://\[([^\]]+)\]\(([^)]+)\)")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_high_section(markdown: str) -> str | None:
    """Return markdown body under a ``High`` severity heading."""
    m = HIGH_SECTION_RE.search(markdown)
    if not m:
        # Fallback: bold **High** line
        alt = re.search(
            r"(?ms)^\*\*High\*\*\s*\n(?P<section>.+?)(?=^\*\*(?:Critical|Medium|Low)\*\*|^#{1,4}\s|\Z)",
            markdown,
        )
        return alt.group("section").strip() if alt else None
    return m.group("section").strip()


def split_high_findings(section: str) -> list[str]:
    """Split High section into bullet/numbered finding blocks."""
    if not section.strip():
        return []
    blocks: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        is_new = bool(
            re.match(r"^[-*•]\s+", stripped)
            or re.match(r"^\d+\.\s+", stripped)
            or re.match(r"^\(\s*\d+\s*\)\s+", stripped)
        )
        if is_new:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def extract_title_from_finding(block: str) -> str:
    first = block.strip().split("\n")[0]
    first = re.sub(r"^[-*•]\s+", "", first)
    first = re.sub(r"^\d+\.\s+", "", first)
    first = re.sub(r"^\(\s*\d+\s*\)\s+", "", first)
    bold = re.search(r"\*\*([^*]+)\*\*", first)
    if bold:
        return bold.group(1).strip()[:240]
    plain = re.sub(r"\*+", "", first).strip()
    return plain[:240] if plain else "High-severity finding"


def extract_paths_from_text(text: str) -> list[str]:
    paths: list[str] = []
    for m in PATH_IN_BACKTICKS_RE.finditer(text):
        paths.append(m.group(1))
    for m in SOURCE_URI_RE.finditer(text):
        label = m.group(1)
        if "/" in label or "." in label:
            paths.append(label.split("#", 1)[0].strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:80]


def extract_hardening_bullets(markdown: str, *, limit: int = 12) -> list[str]:
    m = HARDENING_SECTION_RE.search(markdown)
    if not m:
        return []
    body = m.group("section")
    bullets: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        m2 = re.match(r"^[-*•]\s+(.+)", s)
        if m2:
            bullets.append(m2.group(1).strip()[:500])
        else:
            m3 = re.match(r"^\d+\.\s+(.+)", s)
            if m3:
                bullets.append(m3.group(1).strip()[:500])
        if len(bullets) >= limit:
            break
    return bullets


def gather_receipts_from_snapshot(snapshot: dict[str, Any] | None) -> list[dict[str, str]]:
    """Structured evidence blocks attached to tickets."""
    if not snapshot:
        return []
    receipts: list[dict[str, str]] = []
    pairs = (
        ("trivy_security", str(snapshot.get("security_context_block") or "")),
        ("codeql_sarif", str(snapshot.get("codeql_evidence_block") or "")),
        ("dependency_audit", str(snapshot.get("dependency_audit_block") or "")),
        ("architecture_map", str(snapshot.get("architecture_map_block") or "")),
    )
    for kind, body in pairs:
        body = body.strip()
        if body:
            receipts.append(
                {
                    "type": kind,
                    "markdown": body[:24_000] + ("…" if len(body) > 24_000 else ""),
                }
            )
    src = snapshot.get("sources")
    if isinstance(src, list) and src:
        receipts.append(
            {
                "type": "review_sources_list",
                "markdown": "Sampled/reviewed paths:\n" + "\n".join(f"- `{p}`" for p in src[:60]),
            }
        )
    cov = snapshot.get("coverage")
    if isinstance(cov, dict) and cov:
        receipts.append(
            {
                "type": "coverage_metadata",
                "markdown": "```json\n"
                + json.dumps(cov, indent=2)[:8000]
                + ("\n…" if len(json.dumps(cov)) > 8000 else "")
                + "\n```",
            }
        )
    return receipts


def default_remediation_steps(hardening: list[str]) -> list[str]:
    base = [
        "Reproduce in a branch; add or extend automated tests around the affected flows.",
        "Ship a minimal fix; re-run static scans (Trivy / pip-audit / npm audit) on the change.",
        "Document residual risk, rollout plan, and any monitoring or alerts.",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for item in (hardening[:6] if hardening else []) + base:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:12]


def build_ticket_record(
    finding_md: str,
    *,
    index: int,
    snapshot: dict[str, Any] | None,
    receipts_global: list[dict[str, str]],
    hardening_bullets: list[str],
) -> dict[str, Any]:
    title = extract_title_from_finding(finding_md)
    paths = extract_paths_from_text(finding_md)
    # De-dupe paths preserving order
    seen: set[str] = set()
    upaths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            upaths.append(p)

    desc_parts = [
        "## Finding (High)",
        "",
        finding_md.strip(),
        "",
        "### Evidence paths",
        "",
    ]
    if upaths:
        desc_parts.extend(f"- `{p}`" for p in upaths[:40])
    else:
        desc_parts.append("_No explicit file paths parsed from this finding; see receipts._")

    description_md = "\n".join(desc_parts)

    remediation = default_remediation_steps(hardening_bullets)

    repo_meta = {}
    if snapshot:
        repo_meta = {
            "owner": snapshot.get("owner"),
            "repo": snapshot.get("repo"),
            "default_branch": snapshot.get("default_branch"),
            "scan_root": snapshot.get("scan_root"),
        }

    ticket_id = f"high-{index + 1:03d}"

    jira_summary = title[:254]
    jira_description_lines = [
        "*Severity:* High (from grounded review export)",
        "",
        f"*Repository:* {repo_meta.get('owner')}/{repo_meta.get('repo')}",
        "",
        "*Finding:*",
        "{code}",
        finding_md.strip(),
        "{code}",
        "",
        "*Files/paths:*",
        ("\n".join(f"- {p}" for p in upaths[:25]) or "- (see description markdown in companion JSON)"),
        "",
        "*Suggested remediation:*",
        *[f"- {s}" for s in remediation[:8]],
        "",
        "*Receipts:* see structured `receipts` array in companion JSON export.",
    ]
    jira_description = "\n".join(jira_description_lines)

    linear_description = description_md + "\n\n### Suggested remediation\n\n" + "\n".join(
        f"- {s}" for s in remediation
    )

    return {
        "id": ticket_id,
        "severity": "high",
        "title": title,
        "description_markdown": description_md,
        "file_paths": upaths,
        "receipts": list(receipts_global),
        "suggested_remediation": remediation,
        "repository": repo_meta,
        "integrations": {
            "jira_cloud": {
                "issue_fields_template": {
                    "summary": jira_summary,
                    "description_plain": jira_description[:32_000],
                    "labels": ["security", "octo-spork-review", "high"],
                    "priority_name_suggestion": "High",
                    "issuetype_name_suggestion": "Bug",
                },
                "notes": "Map fields to your project via REST POST /rest/api/3/issue; description often uses Atlassian Document Format (ADF) — convert markdown externally if required.",
            },
            "linear": {
                "issue_create_template": {
                    "title": title[:255],
                    "description": linear_description[:50_000],
                    "priority": 2,
                    "labelSuggestions": ["security", "octo-spork"],
                },
                "notes": "Use GraphQL `issueCreate`; supply teamId / projectId from your workspace. Priority 2 ≈ High in Linear.",
            },
        },
    }


def build_export_document(
    review_markdown: str,
    snapshot: dict[str, Any] | None,
    *,
    query: str | None = None,
) -> dict[str, Any]:
    """Build the top-level export structure."""
    high_section = extract_high_section(review_markdown)
    findings_raw = split_high_findings(high_section or "")
    hardening = extract_hardening_bullets(review_markdown)
    receipts = gather_receipts_from_snapshot(snapshot)

    tickets = []
    for i, block in enumerate(findings_raw):
        tickets.append(
            build_ticket_record(
                block,
                index=i,
                snapshot=snapshot,
                receipts_global=receipts,
                hardening_bullets=hardening,
            )
        )

    cov = (snapshot or {}).get("coverage") if snapshot else {}
    revision = ""
    if isinstance(cov, dict):
        revision = str(cov.get("revision_sha") or (snapshot or {}).get("revision_sha") or "")

    return {
        "format_version": 1,
        "producer": "octo-spork-review-ticket-export",
        "generated_at": _utc_now_iso(),
        "review_query": query or "",
        "repository": {
            "owner": (snapshot or {}).get("owner"),
            "repo": (snapshot or {}).get("repo"),
            "scan_root": (snapshot or {}).get("scan_root"),
            "revision_sha": revision[:40] if revision else None,
        },
        "ticket_target": "high_severity_findings",
        "count": len(tickets),
        "tickets": tickets,
        "meta": {
            "high_section_found": bool(high_section),
            "parsed_findings": len(findings_raw),
            "receipt_blocks": len(receipts),
        },
    }


def export_review_tickets_json(
    review_markdown: str,
    snapshot: dict[str, Any] | None,
    output_path: Path,
    *,
    query: str | None = None,
    indent: int = 2,
) -> Path:
    """Write JSON export; returns output path."""
    doc = build_export_document(review_markdown, snapshot, query=query)
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(doc, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    return output_path


def load_snapshot_from_json(path: Path) -> dict[str, Any]:
    raw = Path(path).expanduser().resolve().read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("snapshot JSON must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Export High-severity review findings to Jira/Linear-oriented JSON")
    p.add_argument("--review-file", help="Path to markdown file containing grounded review output")
    p.add_argument("--snapshot-json", help="Optional JSON file with review snapshot metadata/receipts")
    p.add_argument("-o", "--output", required=True, help="Output JSON path")
    p.add_argument("--query", default="", help="Original review query string (metadata only)")
    args = p.parse_args(argv)

    md = ""
    if args.review_file:
        md = Path(args.review_file).expanduser().resolve().read_text(encoding="utf-8")
    else:
        md = sys.stdin.read()

    snap = None
    if args.snapshot_json:
        snap = load_snapshot_from_json(Path(args.snapshot_json))

    export_review_tickets_json(md, snap, Path(args.output), query=args.query or None)
    print(Path(args.output).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
