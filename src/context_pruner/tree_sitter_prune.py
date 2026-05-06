"""Context pruning for JS/TS via tree-sitter (other languages use the same pipeline)."""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from context_pruner.comments import omitted_comment
from repo_graph.parsers import javascript_parser, tsx_parser, typescript_parser


SUPPORTED_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})

_JS_KEYWORDS = frozenset(
    """
    break case catch class const continue debugger default delete do else export extends
    finally for function if import in instanceof new return super switch this throw try
    typeof var void while with yield let static enum implements interface package private
    protected public abstract boolean byte char double float goto int long short synchronized
    throws transient volatile await null true false undefined as from of get set async
    readonly declare namespace type keyof infer satisfies using
    """.split()
)

_ENCLOSING_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "abstract_class_declaration",
    }
)


def _parser_for(path: Path):
    suf = path.suffix.lower()
    if suf == ".tsx":
        return tsx_parser()
    if suf == ".ts":
        return typescript_parser()
    return javascript_parser()


def _line_spans(node: Node, line_1based: int) -> bool:
    row = line_1based - 1
    return node.start_point[0] <= row <= node.end_point[0]


def _span_size(node: Node) -> int:
    return node.end_byte - node.start_byte


def _find_enclosing(root: Node, line_1based: int) -> Node | None:
    best: Node | None = None
    best_sz: int | None = None

    def visit(n: Node) -> None:
        nonlocal best, best_sz
        if n.type in _ENCLOSING_TYPES and _line_spans(n, line_1based):
            sz = _span_size(n)
            if best is None or sz < best_sz:  # type: ignore[operator]
                best = n
                best_sz = sz
        for c in n.children:
            visit(c)

    visit(root)
    return best


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    s = sorted(ranges)
    out: list[list[int]] = [[s[0][0], s[0][1]]]
    for a, b in s[1:]:
        la, lb = out[-1]
        if a <= lb:
            out[-1][1] = max(lb, b)
        else:
            out.append([a, b])
    return [(int(x[0]), int(x[1])) for x in out]


def _render(lines: list[str], kept: list[tuple[int, int]], omit: str) -> str:
    merged = _merge_ranges(kept)
    n = len(lines)
    if not merged:
        return omit
    parts: list[str] = []
    pos = 0
    for a, b in merged:
        a = max(0, min(a, n))
        b = max(a, min(b, n))
        if pos < a:
            parts.append(omit)
        parts.extend(lines[a:b])
        pos = b
    if pos < n:
        parts.append(omit)
    return "\n".join(parts)


def _collect_identifiers(root: Node, source: bytes) -> set[str]:
    found: set[str] = set()

    def visit(n: Node) -> None:
        if n.type == "identifier":
            txt = source[n.start_byte : n.end_byte].decode("utf-8", errors="replace")
            if txt and txt not in _JS_KEYWORDS:
                found.add(txt)
        for c in n.children:
            visit(c)

    visit(root)
    return found


def _stmt_span_from_node(node: Node) -> tuple[int, int]:
    start_line = node.start_point[0]
    end_line = node.end_point[0]
    # Half-open [start, end) line indices for ``lines`` slice
    return start_line, end_line + 1


def _top_level_decls(program: Node, source: bytes) -> list[tuple[str, Node]]:
    """Return ``(name, node_for_line_span)`` for top-level callables / classes."""
    out: list[tuple[str, Node]] = []
    for ch in program.children:
        if ch.type in ("function_declaration", "class_declaration", "abstract_class_declaration"):
            n = _declaration_name(ch, source)
            if n:
                out.append((n, ch))
        elif ch.type == "lexical_declaration":
            for sub in ch.named_children:
                if sub.type != "variable_declarator":
                    continue
                val = sub.child_by_field_name("value")
                if val is not None and val.type in ("arrow_function", "function_expression"):
                    n = _declaration_name(sub, source)
                    if n:
                        out.append((n, ch))
    return out


def _declaration_name(node: Node, source: bytes) -> str | None:
    if node.type == "variable_declarator":
        name = node.child_by_field_name("name")
        if name and name.type == "identifier":
            return source[name.start_byte : name.end_byte].decode("utf-8", errors="replace")
        return None
    name = node.child_by_field_name("name")
    if name is None or name.type != "identifier":
        return None
    return source[name.start_byte : name.end_byte].decode("utf-8", errors="replace")


def _rough_import_names(text: str) -> set[str]:
    """Best-effort ES module binding extraction for tree-sitter ``import`` / ``export`` nodes."""
    out: set[str] = set()
    t = text.strip()
    # import X from
    for m in re.finditer(r"\bimport\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\s", t):
        out.add(m.group(1))
    # import { a, b as c }
    brace = re.search(r"\bimport\s+(?:type\s+)?\{([^}]*)\}\s*from\s", t)
    if brace:
        for part in brace.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part:
                out.add(part.split(" as ")[-1].strip())
            else:
                out.add(part.split()[0])
    m = re.search(r"\bimport\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s+from\s", t)
    if m:
        out.add(m.group(1))
    # export { a as b } from — reuse brace heuristic
    exp = re.search(r"\bexport\s+\{([^}]*)\}\s*from\s", t)
    if exp:
        for part in exp.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part:
                out.add(part.split(" as ")[-1].strip())
            else:
                out.add(part.split()[0])
    return out


def _program_root(tree: Tree) -> Node | None:
    root = tree.root_node
    if root.type == "program":
        return root
    return root


def prune_with_tree_sitter(path: Path, source: str, line: int) -> tuple[str, str | None]:
    omit = omitted_comment(path=path)
    lines = source.splitlines()
    if line < 1:
        return omit, "invalid_line"
    if not lines:
        return "", "empty"

    parser = _parser_for(path)
    src_bytes = source.encode("utf-8")
    tree = parser.parse(src_bytes)
    prog = _program_root(tree)
    if prog is None:
        lo = max(0, line - 15)
        hi = min(len(lines), line + 15)
        return _render(lines, [(lo, hi)], omit), "no_program"

    enc = _find_enclosing(prog, line)
    if enc is None:
        lo = max(0, line - 2)
        hi = min(len(lines), line + 1)
        return _render(lines, [(lo, hi)], omit), "no_enclosing_def_class"

    used = _collect_identifiers(enc, src_bytes)
    kept = [_stmt_span_from_node(enc)]
    enc_name = _declaration_name(enc, src_bytes) if enc.type != "method_definition" else None

    for decl, span_node in _top_level_decls(prog, src_bytes):
        if enc_name and decl == enc_name:
            continue
        if decl in used:
            kept.append(_stmt_span_from_node(span_node))

    for ch in prog.children:
        if ch.type not in ("import_statement", "export_statement"):
            continue
        frag = src_bytes[ch.start_byte : ch.end_byte].decode("utf-8", errors="replace")
        binds = _rough_import_names(frag)
        if binds & used:
            kept.append(_stmt_span_from_node(ch))

    text = _render(lines, kept, omit)
    return text, None
