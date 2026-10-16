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
  if (n >= 100 || Number.isInteger(n)) return Math.round(n).toLocaleString("en-US")
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

/**
 * Whether the account's main allowance is low enough to warn about. `key` identifies the
 * allowance period, so a warning is shown once per period rather than on every launch.
 */
export function lowCredit(
  account: AccountSnapshot | undefined,
  threshold: number,
): { key: string; left: number; limit: number; empty: boolean; resetsAt?: string } | undefined {
  const b = mainBucket(account)
  if (!b || b.limit <= 0) return undefined
  const left = Math.max(0, b.limit - b.used)
  if (left / b.limit > threshold) return undefined
  return { key: `${b.name}|${b.limit}|${b.resetsAt ?? ""}|${left === 0 ? "empty" : "low"}`, left, limit: b.limit, empty: left === 0, resetsAt: b.resetsAt }
}

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`
}

/** Plain-text body of the "Kiro usage" dialog. */
export function detailText(session: SessionUsage | undefined, account: AccountSnapshot | undefined, now = Date.now()): string {
  // Rows are laid out once all labels are known, so values line up after the longest one.
  const out: (string | [string, string])[] = []
  const row = (label: string, value: string): [string, string] => [label, value]
  if (session) {
    out.push("This session")
    if (session.contextPct !== undefined) out.push(row("Context", `${formatPct(session.contextPct)} of the model's window`))
    out.push(row("Credits", `${formatCredits(session.credits)} over ${session.turns} ${session.turns === 1 ? "request" : "requests"}`))
    if (session.model) out.push(row("Model", session.model))
  } else {
    out.push("No Kiro usage recorded for this session yet.")
  }
  if (account?.buckets.length) {
    out.push("", `Account${account.plan ? ` (${account.plan})` : ""}`)
    for (const b of account.buckets) {
      const left = Math.max(0, b.limit - b.used)
      out.push(row(b.name, `${formatCredits(left)} / ${formatCredits(b.limit)} left${b.resetsAt ? ` · resets ${b.resetsAt}` : ""}`))
    }
    out.push("", `Account figures updated ${ago(now - account.updated)}.`)
  }
  const width = Math.max(0, ...out.filter((r) => Array.isArray(r)).map((r) => r[0].length)) + 2
  return out.map((r) => (Array.isArray(r) ? `  ${r[0].padEnd(width)}${r[1]}` : r)).join("\n")
}
