from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WA = ROOT / "deploy" / "n8n" / "scripts" / "wa_send.sh"


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
