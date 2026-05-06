#!/usr/bin/env bun
/**
 * Token estimation aligned with Octo-spork ``token_governor.py``.
 *
 * Uses a conservative **characters ÷ 4** heuristic (typical rough Claude-context scale for
 * English/code mixed text). Not a substitute for provider-side tokenizers.
 */

export function estimateTokens(text: string): number {
  const t = text.trim();
  if (!t.length) return 0;
  return Math.max(1, Math.ceil(t.length / 4));
}

async function main(): Promise<void> {
  const useStdin = Bun.argv.includes("--stdin");
  const rest = Bun.argv.slice(2).filter((a) => a !== "--stdin");
  let payload = rest.join("\n");
  if (useStdin) {
    payload = await new Response(Bun.stdin).text();
  }
  const estimatedTokens = estimateTokens(payload);
  console.log(JSON.stringify({ estimatedTokens, chars: payload.length }));
}

await main();
