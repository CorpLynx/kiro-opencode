import type { AccountSnapshot, SessionUsage } from "./store.js"

export type Level = "ok" | "warn" | "high"

/** Colour band for a context-usage percentage. */
export function level(pct: number): Level {
  return pct >= 85 ? "high" : pct >= 60 ? "warn" : "ok"
}

export function clampPct(pct: number): number {
  return Math.min(100, Math.max(0, Number.isFinite(pct) ? pct : 0))
}

/** `{ filled: "█████", empty: "░░░░░" }` for a percentage, `width` cells wide. */
export function bar(pct: number, width = 12): { filled: string; empty: string } {
  const cells = Math.round((clampPct(pct) / 100) * width)
  // Never show an empty bar for a non-zero value, nor a full one short of 100%.
  const n = pct > 0 && cells === 0 ? 1 : pct < 100 && cells === width ? width - 1 : cells
  return { filled: "█".repeat(n), empty: "░".repeat(width - n) }
}

export function formatPct(pct: number): string {
  const p = clampPct(pct)
  return p > 0 && p < 1 ? "<1%" : `${Math.round(p)}%`
}

export function formatCredits(n: number): string {
  if (n >= 100) return Math.round(n).toLocaleString("en-US")
  return n.toFixed(2)
}

/** The bucket worth headlining: the paid allowance if there is one, else whatever is left. */
export function mainBucket(account: AccountSnapshot | undefined) {
  const buckets = account?.buckets ?? []
  return buckets.find((b) => !/free trial/i.test(b.name)) ?? buckets[0]
}

export function accountLine(account: AccountSnapshot | undefined): string | undefined {
  const b = mainBucket(account)
  if (!b) return undefined
  const left = Math.max(0, b.limit - b.used)
  return `${formatCredits(left)} / ${formatCredits(b.limit)} left`
}

/** Compact form for the prompt row, e.g. `58% · 3.21 cr`. */
export function chipText(session: SessionUsage | undefined): string {
  if (!session) return ""
  const parts: string[] = []
  if (session.contextPct !== undefined) parts.push(formatPct(session.contextPct))
  parts.push(`${formatCredits(session.credits)} cr`)
  return parts.join(" · ")
}
