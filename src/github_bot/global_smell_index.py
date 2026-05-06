"""Global SQLite index of scanner vulnerability snippets for recurring-architectural-debt detection."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_LOG = logging.getLogger(__name__)

_LOCK = threading.Lock()

_SKIP_ENV = "OCTO_SPORK_SKIP_GLOBAL_SMELL"


def _repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd()


def index_db_path() -> Path:
    override = (os.environ.get("OCTO_GLOBAL_SMELL_DB") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / ".local" / "global_smell_index.db"


def smell_index_enabled() -> bool:
    if os.environ.get(_SKIP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return os.environ.get("OCTO_GLOBAL_SMELL_INDEX", "1").strip().lower() in {"1", "true", "yes", "on"}


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_snippet(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    stripped = [ln.rstrip() for ln in lines]
    while stripped and not stripped[0].strip():
        stripped.pop(0)
    while stripped and not stripped[-1].strip():
        stripped.pop()
    core = "\n".join(stripped)
    return re.sub(r"\s+", " ", core).strip()


def _fallback_snippet_hash(rule_id: str, rel_path: str, line: int, message: str) -> str:
    blob = f"{rule_id}|{rel_path}|{line}|{message[:2000]}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def _snippet_from_sarif_result(
    repo_root: Path,
    result: dict[str, Any],
) -> tuple[str, str, int, int]:
    """Return (hash_hex, rel_path, start_line, end_line)."""
    rule_id = str(result.get("ruleId") or "")
    msg_obj = result.get("message")
    if isinstance(msg_obj, dict):
        message = str(msg_obj.get("text") or "")
    else:
        message = str(msg_obj or "")
    message = message.strip()

    locations = result.get("locations") or []
    loc0 = locations[0] if isinstance(locations, list) and locations else {}
    if not isinstance(loc0, dict):
        loc0 = {}
    phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
    region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
    al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
    uri = str(al.get("uri") or "")
    rel_path = _uri_to_repo_relative(uri) or "(unknown)"
    try:
        start = int(region.get("startLine") or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int(region.get("endLine") or start or 0)
    except (TypeError, ValueError):
        end = start

    snippet_txt = ""
    snip = region.get("snippet") if isinstance(region.get("snippet"), dict) else {}
    if isinstance(snip, dict) and snip.get("text"):
        snippet_txt = str(snip["text"])

    if not snippet_txt and rel_path and rel_path != "(unknown)" and start > 0:
        rp = Path(rel_path.replace("\\", "/").lstrip("/"))
        abs_path = (repo_root / rp).resolve()
        if abs_path.is_file():
            try:
                raw_file = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
                lo = max(1, start)
                hi = max(lo, end if end >= lo else lo + 4)
                hi = min(hi, len(raw_file))
                chunk = raw_file[lo - 1 : hi]
                snippet_txt = "\n".join(chunk)
            except OSError as exc:
                _LOG.debug("global_smell_index: could not read %s: %s", abs_path, exc)

    norm = _normalize_snippet(snippet_txt) if snippet_txt.strip() else ""
    if norm:
        digest = hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()
    else:
        digest = _fallback_snippet_hash(rule_id, rel_path, start, message)
    return digest, rel_path, start, end if end >= start else start


def _sarif_level_relevant(level: str | None) -> bool:
    lv = str(level or "").lower()
    return lv in {"error", "warning"}


def _extract_fix_summary(rule: dict[str, Any] | None) -> tuple[str, str | None]:
    if not rule or not isinstance(rule, dict):
        return "(no remediation text in SARIF rule metadata)", None
    help_uri = rule.get("helpUri")
    uri = str(help_uri).strip() if help_uri else None
    for key in ("fullDescription", "shortDescription", "messageStrings"):
        node = rule.get(key)
        if isinstance(node, dict) and node.get("text"):
            t = str(node["text"]).strip()
            if t:
                return (t[:8000], uri)
        if isinstance(node, str) and node.strip():
            return (node.strip()[:8000], uri)
    props = rule.get("properties")
    if isinstance(props, dict):
        for k in ("problem.severity", "tags"):
            if props.get(k):
                return (json.dumps(props.get(k))[:2000], uri)
    return ("See scanner documentation for this rule.", uri)


def _connect() -> sqlite3.Connection:
    path = index_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS smell_snippets (
            snippet_hash TEXT PRIMARY KEY,
            scanner TEXT NOT NULL,
            rule_id TEXT,
            fix_summary TEXT NOT NULL,
            fix_uri TEXT,
            first_repo TEXT NOT NULL,
            first_pr_url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_repo TEXT,
            last_pr_url TEXT,
            last_seen_at TEXT,
            hit_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_smell_first_repo ON smell_snippets(first_repo);
        """
    )


@dataclass(frozen=True)
class RecurringDebt:
    snippet_hash: str
    scanner: str
    rule_id: str
    file_hint: str
    line_hint: int
    prior_repo: str
    prior_pr_url: str
    fix_summary: str
    fix_uri: str | None


