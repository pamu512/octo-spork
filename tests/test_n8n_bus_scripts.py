from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WA = ROOT / "deploy" / "n8n" / "scripts" / "wa_send.sh"
HERMES_BRIEF = ROOT / "deploy" / "n8n" / "scripts" / "run_hermes_brief.sh"
WORKFLOWS = ROOT / "deploy" / "n8n" / "workflows"
WAVE1_WORKFLOWS = (
    "octo_nightly_audit.workflow.json",
    "hermes_fraud_brief.workflow.json",
    "hermes_finance_brief.workflow.json",
    "ollama_health.workflow.json",
    "tarka_ci_red.workflow.json",
)


def _wa_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WA_BRIEFS_TO"] = "whatsapp:+85200000000"
    env["WA_ALERTS_TO"] = "whatsapp:+85211111111"
    return env


def test_wa_send_requires_channel() -> None:
    assert WA.is_file()
    proc = subprocess.run(
        ["bash", str(WA), "--text", "hi"],
        cwd=ROOT,
        env=_wa_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_wa_send_unknown_channel_fails() -> None:
    assert WA.is_file()
    proc = subprocess.run(
        ["bash", str(WA), "--channel", "other", "--text", "hi"],
        cwd=ROOT,
        env=_wa_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_run_hermes_brief_fails_without_root() -> None:
    assert HERMES_BRIEF.is_file()
    env = os.environ.copy()
    env.pop("HERMES_FRAUD_ROOT", None)
    env.pop("HERMES_FINANCE_ROOT", None)
    proc = subprocess.run(
        ["bash", str(HERMES_BRIEF), "fraud"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_wave1_workflow_exports_are_importable() -> None:
    required = set(WAVE1_WORKFLOWS)
    on_disk = {p.name for p in WORKFLOWS.glob("*.workflow.json")}
    assert required <= on_disk
    for name in WAVE1_WORKFLOWS:
        data = json.loads((WORKFLOWS / name).read_text())
        assert data["active"] is False
        assert "nodes" in data and "connections" in data
        assert data.get("pinData") == {}
        assert "versionId" in data
        names = {node["name"] for node in data["nodes"]}
        ids = [node["id"] for node in data["nodes"]]
        assert len(ids) == len(set(ids))
        for src, conn in data["connections"].items():
            assert src in names
            for branch in conn["main"]:
                for link in branch:
                    assert link["node"] in names
                    assert link["type"] == "main"
