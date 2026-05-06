"""Resolve import targets to repository-relative module/file ids (mirrors architecture_map heuristics)."""

from __future__ import annotations

from pathlib import Path


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


def resolve_relative_py_import(
    from_file: Path, scan_root: Path, level: int, module: str | None
) -> str | None:
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


def is_internal_target(node: str, scan_root: Path) -> bool:
    if not node or node.startswith(".."):
        return False
    root_s = scan_root.resolve()
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


def top_level_prefix(node_id: str) -> str:
    parts = [x for x in node_id.split("/") if x]
    return parts[0] if parts else "(root)"
