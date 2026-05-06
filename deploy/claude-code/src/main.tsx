#!/usr/bin/env bun
/**
 * Agent launcher: loads ``.env``, builds **Grounded Evidence** (pytest transcripts + Ruff), then
 * execs ``claude`` with ``--append-system-prompt`` or ``--append-system-prompt-file``.
 *
 * Environment:
 * - ``CLAUDE_CONFIG_DIR`` — directory containing ``.env`` (default ``/app/.local/claude_config``).
 * - ``OCTO_WORKSPACE`` — repository root for evidence (default: ``process.cwd()``).
 * - ``OCTO_SKIP_GROUNDED_EVIDENCE=1`` — do not inject append-system-prompt.
 * - ``OCTO_APPEND_INLINE_MAX`` — max chars for inline flag before switching to file (default 16000).
 * - ``OCTO_SKIP_SIDECAR=1`` — skip automatic ``--add-dir`` for the parent Octo-spork checkout.
 * - ``OCTO_CLAUDE_ALLOWED_TOOLS`` — comma-separated Claude Code tools allowed without prompts
 *   (default ``Read,Grep,Glob``). Set in ``CLAUDE_CONFIG_DIR/.env``; use ``run_agent elevate`` to widen.
 * - ``OCTO_SKIP_ALLOWED_TOOLS=1`` — do not pass ``--allowedTools`` (escape hatch).
 */
import { config } from "dotenv";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

const cfgDir = process.env.CLAUDE_CONFIG_DIR ?? "/app/.local/claude_config";
const envPath = join(cfgDir, ".env");
if (existsSync(envPath)) {
  config({ path: envPath });
  console.info("[claude-code] Loaded env from", envPath);
} else {
  console.warn("[claude-code] No .env at", envPath, "(mount .local/claude_config with your config)");
}

const workspace = process.env.OCTO_WORKSPACE ?? process.cwd();
const inlineMax = Number.parseInt(process.env.OCTO_APPEND_INLINE_MAX ?? "16000", 10) || 16000;
const skipEvidence = process.env.OCTO_SKIP_GROUNDED_EVIDENCE?.trim() === "1";
const skipSidecar = process.env.OCTO_SKIP_SIDECAR?.trim() === "1";
const skipAllowedTools = process.env.OCTO_SKIP_ALLOWED_TOOLS?.trim() === "1";

const DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob";

function allowedToolsArgvFromEnv(): string[] {
  if (skipAllowedTools) return [];
  const raw = (process.env.OCTO_CLAUDE_ALLOWED_TOOLS ?? DEFAULT_ALLOWED_TOOLS).trim();
  const tools = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (tools.length === 0) return [];
  return ["--allowedTools", tools.join(",")];
}

function userSuppliedAllowedTools(argv: string[]): boolean {
  return argv.some((a) => a === "--allowedTools" || a === "--allowed-tools");
}

function listNewestFailureLogs(root: string, limit: number): string[] {
  const dir = join(root, ".octo", "evidence", "pytest_failures");
  if (!existsSync(dir)) return [];
  const entries = readdirSync(dir)
    .filter((n) => n.endsWith(".log") || n.endsWith(".txt"))
    .map((name) => {
      const p = join(dir, name);
      try {
        return { m: statSync(p).mtimeMs, p };
      } catch {
        return null;
      }
    })
    .filter((x): x is { m: number; p: string } => x !== null)
    .sort((a, b) => b.m - a.m);
  return entries.slice(0, limit).map((e) => e.p);
}

function truncate(s: string, max: number): string {
  const t = s.trimEnd();
  if (t.length <= max) return t;
  return `${t.slice(0, max - 80)}\n\n… [truncated]\n`;
}

/** Minimal evidence when Python bridge is unavailable (test logs only). */
function bunFallbackEvidence(root: string): string {
  const logs = listNewestFailureLogs(root, 3);
  const blocks: string[] = [];
  for (let i = 0; i < logs.length; i++) {
    const raw = readFileSync(logs[i], "utf-8");
    const name = logs[i].split(/[/\\]/).pop() ?? "log";
    blocks.push(`#### Failure log ${i + 1}: \`${name}\`\n\n\`\`\`text\n${truncate(raw, 12000)}\n\`\`\``);
  }
  const pytestSection =
    blocks.length > 0
      ? blocks.join("\n\n")
      : "_No failure transcripts under `.octo/evidence/pytest_failures/`._";
  return [
    "## Grounded Evidence",
    "",
    "_Octo-spork evidence injection (Bun fallback — run `python -m claude_bridge.evidence_context` for Ruff)._",
    "",
    "### Last failed test runs (raw transcripts)",
    "",
    pytestSection,
    "",
    "### Critical lint (Ruff)",
    "",
    "_Unavailable in Bun fallback — install Python + Ruff or pre-render with `evidence_context.py`._",
    "",
  ].join("\n");
}

