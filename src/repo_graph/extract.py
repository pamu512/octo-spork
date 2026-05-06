"""Tree-sitter import / export extraction."""

from __future__ import annotations

from pathlib import Path

from repo_graph.parsers import javascript_parser, python_parser, tsx_parser, typescript_parser
from repo_graph.resolve import (
    file_to_node,
    resolve_absolute_py_module,
    resolve_relative_py_import,
    resolve_ts_import,
)


def _py_relative_level_module(relative_node) -> tuple[int, str | None]:
    raw = relative_node.text.decode("utf-8", errors="replace")
    i = 0
    while i < len(raw) and raw[i] == ".":
        i += 1
    level = i
    mod = raw[i:] if i < len(raw) else None
    return (level, mod if mod else None)


def _py_import_from_edges(node, from_file: Path, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    kids = list(node.children)
    import_idx = None
    from_idx = None
    for i, k in enumerate(kids):
        if k.type == "from":
            from_idx = i
        if k.type == "import" and k.text == b"import":
            import_idx = i
            break
    if import_idx is None:
        return edges
    between = kids[from_idx + 1 : import_idx] if from_idx is not None else []
    rel = None
    mod_abs: str | None = None
    for ch in between:
        if ch.type == "relative_import":
            rel = ch
        elif ch.type == "dotted_name":
            mod_abs = ch.text.decode("utf-8", errors="replace")
            break
    if rel is not None:
        level, mod = _py_relative_level_module(rel)
        tgt = resolve_relative_py_import(from_file, scan_root, level, mod)
        if tgt:
            edges.add((src_id, tgt))
        return edges
    if mod_abs:
        tgt = resolve_absolute_py_module(mod_abs, scan_root)
        if tgt:
            edges.add((src_id, tgt))
    return edges


def _py_import_statement_edges(node, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for ch in node.named_children:
        if ch.type == "dotted_as_names":
            for das in ch.named_children:
                if das.type != "dotted_as_name":
                    continue
                for sub in das.named_children:
                    if sub.type == "dotted_name":
                        mod = sub.text.decode("utf-8", errors="replace")
                        tgt = resolve_absolute_py_module(mod, scan_root)
                        if tgt:
                            edges.add((src_id, tgt))
        elif ch.type == "dotted_name":
            mod = ch.text.decode("utf-8", errors="replace")
            tgt = resolve_absolute_py_module(mod, scan_root)
            if tgt:
                edges.add((src_id, tgt))
    return edges


def extract_python_edges(from_file: Path, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    try:
        text = from_file.read_bytes()
    except OSError:
        return edges
    parser = python_parser()
    tree = parser.parse(text)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_from_statement":
            edges |= _py_import_from_edges(node, from_file, scan_root, src_id)
        elif node.type == "import_statement":
            edges |= _py_import_statement_edges(node, scan_root, src_id)
        for c in node.children:
            stack.append(c)
    return edges


def _decode_js_string(node) -> str | None:
    if node.type != "string":
        return None
    raw = node.text.decode("utf-8", errors="replace")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    frag = None
    for ch in node.named_children:
        if ch.type == "string_fragment":
            frag = ch.text.decode("utf-8", errors="replace")
    return frag


def _collect_js_module_specs(root_node) -> list[str]:
    specs: list[str] = []
    stack = [root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            seen_from = False
            for ch in node.children:
                if ch.type == "from":
                    seen_from = True
                if ch.type == "string":
                    s = _decode_js_string(ch)
                    if s and seen_from:
                        specs.append(s)
                if ch.type == "string" and not seen_from:
                    s = _decode_js_string(ch)
                    if s:
                        specs.append(s)
        elif node.type == "export_statement":
            has_from = any(c.type == "from" for c in node.children)
            if has_from:
                for ch in node.children:
                    if ch.type == "string":
                        s = _decode_js_string(ch)
                        if s:
                            specs.append(s)
        elif node.type == "call_expression":
            children = list(node.children)
            if not children:
                pass
            else:
                fn = children[0]
                is_req = fn.type == "identifier" and fn.text == b"require"
                is_dyn_import = fn.type == "import"
                if is_req or is_dyn_import:
                    for ch in children:
                        if ch.type == "arguments":
                            for a in ch.named_children:
                                if a.type == "string":
                                    s = _decode_js_string(a)
                                    if s:
                                        specs.append(s)
        for c in node.children:
            stack.append(c)
    return specs


def _parser_for_path(path: Path):
    suf = path.suffix.lower()
    if suf in (".ts",):
        return typescript_parser()
    if suf in (".tsx",):
        return tsx_parser()
    return javascript_parser()


def extract_js_ts_edges(from_file: Path, scan_root: Path, src_id: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    try:
        text = from_file.read_bytes()
    except OSError:
        return edges
    parser = _parser_for_path(from_file)
    try:
        tree = parser.parse(text)
    except Exception:
        return edges
    for spec in _collect_js_module_specs(tree.root_node):
        tgt = resolve_ts_import(spec, from_file, scan_root)
        if tgt:
            edges.add((src_id, tgt))
    return edges


def build_edges_for_file(path: Path, scan_root: Path) -> set[tuple[str, str]]:
    try:
        src_id = file_to_node(scan_root, path)
    except ValueError:
        return set()
    suf = path.suffix.lower()
    if suf == ".py":
        return extract_python_edges(path, scan_root, src_id)
    if suf in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return extract_js_ts_edges(path, scan_root, src_id)
    return set()
