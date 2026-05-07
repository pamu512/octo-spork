"""Shell execution tool for agents (subprocess-based; structured results)."""

from __future__ import annotations

import os
import shlex
import subprocess

_COMMAND_TIMEOUT_SECONDS = 30
_MAX_SANITIZED_OUTPUT_CHARS = 4000
_TRUNCATION_SUFFIX = "\n...[TRUNCATED FOR MEMORY LIMITS]..."


class TerminalTool:
    """Run shell commands through ``subprocess`` and surface machine-readable outcomes to callers.

    Instances of this class are responsible for invoking the operating system shell or a command
    interpreter through Python standard-library subprocess facilities so that agent logic never
    shells out ad hoc: all command execution flows through this single surface where timeouts,
    working directories, environment inheritance, and output handling can be enforced consistently.

    The primary entry point is :meth:`execute`, which returns a plain dictionary whose keys are
    always the strings ``exit_code``, ``stdout``, and ``stderr``. The ``exit_code`` field carries the process
    termination status as reported by the subprocess layer (zero conventionally means success on
    POSIX systems). The ``stdout`` and ``stderr`` fields contain the decoded textual streams
    captured from the child process after any sanitization policy applied by :meth:`_sanitize_output`
    has been run independently on each stream string.

    Raw bytes from the subprocess are expected to be decoded to Unicode strings before sanitization;
    consumers should treat absent output as the empty string rather than ``None``. Commands are
    parsed into an argument vector with :func:`shlex.split` and executed without ``shell=True`` so
    shell metacharacters are not interpreted by a system shell; see :meth:`execute` for details.

    Side effects of commands (filesystem mutations, network calls, container interactions) are
    identical to running the same command in an interactive terminal subject to the configured
    sandbox parameters; this class does not imply isolation beyond what the eventual subprocess
    invocation configures explicitly.
    """

    def __init__(self) -> None:
        """Prepare the tool for subsequent command execution.

        Initialization establishes immutable configuration that applies to every future
        :meth:`execute` call issued on this instance, such as default timeouts, working directory,
        environment-variable overlays, and encoding choices used when decoding subprocess byte
        streams before sanitization.
        """
        pass

    def execute(self, command: str) -> dict:
        """Execute a shell command string via ``subprocess`` and return structured results.

        The ``command`` string is split into an executable path and arguments using
        :func:`shlex.split` with POSIX rules on POSIX platforms and Windows-correct quoting rules on
        Windows (``posix=False``). The resulting argv list is passed to :func:`subprocess.run` with
        ``shell=False``. This avoids invoking an interactive shell, which prevents attackers from
        injecting extra operators or subshells through metacharacters in ``command``. Using
        ``shell=True`` would reintroduce that entire class of vulnerabilities because the string
        would be interpreted by ``/bin/sh``, ``cmd.exe``, or an equivalent; this implementation does
        not enable ``shell=True``.

        A wall-clock timeout of 30 seconds is enforced. If the child process does not finish in
        time, :exc:`subprocess.TimeoutExpired` is handled: the returned dictionary uses
        ``exit_code`` ``-1`` and ``stderr`` describes the timeout (partial stream captures, if any,
        are still sanitized into ``stdout`` and ``stderr``).

        When the child exits with a non-zero status, :exc:`subprocess.CalledProcessError` is raised
        by :func:`subprocess.run` because ``check=True``; it is caught and translated into a result
        whose ``exit_code`` is the process exit status and whose streams reflect captured output.

        Operating-system failures while starting or waiting on the process (for example missing
        executable, permission denied, path errors) are caught as :exc:`OSError` and reported with
        ``exit_code`` ``-1`` and a textual explanation in ``stderr``.

        Returns
        -------
        dict
            A dictionary with exactly three keys:

            ``exit_code``
                Integer process exit status from ``subprocess.CompletedProcess.returncode`` or the
                equivalent when non-blocking or canceled behaviors are modeled.

            ``stdout``
                Sanitized textual standard output of the command (decoded bytes).

            ``stderr``
                Sanitized textual standard error of the command (decoded bytes).

        Implementation will ensure these keys are always present so callers can rely on stable
        dictionary shape without optional keys.
        """
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": self._sanitize_output(f"command parse error: {exc}"),
            }

        if not argv:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": self._sanitize_output("empty command: no executable or arguments after parsing"),
            }

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": -1,
                "stdout": self._sanitize_output(exc.stdout),
                "stderr": self._sanitize_output(
                    _format_timeout_stderr(exc, _COMMAND_TIMEOUT_SECONDS)
                ),
            }
        except subprocess.CalledProcessError as exc:
            return {
                "exit_code": exc.returncode,
                "stdout": self._sanitize_output(exc.stdout),
                "stderr": self._sanitize_output(exc.stderr),
            }
        except OSError as exc:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": self._sanitize_output(f"{type(exc).__name__}: {exc}"),
            }

        return {
            "exit_code": completed.returncode,
            "stdout": self._sanitize_output(completed.stdout),
            "stderr": self._sanitize_output(completed.stderr),
        }

    def _sanitize_output(self, output: object) -> str:
        """Normalize or redact streaming subprocess output before exposing it to agents.

        Parameters
        ----------
        output
            Text from stdout or stderr. ``None`` becomes an empty string. :class:`bytes` are
            decoded as UTF-8 with replacement for invalid sequences. Other types are converted with
            :func:`str`; if conversion fails, the result is treated as empty.

        Returns
        -------
        str
            NUL bytes are removed. The returned string is never longer than
            ``_MAX_SANITIZED_OUTPUT_CHARS`` (4000). If the normalized text would exceed that length,
            it is sliced so that the final value is exactly that many characters: a prefix of the
            text followed by ``\\n...[TRUNCATED FOR MEMORY LIMITS]...``.

        This method will be invoked independently for stdout and stderr so that sanitization rules
        apply uniformly regardless of which stream carried a given fragment of text.
        """
        text = _coerce_for_sanitization(output)
        text = text.replace("\x00", "")
        limit = _MAX_SANITIZED_OUTPUT_CHARS
        if len(text) <= limit:
            return text
        suffix_len = len(_TRUNCATION_SUFFIX)
        max_prefix = limit - suffix_len
        if max_prefix <= 0:
            return _TRUNCATION_SUFFIX[:limit] if limit > 0 else ""
        return text[:max_prefix] + _TRUNCATION_SUFFIX


def _coerce_for_sanitization(output: object) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    try:
        return str(output)
    except Exception:
        return ""


def _stringify_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _format_timeout_stderr(exc: subprocess.TimeoutExpired, timeout_seconds: int) -> str:
    parts = [f"Command timed out after {timeout_seconds} seconds."]
    err = _stringify_stream(exc.stderr)
    if err:
        parts.append(f"captured stderr before timeout:\n{err}")
    return "\n".join(parts)
