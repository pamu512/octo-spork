"""LangGraph agent state and SARIF-shaped vulnerability context (Trivy ``--format sarif``)."""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import BaseMessage


# --- SARIF 2.1.0 fragments matching JSON emitted by Trivy filesystem scans (subset of OASIS SARIF) ---


class SarifMultiformatMessageString(TypedDict, total=False):
    """SARIF ``message`` object ``text`` carrier."""

    text: str


class SarifArtifactLocation(TypedDict, total=False):
    """``artifactLocation`` inside ``physicalLocation``."""

    uri: str
    uriBaseId: str


class SarifRegion(TypedDict, total=False):
    """Source region inside ``physicalLocation``."""

    startLine: int
    startColumn: int
    endLine: int
    endColumn: int


class SarifPhysicalLocation(TypedDict, total=False):
    """Maps a tool finding to a file and optional line span."""

    artifactLocation: SarifArtifactLocation
    region: SarifRegion


class SarifLocation(TypedDict, total=False):
    """Single ``location`` element under a ``result``."""

    physicalLocation: SarifPhysicalLocation


class SarifResult(TypedDict, total=False):
    """One SARIF ``result`` object (CVE / misconfiguration row)."""

    ruleId: str
    ruleIndex: int
    level: str
    message: SarifMultiformatMessageString
    locations: list[SarifLocation]


class SarifReportingRule(TypedDict, total=False):
    """Single SARIF ``reportingDescriptor`` (rule metadata)."""

    id: str
    name: str
    shortDescription: SarifMultiformatMessageString
    fullDescription: SarifMultiformatMessageString


class SarifToolComponent(TypedDict, total=False):
    """``driver`` describing the scanner executable."""

    name: str
    version: str
    fullName: str
    rules: list[SarifReportingRule]


class SarifTool(TypedDict, total=False):
    """``tool`` block attached to a SARIF ``run``."""

    driver: SarifToolComponent


class SarifRun(TypedDict, total=False):
    """Single SARIF ``run`` (one scanner invocation)."""

    tool: SarifTool
    results: list[SarifResult]


# Functional form allows the SARIF ``$schema`` property (invalid as a Python identifier in class body).
TrivySarifJson = TypedDict(
    "TrivySarifJson",
    {
        "$schema": str,
        "version": str,
        "runs": list[SarifRun],
    },
    total=False,
)
"""JSON payload shape for Trivy SARIF output (``trivy fs --format sarif``).

Empty scans commonly serialize as ``{\"runs\": []}``. Production runs include ``version`` ``\"2.1.0\"``
and optionally ``\"$schema\"`` pointing at the OASIS SARIF JSON Schema URI.
"""


class AgentState(TypedDict):
    """Checkpointed LangGraph state for the remediation agent."""

    messages: list[BaseMessage]
    current_file: str
    target_cve: str
    vulnerability_context: TrivySarifJson
    test_failures: int
    is_verified: bool
    start_time: float
