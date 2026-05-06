"""CLI: ``check``, ``run`` (wraps ``ollama run``), ``watch`` (/api/ps), ``proxy``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ollama_guard.client import ollama_ps
from ollama_guard.policy import analyze_model, resolve_model_for_run


def _default_base_url() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _cmd_check(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    d = analyze_model(args.model, base_url=base)
    print(d.reason)
    if d.free_mib is not None:
        est = f"{d.estimated_mib:.0f}" if d.estimated_mib is not None else "?"
        print(f"free_vram_mib={d.free_mib:.0f} estimated_mib={est}")
    if d.proposed_model:
        print(f"proposed_model={d.proposed_model}")
    if args.json:
        print(
            json.dumps(
                {
                    "model": d.model,
                    "free_mib": d.free_mib,
                    "estimated_mib": d.estimated_mib,
                    "fits_without_change": d.fits_without_change,
                    "proposed_model": d.proposed_model,
                    "reason": d.reason,
                },
                indent=2,
            )
        )
    if d.fits_without_change is False:
        return 2
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    use, decision, ok, msg = resolve_model_for_run(
        args.model,
        base_url=base,
        pull=args.pull,
    )
    print(decision.reason, file=sys.stderr)
    if decision.proposed_model and use != args.model:
        print(
            f"[ollama-guard] using `{use}` instead of `{args.model}`.",
            file=sys.stderr,
        )
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    if msg:
        print(msg, file=sys.stderr)
    argv = ["ollama", "run", use, *args.rest]
    os.execvp("ollama", argv)


def _cmd_watch(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    interval = max(1.0, args.interval)
    while True:
        data = ollama_ps(base) or {}
        models = data.get("models") or []
        for item in models:
            name = item.get("model") or item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            d = analyze_model(name.strip(), base_url=base)
            if d.fits_without_change is False and d.proposed_model:
                print(
                    f"[watch] running `{name}` may exceed VRAM — suggest `{d.proposed_model}`",
                    flush=True,
                )
                print(f"         {d.reason}", flush=True)
        time.sleep(interval)


def _cmd_proxy(args: argparse.Namespace) -> int:
    os.environ["OLLAMA_GUARD_UPSTREAM"] = args.upstream.rstrip("/")
    if args.analyze_url:
        os.environ["OLLAMA_GUARD_ANALYZE_URL"] = args.analyze_url.rstrip("/")
    import uvicorn

    from ollama_guard.http_proxy import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ollama-guard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Estimate VRAM vs free GPU memory and prefer quantized Ollama tags when needed.\n"
            "Environment: OLLAMA_HOST, OLLAMA_GUARD_VRAM_HEADROOM, OLLAMA_GUARD_KV_OVERHEAD_MIB,\n"
            "OLLAMA_GUARD_QUANT_SUFFIXES."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=_default_base_url(),
        dest="base_url",
        help="Ollama API base for /api/show and /api/ps (default: $OLLAMA_HOST or http://127.0.0.1:11434)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_check = sub.add_parser("check", help="Print VRAM decision for a model tag")
    sp_check.add_argument("model")
    sp_check.add_argument("--json", action="store_true", help="Emit JSON details")
    sp_check.set_defaults(func=_cmd_check)

    sp_run = sub.add_parser(
        "run",
        help="Resolve tag (maybe quantized), optionally pull, then exec `ollama run`",
    )
    sp_run.add_argument("model")
    sp_run.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="Extra args forwarded to `ollama run`",
    )
    sp_run.add_argument(
        "--pull",
        action="store_true",
        help="Run `ollama pull` if the resolved tag is missing locally",
    )
    sp_run.set_defaults(func=_cmd_run)

    sp_watch = sub.add_parser(
        "watch",
        help="Poll GET /api/ps and warn when a running model likely exceeds free VRAM",
    )
    sp_watch.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between /api/ps polls (default: 5)",
    )
    sp_watch.set_defaults(func=_cmd_watch)

    sp_proxy = sub.add_parser(
        "proxy",
        help="HTTP proxy that rewrites model on generate/chat/embeddings POSTs",
    )
    sp_proxy.add_argument(
        "--upstream",
        default="",
        help="Real Ollama base URL (default: same as --base-url)",
    )
    sp_proxy.add_argument(
        "--analyze-url",
        default="",
        help="Base URL for /api/show during decisions (default: same as --upstream)",
    )
    sp_proxy.add_argument("--host", default="127.0.0.1")
    sp_proxy.add_argument("--port", type=int, default=11435)
    sp_proxy.set_defaults(func=_cmd_proxy)

    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")
    if args.cmd == "proxy":
        if not args.upstream:
            args.upstream = args.base_url
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
