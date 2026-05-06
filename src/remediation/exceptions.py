"""Remediation-specific errors."""

from __future__ import annotations


class VerificationFailedError(Exception):
    """Post-patch verification failed (e.g. target CVE still reported by Trivy)."""

    def __init__(self, message: str, *, snippet: str = "") -> None:
        super().__init__(message)
        self.snippet = snippet
        """Human/LLM-oriented excerpt (e.g. JSON fragment from the failing scan)."""
