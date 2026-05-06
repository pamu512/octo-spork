"""Static import-based dependency sketch for grounded review (Mermaid flowchart).

- **Python:** ``ast`` analysis with relative/absolute import resolution against ``scan_root``.
- **TypeScript/JavaScript:** lexical import/require extraction (grep-style); aligns with common
  IDE fast-path graphs. Optional **ts-morph** bridge: set ``GROUNDED_REVIEW_ARCH_MAP_TS_MORPH=1``
  and run ``npm install`` in ``overlays/agenticseek/scripts/arch_ts`` once to enrich TS edges.

This is a heuristic architecture map, not a full type-aware program graph.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".next",
        ".tox",
        "coverage",
        "htmlcov",
    }
)

TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

TS_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""import\s+[\w*{}\s,$\n]+\s+from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""import\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
)

GROUNDED_REVIEW_ARCH_MAP_ENABLED = os.getenv("GROUNDED_REVIEW_ARCH_MAP_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_ARCH_MAP_MAX_FILES = int(os.getenv("GROUNDED_REVIEW_ARCH_MAP_MAX_FILES", "500"))
GROUNDED_REVIEW_ARCH_MAP_MAX_EDGES = int(os.getenv("GROUNDED_REVIEW_ARCH_MAP_MAX_EDGES", "220"))
GROUNDED_REVIEW_ARCH_MAP_TS_MORPH = os.getenv("GROUNDED_REVIEW_ARCH_MAP_TS_MORPH", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


def discover_source_files(scan_root: Path, *, max_files: int) -> tuple[list[Path], list[Path]]:
    """Return (python_files, ts_js_files) bounded by max_files total."""
    py_files: list[Path] = []
    ts_files: list[Path] = []
    rg = shutil.which("rg")
    if rg:
        try:
            completed = subprocess.run(
                [
                    rg,
                    "--files",
                    "-g",
                    "*.py",
                    "-g",
                    "*.ts",
                    "-g",
                    "*.tsx",
                    "-g",
                    "*.js",
                    "-g",
                    "*.jsx",
                    "-g",
                    "*.mjs",
                    str(scan_root),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                paths: list[Path] = []
                for line in completed.stdout.splitlines():
                    p = Path(line.strip())
                    if p.is_file():
                        paths.append(p)
                paths.sort(key=lambda x: str(x))
                for p in paths[: max(1, max_files)]:
                    if any(part in SKIP_DIR_NAMES for part in p.parts):
                        continue
                    suf = p.suffix.lower()
                    if suf == ".py":
                        py_files.append(p)
                    elif suf in TS_JS_EXTENSIONS:
                        ts_files.append(p)
                return py_files, ts_files
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
        dirnames[:] = [d for d in sorted(dirnames) if not _should_skip_dir(d)]
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            suf = p.suffix.lower()
            if suf == ".py" or suf in TS_JS_EXTENSIONS:
                collected.append(p)
            if len(collected) >= max_files:
                break
        if len(collected) >= max_files:
            break
    collected.sort(key=lambda x: str(x))
    for p in collected:
        suf = p.suffix.lower()
        if suf == ".py":
            py_files.append(p)
        elif suf in TS_JS_EXTENSIONS:
            ts_files.append(p)
    return py_files, ts_files


def _node_id(path_like: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "_", path_like)[:48].strip("_") or "root"
    h = hashlib.sha1(path_like.encode("utf-8")).hexdigest()[:8]
    return f"n_{safe}_{h}"


def _display_label(path_like: str, *, max_len: int = 56) -> str:
    s = path_like.replace('"', "'")
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def file_to_node(scan_root: Path, file_path: Path) -> str:
    rel = file_path.resolve().relative_to(scan_root.resolve())
    if rel.name == "__init__.py":
        return rel.parent.as_posix() or "."
    return rel.with_suffix("").as_posix()


def resolve_absolute_py_module(mod: str, scan_root: Path) -> str | None:
    parts = mod.split(".")
    root = scan_root.resolve()
    for k in range(len(parts), 0, -1):
        sub = parts[:k]
        p = root.joinpath(*sub)
        py_file = p.with_suffix(".py")
        if py_file.is_file():
            return py_file.relative_to(root).with_suffix("").as_posix()
        init_f = p / "__init__.py"
        if init_f.is_file():
            return p.relative_to(root).as_posix()
    return None


def resolve_relative_py_import(from_file: Path, scan_root: Path, level: int, module: str | None) -> str | None:
    """Resolve ImportFrom with relative level (best-effort)."""
    root = scan_root.resolve()
    cur = from_file.parent
    if from_file.name != "__init__.py":
        anchor = cur
    else:
        anchor = cur
    base = anchor
    for _ in range(level):
        base = base.parent
    if module:
        parts = module.split(".")
        target = base.joinpath(*parts)
        py_file = target.with_suffix(".py")
        if py_file.is_file():
            return py_file.relative_to(root).with_suffix("").as_posix()
        init_f = target / "__init__.py"
        if init_f.is_file():
            return target.relative_to(root).as_posix()
        if target.is_dir():
            init2 = target / "__init__.py"
            if init2.is_file():
                return target.relative_to(root).as_posix()
    return None


def extract_python_edges(from_file: Path, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    try:
        text = from_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return edges
    try:
        tree = ast.parse(text, filename=str(from_file))
    except SyntaxError:
        return edges

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tgt = resolve_absolute_py_module(alias.name, scan_root)
                if tgt:
                    edges.add((src_id, tgt))
        elif isinstance(node, ast.ImportFrom):
            level = int(node.level or 0)
            mod = node.module
            if level == 0 and mod:
                tgt = resolve_absolute_py_module(mod, scan_root)
                if tgt:
                    edges.add((src_id, tgt))
            elif level > 0:
                tgt = resolve_relative_py_import(from_file, scan_root, level, mod)
                if tgt:
                    edges.add((src_id, tgt))
            elif mod:
                tgt = resolve_absolute_py_module(mod, scan_root)
                if tgt:
                    edges.add((src_id, tgt))
    return edges


def resolve_ts_import(spec: str, from_file: Path, scan_root: Path) -> str | None:
    spec = spec.strip()
    if not spec or spec.startswith("@types/"):
        return None
    if spec.startswith(("http://", "https://", "node:")):
        return None
    root = scan_root.resolve()
    cur_dir = from_file.parent.resolve()

    if spec.startswith("."):
        cand = (cur_dir / spec).resolve()
        for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs"):
            p = cand.with_suffix(ext) if ext else cand
            if p.is_file():
                try:
                    return p.relative_to(root).with_suffix("").as_posix()
                except ValueError:
                    return None
            if (cand / "index.ts").is_file():
                try:
                    return cand.relative_to(root).as_posix()
                except ValueError:
                    return None
        return None

    cand = root.joinpath(*spec.split("/"))
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        if cand.with_suffix(ext).is_file():
            try:
                return cand.with_suffix(ext).relative_to(root).with_suffix("").as_posix()
            except ValueError:
                return None
    if (cand / "index.ts").is_file():
        try:
            return cand.relative_to(root).as_posix()
        except ValueError:
            return None
    return None


def extract_ts_js_edges(from_file: Path, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    try:
        text = from_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return edges
    seen_specs: set[str] = set()
    for pat in TS_IMPORT_PATTERNS:
        for m in pat.finditer(text):
            for g in m.groups():
                if g:
                    seen_specs.add(g)
    for spec in seen_specs:
        tgt = resolve_ts_import(spec, from_file, scan_root)
        if tgt:
            edges.add((src_id, tgt))
    return edges


def _scripts_ts_bridge_root() -> Path:
    return Path(__file__).resolve().parent / "scripts" / "arch_ts"


def extract_ts_morph_edges(scan_root: Path, py_ts_files: list[Path]) -> set[tuple[str, str]]:
    """Optional Node/ts-morph enrichment (requires npm install under scripts/arch_ts)."""
    if not GROUNDED_REVIEW_ARCH_MAP_TS_MORPH:
        return set()
    bridge = _scripts_ts_bridge_root()
    script = bridge / "run.cjs"
    if not script.is_file():
        return set()
    nm = bridge / "node_modules"
    if not nm.is_dir():
        return set()
    node = shutil.which("node")
    if not node:
        return set()
    payload = json.dumps([str(p.resolve()) for p in py_ts_files[:GROUNDED_REVIEW_ARCH_MAP_MAX_FILES]])
    try:
        completed = subprocess.run(
            [node, str(script), str(scan_root.resolve()), payload],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(bridge),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return set()
    raw = (completed.stdout or "").strip()
    if not raw:
        return set()
    edges: set[tuple[str, str]] = set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    for item in data:
        if isinstance(item, dict):
            a = item.get("from")
            b = item.get("to")
            if isinstance(a, str) and isinstance(b, str) and a and b:
                edges.add((a, b))
    return edges


def build_dependency_edges(scan_root: Path) -> set[tuple[str, str]]:
    scan_root = scan_root.expanduser().resolve()
    if not scan_root.is_dir():
        return set()
    max_f = max(50, GROUNDED_REVIEW_ARCH_MAP_MAX_FILES)
    py_files, ts_files = discover_source_files(scan_root, max_files=max_f)
    edges: set[tuple[str, str]] = set()

    for pf in py_files:
        try:
            sid = file_to_node(scan_root, pf)
        except ValueError:
            continue
        edges |= extract_python_edges(pf, scan_root, sid)

    for tf in ts_files:
        try:
            sid = file_to_node(scan_root, tf)
        except ValueError:
            continue
        edges |= extract_ts_js_edges(tf, scan_root, sid)

    edges |= extract_ts_morph_edges(scan_root, ts_files)

    root_s = scan_root.resolve()

    def is_internal_target(node: str) -> bool:
        if not node or node.startswith(".."):
            return False
        p = root_s.joinpath(*node.split("/"))
        if p.is_file():
            return True
        if (p / "__init__.py").is_file():
            return True
        for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if p.with_suffix(ext).is_file():
                return True
        if (p / "index.ts").is_file() or (p / "index.tsx").is_file():
            return True
        if (p / "index.js").is_file():
            return True
        return False

    internal: set[tuple[str, str]] = set()
    for a, b in edges:
        if a == b:
            continue
        if not is_internal_target(b):
            continue
        internal.add((a, b))
    return internal


def edges_to_mermaid_flowchart(edges: set[tuple[str, str]]) -> str:
    max_e = max(20, GROUNDED_REVIEW_ARCH_MAP_MAX_EDGES)
    items = sorted(edges)
    truncated = False
    if len(items) > max_e:
        items = items[:max_e]
        truncated = True

    nodes: set[str] = set()
    for a, b in items:
        nodes.add(a)
        nodes.add(b)

    lines = [
        "```mermaid",
        "flowchart LR",
    ]
    for n in sorted(nodes):
        mid = _node_id(n)
        lab = _display_label(n)
        lines.append(f'  {mid}["{lab}"]')

    for a, b in items:
        lines.append(f"  {_node_id(a)} --> {_node_id(b)}")

    lines.append("```")
    if truncated:
        lines.append("")
        lines.append(f"_Edges truncated to {max_e} for prompt size; tune via GROUNDED_REVIEW_ARCH_MAP_MAX_EDGES._")
    return "\n".join(lines)


def format_architecture_map_block(scan_root: Path, edges: set[tuple[str, str]]) -> str:
    if not edges:
        return (
            "### Architecture map (import dependency sketch)\n\n"
            "_No internal import edges were resolved (empty repo slice, only externals, or parse limits)._ "
            "This is not proof of a clean architecture.\n\n"
        )

    diagram = edges_to_mermaid_flowchart(edges)
    return (
        "### Architecture map (import dependency sketch)\n\n"
        "Heuristic directed graph from **Python `ast` imports** and **TS/JS lexical imports** "
        "(optional ts-morph bridge if enabled). External packages are omitted.\n\n"
        f"{diagram}\n\n"
        "Use this map as **supporting context** for coupling/tangle discussions; it may miss dynamic imports.\n\n"
    )


def attach_architecture_map(snapshot: dict[str, Any]) -> None:
    snapshot["architecture_map_block"] = ""
    snapshot["architecture_map_edges"] = []
    if not GROUNDED_REVIEW_ARCH_MAP_ENABLED:
        snapshot["architecture_map_block"] = (
            "### Architecture map\n\nSkipped: `GROUNDED_REVIEW_ARCH_MAP_ENABLED` is false.\n\n"
        )
        return
    raw = snapshot.get("scan_root")
    if not raw:
        return
    scan_root = Path(str(raw)).expanduser().resolve()
    if not scan_root.is_dir():
        return
    try:
        edges = build_dependency_edges(scan_root)
        snapshot["architecture_map_edges"] = [{"from": a, "to": b} for a, b in sorted(edges)]
        snapshot["architecture_map_block"] = format_architecture_map_block(scan_root, edges)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        snapshot["architecture_map_block"] = (
            "### Architecture map\n\n"
            f"Could not build dependency sketch: `{exc}`\n\n"
        )


def main(argv: list[str] | None = None) -> int:
    """CLI for debugging: python architecture_map.py /path/to/repo"""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: architecture_map.py <scan_root>", file=sys.stderr)
        return 1
    root = Path(args[0]).expanduser().resolve()
    edges = build_dependency_edges(root)
    print(edges_to_mermaid_flowchart(edges))
    print(len(edges), "edges", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