async function octoAddDirArgv(ws: string): Promise<string[]> {
  const pyPath = join(ws, "src", "claude_bridge", "sidecar_context.py");
  if (!existsSync(pyPath)) return [];
  try {
    const proc = Bun.spawn({
      cmd: ["python3", "-m", "claude_bridge.sidecar_context", "--workspace", ws, "--emit", "json"],
      cwd: ws,
      env: { ...process.env, PYTHONPATH: join(ws, "src") },
      stdout: "pipe",
      stderr: "pipe",
    });
    const out = await new Response(proc.stdout).text();
    const code = await proc.exited;
    if (code !== 0 || !out.trim()) return [];
    const data = JSON.parse(out) as { extra?: string[] };
    const extra = Array.isArray(data.extra) ? data.extra : [];
    if (extra.length > 1) {
      console.info("[claude-code] Octo sidecar context:", extra[1]);
    }
    return extra;
  } catch (exc) {
    console.warn("[claude-code] sidecar_context.py failed:", exc);
    return [];
  }
}

async function buildGroundedEvidence(root: string): Promise<string> {
  try {
    const pyModulePath = join(root, "src", "claude_bridge", "evidence_context.py");
    if (existsSync(pyModulePath)) {
      const proc = Bun.spawn({
        cmd: ["python3", "-m", "claude_bridge.evidence_context", "--repo", root],
        cwd: root,
        env: { ...process.env, PYTHONPATH: join(root, "src") },
        stdout: "pipe",
        stderr: "pipe",
      });
      const out = await new Response(proc.stdout).text();
      const err = await new Response(proc.stderr).text();
      const code = await proc.exited;
      if (code === 0 && out.trim()) {
        return out.trim();
      }
      console.warn("[claude-code] evidence_context.py exit", code, err.slice(0, 400));
    }
    return bunFallbackEvidence(root);
  } catch (exc) {
    console.warn("[claude-code] Grounded Evidence build failed; using Bun fallback.", exc);
    return bunFallbackEvidence(root);
  }
}

function userRequestedAppend(argv: string[]): boolean {
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--append-system-prompt" || a === "--append-system-prompt-file") return true;
  }
  return false;
}

function userSuppliedAddDir(argv: string[]): boolean {
  return argv.includes("--add-dir");
}

async function main(): Promise<void> {
  const userArgs = process.argv.slice(2);

  const addDirArgs =
    userSuppliedAddDir(userArgs) || skipSidecar ? [] : await octoAddDirArgv(workspace);

  const allowedToolsArgs =
    userSuppliedAllowedTools(userArgs) ? [] : allowedToolsArgvFromEnv();

  if (userRequestedAppend(userArgs)) {
    console.info("[claude-code] User supplied append-system-prompt; skipping Octo injection.");
    const proc = Bun.spawn({
      cmd: ["claude", ...allowedToolsArgs, ...addDirArgs, ...userArgs],
      stdin: "inherit",
      stdout: "inherit",
      stderr: "inherit",
    });
    process.exit(await proc.exited);
    return;
  }

  let appendArgs: string[] = [];
  if (!skipEvidence) {
    const evidence = await buildGroundedEvidence(workspace);
    if (evidence.length > 0) {
      if (evidence.length > inlineMax) {
        const evidenceDir = join(workspace, ".octo", "evidence");
        try {
          mkdirSync(evidenceDir, { recursive: true });
        } catch {
          /* ignore */
        }
        const fp = join(evidenceDir, ".last_append_system_prompt.md");
        writeFileSync(fp, evidence, "utf-8");
        appendArgs = ["--append-system-prompt-file", fp];
        console.info("[claude-code] Grounded Evidence via file:", fp, `(${evidence.length} chars)`);
      } else {
        appendArgs = ["--append-system-prompt", evidence];
        console.info("[claude-code] Grounded Evidence injected inline.", `(${evidence.length} chars)`);
      }
    }
  } else {
    console.info("[claude-code] OCTO_SKIP_GROUNDED_EVIDENCE=1 — no injection.");
  }

  const proc = Bun.spawn({
    cmd: ["claude", ...allowedToolsArgs, ...addDirArgs, ...userArgs, ...appendArgs],
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
  const exitCode = await proc.exited;
  process.exit(exitCode ?? 0);
}

await main();
