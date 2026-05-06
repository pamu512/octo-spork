"""Lazy tree-sitter ``Parser`` instances."""

from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Parser


@lru_cache(maxsize=8)
def python_parser() -> Parser:
    import tree_sitter_python as tsp

    return Parser(Language(tsp.language()))


@lru_cache(maxsize=8)
def javascript_parser() -> Parser:
    import tree_sitter_javascript as tsjs

    return Parser(Language(tsjs.language()))


@lru_cache(maxsize=8)
def typescript_parser() -> Parser:
    import tree_sitter_typescript as tst

    return Parser(Language(tst.language_typescript()))


@lru_cache(maxsize=8)
def tsx_parser() -> Parser:
    import tree_sitter_typescript as tst

    return Parser(Language(tst.language_tsx()))
