"""AST-scoped Python context extraction and generic line-window fallback."""

from __future__ import annotations

import ast
from pathlib import Path

_GENERIC_OMITTED_PREFIX = "// ... omitted ...\n"
_GENERIC_OMITTED_SUFFIX = "\n// ... omitted ...."


def _line_contained_in_def_or_class(node: ast.AST, target_line: int) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    start = node.lineno
    end = getattr(node, "end_lineno", None)
    if end is None:
        # Without ``end_lineno`` (very old interpreters), treat the definition header line only.
        return target_line == start
    return start <= target_line <= end


def extract_python_context(filepath: str, target_line: int) -> str:
    """Return source for the innermost ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` covering ``target_line``.

    ``target_line`` is **1-based**, matching editor / SARIF conventions. The file at ``filepath`` is
    parsed with :func:`ast.parse`; every function and class node whose line span contains
    ``target_line`` is collected, and the **innermost** definition (largest starting line number among
    those enclosing nodes) is rendered with :func:`ast.unparse`.

    If ``target_line`` lies outside any function or class body (for example pure module-level
    statements), returns an empty string—never the whole module.

    Parameters
    ----------
    filepath
        UTF-8 text file containing Python source.
    target_line
        1-based line index into that file.
    """
    if target_line < 1:
        return ""

    path = Path(filepath)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    enclosing: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for node in ast.walk(tree):
        if _line_contained_in_def_or_class(node, target_line):
            enclosing.append(node)

    if not enclosing:
        return ""

    innermost = max(enclosing, key=lambda n: n.lineno)
    return ast.unparse(innermost)


def extract_generic_context(filepath: str, target_line: int) -> str:
    """Return a bounded window of lines around ``target_line`` for non-Python artifacts.

    Reads the file as UTF-8 (with replacement on decode errors), splits into physical lines, and
    returns lines whose **1-based** indices fall in
    ``[max(1, target_line - 20), min(n, target_line + 20)]`` inclusive—equivalent to slicing the
    line list with indices ``[target_line - 21 : target_line + 20]`` after clamping ``target_line``
    into ``[1, n]`` when the file has ``n`` lines.

    Prepends ``// ... omitted ...`` and a newline, appends a newline and ``// ... omitted ....``, as specified.
    Empty files yield only those markers. Bounds are clamped so short files never raise
    :exc:`IndexError`.
    """
    path = Path(filepath)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    n = len(lines)
    if n == 0:
        return _GENERIC_OMITTED_PREFIX + _GENERIC_OMITTED_SUFFIX

    anchor = target_line
    if anchor < 1:
        anchor = 1
    elif anchor > n:
        anchor = n

    first_line = max(1, anchor - 20)
    last_line = min(n, anchor + 20)

    start_idx = first_line - 1
    end_idx = last_line
    window = lines[start_idx:end_idx]
    body = "\n".join(window)
    return _GENERIC_OMITTED_PREFIX + body + _GENERIC_OMITTED_SUFFIX