def ingest_sarif_findings(
    scanner_name: str,
    sarif_payload: dict[str, Any],
    repo_root: Path,
    *,
    repo_full_name: str,
    pr_html_url: str,
) -> tuple[str, list[RecurringDebt]]:
    """Persist hashes + fixes; return (markdown_block, recurring_matches).

    *scanner_name* should be ``\"trivy\"`` or ``\"codeql\"``.
    """
    if not smell_index_enabled():
        return "", []

    recurring: list[RecurringDebt] = []

    with _LOCK:
        conn = _connect()
        try:
            init_schema(conn)
            cur = conn.cursor()

            for run in sarif_payload.get("runs") or []:
                if not isinstance(run, dict):
                    continue
                rules_map: dict[str, dict[str, Any]] = {}
                driver = ((run.get("tool") or {}).get("driver")) or {}
                for rule in driver.get("rules") or []:
                    if isinstance(rule, dict) and rule.get("id"):
                        rules_map[str(rule["id"])] = rule

                for result in run.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    if not _sarif_level_relevant(str(result.get("level"))):
                        continue

                    rule_id = str(result.get("ruleId") or "")
                    rule_meta = rules_map.get(rule_id) or {}
                    fix_summary, fix_uri = _extract_fix_summary(rule_meta)

                    try:
                        h, rel_path, start_ln, _end = _snippet_from_sarif_result(repo_root, result)
                    except Exception as exc:
                        _LOG.debug("global_smell_index: skip result: %s", exc)
                        continue

                    row = cur.execute(
                        "SELECT * FROM smell_snippets WHERE snippet_hash = ?",
                        (h,),
                    ).fetchone()

                    if row is None:
                        try:
                            cur.execute(
                                """
                                INSERT INTO smell_snippets (
                                    snippet_hash, scanner, rule_id, fix_summary, fix_uri,
                                    first_repo, first_pr_url, first_seen_at,
                                    last_repo, last_pr_url, last_seen_at, hit_count
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                                """,
                                (
                                    h,
                                    scanner_name,
                                    rule_id[:512],
                                    fix_summary[:12000],
                                    fix_uri,
                                    repo_full_name[:512],
                                    pr_html_url[:2000],
                                    _now_iso(),
                                    repo_full_name[:512],
                                    pr_html_url[:2000],
                                    _now_iso(),
                                ),
                            )
                        except sqlite3.IntegrityError:
                            row = cur.execute(
                                "SELECT * FROM smell_snippets WHERE snippet_hash = ?",
                                (h,),
                            ).fetchone()

                    if row is not None:
                        prior_repo = str(row["first_repo"] or "")
                        prior_pr = str(row["first_pr_url"] or "")
                        same_pr = prior_pr == pr_html_url
                        cur.execute(
                            """
                            UPDATE smell_snippets SET
                                last_repo = ?, last_pr_url = ?, last_seen_at = ?, hit_count = hit_count + 1
                            WHERE snippet_hash = ?
                            """,
                            (
                                repo_full_name[:512],
                                pr_html_url[:2000],
                                _now_iso(),
                                h,
                            ),
                        )
                        if not same_pr:
                            recurring.append(
                                RecurringDebt(
                                    snippet_hash=h,
                                    scanner=scanner_name,
                                    rule_id=rule_id[:256],
                                    file_hint=rel_path[:512],
                                    line_hint=start_ln,
                                    prior_repo=prior_repo or "?",
                                    prior_pr_url=prior_pr or "",
                                    fix_summary=str(row["fix_summary"] or "")[:4000],
                                    fix_uri=str(row["fix_uri"]) if row["fix_uri"] else None,
                                )
                            )

            conn.commit()
        finally:
            conn.close()

    md = format_recurring_section(recurring)
    return md, recurring


def format_recurring_section(recurring: list[RecurringDebt]) -> str:
    if not recurring:
        return ""
    lines = [
        "### Recurring architectural debt (global smell index)",
        "",
        "_The same **code pattern hash** was recorded on an earlier PR after Trivy/CodeQL flagged it; "
        "treat as systemic drift unless proven unrelated._",
        "",
    ]
    seen_keys: set[tuple[str, str]] = set()
    for r in recurring:
        key = (r.snippet_hash, r.prior_pr_url)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        link = f"[prior PR / recorded fix context]({r.prior_pr_url})" if r.prior_pr_url else "_no PR link stored_"
        fu = f" · [guidance]({r.fix_uri})" if r.fix_uri else ""
        fx = r.fix_summary.replace("\n", " ").strip()
        if len(fx) > 420:
            fx = fx[:417] + "…"
        short_h = r.snippet_hash[:16] if len(r.snippet_hash) >= 16 else r.snippet_hash
        lines.append(
            f"- **Hash `{short_h}…`** ({r.scanner} · `{r.rule_id}`) at `{r.file_hint}`:{r.line_hint} "
            f"— previously seen in **`{r.prior_repo}`** ({link}){fu}. "
            f"_Recorded remediation excerpt:_ {fx}"
        )
    lines.append("")
    return "\n".join(lines)


def merge_smell_sections(*sections: str) -> str:
    parts = [s.strip() for s in sections if s and str(s).strip()]
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"
