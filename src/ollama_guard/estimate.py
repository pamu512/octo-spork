"""Parse Ollama ``/api/show`` output and estimate GPU weight footprint."""

from __future__ import annotations

import re


_PARAM_RE = re.compile(
    r"^\s*([\d.]+)\s*([PTGMKB])?\s*$",
    re.IGNORECASE,
)


def parse_parameter_size(text: str | None) -> float | None:
    """Return approximate parameter count (billions) from strings like ``8.0B``, ``70B``."""
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    m = _PARAM_RE.match(s)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    mult = {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3, "P": 1e6, "B": 1.0}.get(unit, 1.0)
    billions = val * mult
    return billions if billions > 0 else None


def quant_bytes_per_param(quantization_level: str | None) -> float:
    """Rough bytes per parameter for GGUF-style tiers (weights only)."""
    if not quantization_level:
        return 2.0
    q = quantization_level.upper()
    if "Q2" in q or "Q3" in q:
        return 0.45
    if "Q4" in q:
        return 0.55
    if "Q5" in q:
        return 0.65
    if "Q6" in q:
        return 0.75
    if "Q8" in q or "I8" in q:
        return 1.0
    if "F16" in q or "FP16" in q:
        return 2.0
    if "F32" in q or "FP32" in q:
        return 4.0
    return 1.0


def infer_params_from_name(model_name: str) -> float | None:
    """Heuristic: ``qwen2.5:32b`` → 32.0 billion parameters."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m2 = re.search(r"-(\d+)b", model_name, re.IGNORECASE)
    if m2:
        return float(m2.group(1))
    return None


def estimate_weight_mib(
    *,
    params_billions: float,
    bpp: float,
    kv_overhead_mib: float,
) -> float:
    """Approximate MiB for model weights + fixed KV/runtime overhead."""
    params = params_billions * 1e9
    bytes_w = params * bpp
    mib = bytes_w / (1024.0 * 1024.0)
    return mib + kv_overhead_mib


def recommend_bpp(current_bpp: float) -> float | None:
    """Next lighter quantization step for planning alternate pulls."""
    order = [4.0, 2.0, 1.0, 0.75, 0.65, 0.55, 0.45]
    for b in order:
        if b < current_bpp * 0.92:
            return b
    return None
