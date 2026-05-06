"""Python context pruning via ``ast`` — enclosing def/class plus imports and local deps."""

from __future__ import annotations

import ast
from context_pruner.comments import omitted_comment


def _stmt_span(stmt: ast.stmt) -> tuple[int, int]:
    lo = getattr(stmt, "lineno", None) or 1
    hi = getattr(stmt, "end_lineno", None) or lo
    # Slice end exclusive: lines [lo, hi] inclusive -> indices [lo-1 : hi]
    return lo - 1, hi


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge half-open [start, end) line ranges (0-based start, exclusive end)."""
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
    if not merged:
        return omit
    parts: list[str] = []
    pos = 0
    n = len(lines)
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


class _CollectLoads(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        cur: ast.expr | ast.Attribute = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load):
            self.names.add(cur.id)
        self.generic_visit(node)


def _collect_used_names(node: ast.AST) -> set[str]:
    vis = _CollectLoads()
    vis.visit(node)
    return vis.names


def _top_level_defs(mod: ast.Module) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for stmt in mod.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = stmt
    return out


def _import_keeps(stmt: ast.stmt, used: set[str]) -> bool:
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            name = alias.asname or alias.name.split(".")[0]
            if name in used:
                return True
        return False
    if isinstance(stmt, ast.ImportFrom):
        if stmt.names is None:
            return False
        for alias in stmt.names:
            if alias.name == "*":
                return True
            name = alias.asname or alias.name
            if name in used:
                return True
        return False
    return False


def _find_enclosing(mod: ast.Module, line: int) -> ast.AST | None:
    """Innermost function/class whose span contains ``line`` (1-based)."""
    best: ast.AST | None = None
    best_span: int | None = None
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ln = getattr(node, "lineno", None)
            en = getattr(node, "end_lineno", None) or ln
            if ln is None:
                continue
            if ln <= line <= en:
                span = en - ln
                if best_span is None or span < best_span:
                    best = node
                    best_span = span
    return best


def _find_statement_span(mod: ast.Module, line: int) -> tuple[int, int] | None:
    for stmt in mod.body:
        ln = getattr(stmt, "lineno", None)
        en = getattr(stmt, "end_lineno", None) or ln
        if ln is None:
            continue
        if ln <= line <= (en or ln):
            return _stmt_span(stmt)
    return None


def prune_python_source(source: str, line: int) -> tuple[str, str | None]:
    omit = omitted_comment(language="python")
    lines = source.splitlines()
    if line < 1:
        return omit, "invalid_line"

    try:
        mod = ast.parse(source)
    except SyntaxError:
        # Preserve scanner line context without AST.
        lo = max(1, line - 25)
        hi = min(len(lines), line + 25)
        kept = [(lo - 1, hi)]
        return _render(lines, kept, omit), "syntax_error_window"

    if not isinstance(mod, ast.Module):
        return "\n".join(lines), None

    enclosing = _find_enclosing(mod, line)
    if enclosing is None:
        span = _find_statement_span(mod, line)
        if span is None:
            lo = max(1, line - 1)
            hi = min(len(lines), line + 1)
            span = (lo - 1, hi)
        a, b = span
        kept_ranges = [(a, b)]
        text = _render(lines, kept_ranges, omit)
        return text, "no_enclosing_def_class"
    used = _collect_used_names(enclosing)
    kept_ranges = [_stmt_span(enclosing)]  # type: ignore[arg-type]

    defs = _top_level_defs(mod)
    enc_name: str | None = None
    if isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        enc_name = enclosing.name

    for name, stmt in defs.items():
        if enc_name and name == enc_name:
            continue
        if name in used:
            kept_ranges.append(_stmt_span(stmt))

    for stmt in mod.body:
        if _import_keeps(stmt, used):
            kept_ranges.append(_stmt_span(stmt))

    text = _render(lines, kept_ranges, omit)
    return text, None
