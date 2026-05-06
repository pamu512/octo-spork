"""Decide if a model likely exceeds VRAM and propose quantized alternatives."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from ollama_guard.client import ollama_list_tags, ollama_show
from ollama_guard.estimate import (
    estimate_weight_mib,
    infer_params_from_name,
    parse_parameter_size,
    quant_bytes_per_param,
    recommend_bpp,
)
from ollama_guard.vram import sample_gpu_free_mib


def headroom_ratio() -> float:
    raw = (os.environ.get("OLLAMA_GUARD_VRAM_HEADROOM") or "0.82").strip()
    try:
        return max(0.5, min(0.98, float(raw)))
    except ValueError:
        return 0.82


def kv_overhead_mib() -> float:
    raw = (os.environ.get("OLLAMA_GUARD_KV_OVERHEAD_MIB") or "768").strip()
    try:
        return max(128.0, float(raw))
    except ValueError:
        return 768.0


def candidate_quant_tags(model_name: str) -> list[str]:
    """Generate plausible alternative Ollama tags (same registry image, lighter quant)."""
    raw = (os.environ.get("OLLAMA_GUARD_QUANT_SUFFIXES") or "").strip()
    if raw:
        suffixes = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        suffixes = [
            "q4_K_M",
            "q4_0",
            "Q4_K_M",
            "q4_K_S",
            "q3_K_M",
            "iq4_xs",
        ]

    repo, sep, tag = model_name.partition(":")
    if not sep:
        repo, tag = model_name, "latest"

    out: list[str] = []
    base_tag = tag

    strip_quant = re.sub(
        r"-(?:q\d|Q\d|iq\d)[-_a-zA-Z0-9]*$",
        "",
        base_tag,
        flags=re.IGNORECASE,
    )
    if strip_quant != base_tag:
        base_tag = strip_quant.rstrip("-")

    for suf in suffixes:
        cand_tag = f"{base_tag}-{suf}" if base_tag else suf
        out.append(f"{repo}:{cand_tag}")

    for suf in suffixes:
        out.append(f"{repo}:{suf}")

    dedup: list[str] = []
    seen: set[str] = set()
    for n in out:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    return dedup


@dataclass
class GuardDecision:
    model: str
    free_mib: float | None
    estimated_mib: float | None
    params_billions: float | None
    bpp: float | None
    quantization_level: str | None
    fits_without_change: bool | None
    proposed_model: str | None
    reason: str


def analyze_model(
    model_name: str,
    *,
    base_url: str,
    free_mib_override: float | None = None,
) -> GuardDecision:
    """Inspect model metadata + VRAM and decide whether to swap to a quantized tag."""
    free_mib, _gpu = sample_gpu_free_mib() if free_mib_override is None else (free_mib_override, {})
    show = ollama_show(base_url, model_name)
    details = (show or {}).get("details") if isinstance(show, dict) else None
    details = details if isinstance(details, dict) else {}

    qlevel = details.get("quantization_level")
    qlevel_s = str(qlevel) if qlevel is not None else None

    ps = details.get("parameter_size")
    pb = parse_parameter_size(str(ps)) if ps is not None else None
    if pb is None:
        pb = infer_params_from_name(model_name)

    bpp = quant_bytes_per_param(qlevel_s)

    est_mib = None
    if pb is not None:
        est_mib = estimate_weight_mib(
            params_billions=pb,
            bpp=bpp,
            kv_overhead_mib=kv_overhead_mib(),
        )

    proposed: str | None = None
    reason = ""

    if free_mib is None:
        reason = "GPU memory unavailable (nvidia-smi missing or failed); cannot enforce VRAM guard."
        return GuardDecision(
            model=model_name,
            free_mib=None,
            estimated_mib=est_mib,
            params_billions=pb,
            bpp=bpp,
            quantization_level=qlevel_s,
            fits_without_change=None,
            proposed_model=None,
            reason=reason,
        )

    if est_mib is None:
        reason = "Could not estimate model size (no parameter_size in /api/show and name heuristic failed)."
        return GuardDecision(
            model=model_name,
            free_mib=free_mib,
            estimated_mib=None,
            params_billions=pb,
            bpp=bpp,
            quantization_level=qlevel_s,
            fits_without_change=None,
            proposed_model=None,
            reason=reason,
        )

    budget = free_mib * headroom_ratio()
    fits = est_mib <= budget

    if fits:
        return GuardDecision(
            model=model_name,
            free_mib=free_mib,
            estimated_mib=est_mib,
            params_billions=pb,
            bpp=bpp,
            quantization_level=qlevel_s,
            fits_without_change=True,
            proposed_model=None,
            reason=f"Estimated VRAM footprint ~{est_mib:.0f} MiB fits under budget ~{budget:.0f} MiB (free {free_mib:.0f} MiB × headroom).",
        )

    reason = (
        f"Estimated footprint ~{est_mib:.0f} MiB exceeds budget ~{budget:.0f} MiB "
        f"(free VRAM ~{free_mib:.0f} MiB × {headroom_ratio():.2f})."
    )

    target_bpp = recommend_bpp(bpp)
    proposed = None
    if target_bpp is None or pb is None:
        reason += " Selecting first heuristic quantized tag candidate."
    else:
        alt_mib = estimate_weight_mib(
            params_billions=pb,
            bpp=target_bpp,
            kv_overhead_mib=kv_overhead_mib() * 0.85,
        )
        reason += f" Lighter quant tier (~{target_bpp} B/param) estimates ~{alt_mib:.0f} MiB."

    for cand in candidate_quant_tags(model_name):
        sh = ollama_show(base_url, cand)
        if isinstance(sh, dict) and not sh.get("error"):
            proposed = cand
            reason += f" `/api/show` accepts `{cand}`."
            break

    if proposed is None:
        proposed = candidate_quant_tags(model_name)[0]
        reason += f" Using `{proposed}` as pull target (confirm tag exists in Ollama library)."

    return GuardDecision(
        model=model_name,
        free_mib=free_mib,
        estimated_mib=est_mib,
        params_billions=pb,
        bpp=bpp,
        quantization_level=qlevel_s,
        fits_without_change=False,
        proposed_model=proposed,
        reason=reason,
    )


def run_ollama_pull(model: str, *, timeout_sec: float = 7200.0) -> tuple[int, str]:
    """Invoke ``ollama pull``; return exit code and merged stdout/stderr."""
    try:
        proc = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return int(proc.returncode), out.strip()
    except FileNotFoundError:
        return 127, "ollama CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "ollama pull timed out"


def list_local_model_names() -> set[str]:
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return set()
        return ollama_list_tags(proc.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return set()


def resolve_model_for_run(
    model: str,
    *,
    base_url: str,
    pull: bool,
) -> tuple[str, GuardDecision, bool, str]:
    """Pick an executable tag (possibly quantized), then ensure it exists locally."""
    decision = analyze_model(model, base_url=base_url)
    use = model
    if decision.fits_without_change is False and decision.proposed_model:
        use = decision.proposed_model
    ok, msg = ensure_model_present(use, pull=pull)
    return use, decision, ok, msg


def ensure_model_present(model: str, *, pull: bool) -> tuple[bool, str]:
    """Return (ok, message)."""
    names = list_local_model_names()
    if model in names:
        return True, f"Model `{model}` already present (`ollama list`)."
    if not pull:
        return False, f"Model `{model}` not installed — pass --pull to fetch."
    code, out = run_ollama_pull(model)
    if code == 0:
        return True, out or f"Pulled `{model}`."
    return False, out or f"ollama pull exited {code}"
