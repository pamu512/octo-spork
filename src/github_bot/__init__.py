"""GitHub webhook FastAPI application."""

from typing import Any

__all__ = ["GitHubAuth"]


def __getattr__(name: str) -> Any:
    if name == "GitHubAuth":
        from .auth import GitHubAuth as gh

        return gh
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
