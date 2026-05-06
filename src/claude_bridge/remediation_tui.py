"""Interactive split-pane 'Doctor' UI: security finding vs Claude-proposed fix, with safe Apply (human-in-the-loop).

Run::

    PYTHONPATH=src python -m claude_bridge.remediation_tui --repo . \\
        --finding-file vuln.md --fix-file proposed.txt --target src/foo.py

Or ``python3 -m local_ai_stack remediation-ui ...`` (see ``local_ai_stack.__main__``).

The fix panel may start with ``# OCTO_EDIT_TARGET: relative/path.py`` to choose the write target
when ``--target`` is omitted.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_EDIT_TARGET_RE = re.compile(
    r"^\s*#\s*OCTO_EDIT_TARGET:\s*(.+?)\s*$",
    re.IGNORECASE,
)


def ensure_package_on_path() -> Path:
    """Ensure ``src`` is importable when launched without PYTHONPATH."""
    here = Path(__file__).resolve()
    src = here.parents[1]
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return src.parent


def extract_edit_target(fix_text: str, explicit: str | None) -> str | None:
    """Resolve repo-relative path for Apply: CLI wins, else ``# OCTO_EDIT_TARGET:`` header."""
    if explicit and explicit.strip():
        return explicit.strip()
    for line in fix_text.splitlines()[:40]:
        m = _EDIT_TARGET_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


def strip_leading_edit_directives(fix_text: str) -> str:
    """Remove leading ``# OCTO_EDIT_TARGET:`` lines so they are not written into the file."""
    lines = fix_text.splitlines()
    i = 0
    while i < len(lines) and _EDIT_TARGET_RE.match(lines[i]):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


def apply_file_edit(repo_root: Path, target_rel: str, new_body: str) -> tuple[bool, str]:
    """Host-side FileEdit-style write guarded by :class:`~claude_bridge.safe_path.SafePathMiddleware`."""
    from claude_bridge.safe_path import SafePathMiddleware, SafePathViolation

    root = repo_root.expanduser().resolve()
    mw = SafePathMiddleware(root)
    try:
        path = mw.assert_allowed(target_rel)
    except SafePathViolation as exc:
        return False, str(exc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_body, encoding="utf-8", newline="\n")
    except OSError as exc:
        return False, str(exc)
    return True, str(path)


def load_text(path: Path | None, default: str) -> str:
    if path is None or not path.is_file():
        return default
    return path.read_text(encoding="utf-8", errors="replace")


def build_app_class():
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Button, Footer, Header, Static

    class RemediationDoctor(App[None]):
        """Split finding | proposed fix; Apply runs sandboxed file write."""

        CSS = """
        Screen {
            layout: vertical;
        }
        Horizontal#split {
            height: 1fr;
            min-height: 8;
        }
        VerticalScroll.pane {
            width: 1fr;
            border: heavy $primary;
            padding: 0 1;
        }
        Static.title {
            padding: 1 1 0 1;
            text-style: bold;
            background: $surface;
        }
        Static.body {
            padding: 0 1 1 1;
        }
        Horizontal#actions {
            height: auto;
            dock: bottom;
            padding: 1 2;
            background: $surface;
        }
        Button {
            margin-right: 2;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit", show=True),
            Binding("a", "apply", "Apply", show=True),
        ]

        def __init__(
            self,
            *,
            repo_root: Path,
            finding_text: str,
            fix_text: str,
            explicit_target: str | None,
        ) -> None:
            super().__init__()
            self.repo_root = repo_root.expanduser().resolve()
            self.finding_text = finding_text
            self.fix_text = fix_text
            self.explicit_target = explicit_target

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="split"):
                with VerticalScroll(classes="pane", id="left-pane"):
                    yield Static("Security finding / vulnerability", classes="title")
                    yield Static(self.finding_text, classes="body", shrink=False)
                with VerticalScroll(classes="pane", id="right-pane"):
                    yield Static("Claude Code proposed fix (file body or patch context)", classes="title")
                    yield Static(self.fix_text, classes="body", shrink=False)
            with Horizontal(id="actions"):
                yield Button("Apply (write file)", variant="success", id="btn_apply")
                yield Button("Quit", variant="error", id="btn_quit")
            yield Footer()

        def action_quit(self) -> None:
            self.exit()

        def action_apply(self) -> None:
            self._do_apply()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn_apply":
                self._do_apply()
            elif event.button.id == "btn_quit":
                self.exit()

        def _do_apply(self) -> None:
            target = extract_edit_target(self.fix_text, self.explicit_target)
            if not target:
                self.notify(
                    "No write target: pass --target or add a line "
                    "'# OCTO_EDIT_TARGET: path/to/file' at the top of the fix.",
                    severity="error",
                    timeout=12,
                )
                return
            body = strip_leading_edit_directives(self.fix_text)
            ok, msg = apply_file_edit(self.repo_root, target, body)
            if ok:
                self.notify(f"Applied fix → {msg}", title="FileEdit", timeout=10)
            else:
                self.notify(msg, severity="error", title="Apply blocked", timeout=12)

    return RemediationDoctor


def run_remediation_tui(
    *,
    repo: Path,
    finding_file: Path | None,
    fix_file: Path | None,
    target: str | None,
    demo: bool,
) -> int:
    ensure_package_on_path()

    if demo:
        finding = (
            "## Example: SQL injection risk\n\n"
            "`execute(query)` builds SQL from request parameters without binding.\n\n"
            "**Severity:** High\n"
        )
        fix = (
            "# OCTO_EDIT_TARGET: .octo-remediation-demo.txt\n"
            "# Human-in-the-loop demo — Apply creates this file under --repo if allowed.\n"
            "remediation_ok=true\n"
        )
    else:
        finding = load_text(
            finding_file,
            "_No finding file — pass --finding-file or use --demo._",
        )
        fix = load_text(
            fix_file,
            "_No fix file — pass --fix-file or use --demo._",
        )

    RemediationDoctor = build_app_class()
    app = RemediationDoctor(
        repo_root=repo,
        finding_text=finding,
        fix_text=fix,
        explicit_target=target,
    )
    app.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Split-pane remediation Doctor UI (finding vs fix, Apply = sandboxed write).",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository root for SafePath edits (default: .)",
    )
    parser.add_argument("--finding-file", type=Path, default=None, help="Markdown/text for left pane")
    parser.add_argument("--fix-file", type=Path, default=None, help="Proposed file body for right pane")
    parser.add_argument(
        "--target",
        default=None,
        help="Repo-relative path to write on Apply (overrides # OCTO_EDIT_TARGET in fix file)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Load built-in sample finding/fix (writes .octo-remediation-demo.txt when applied)",
    )
    args = parser.parse_args(argv)
    return run_remediation_tui(
        repo=args.repo.expanduser().resolve(),
        finding_file=args.finding_file,
        fix_file=args.fix_file,
        target=args.target,
        demo=args.demo,
    )


if __name__ == "__main__":
    raise SystemExit(main())
