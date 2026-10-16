import { apiEndpoint, KIRO_VERSION } from "./constants.js"
import { apiRegion, type KiroCredentials } from "./auth/credentials.js"

export interface UsageBucket {
  name: string
  used: number
  limit: number
  resetsAt?: string
}

export interface KiroUsage {
  email?: string
  plan?: string
  buckets: UsageBucket[]
}

const num = (...vals: unknown[]) => {
  for (const v of vals) if (typeof v === "number" && Number.isFinite(v)) return v
  return undefined
}

/** Kiro sends dates as epoch seconds, epoch milliseconds or ISO strings depending on the field. */
function date(v: unknown): string | undefined {
  if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v).toISOString().slice(0, 10)
  if (typeof v === "string" && !Number.isNaN(Date.parse(v))) return new Date(v).toISOString().slice(0, 10)
  return undefined
}

export function parseUsage(data: any): KiroUsage {
  const buckets: UsageBucket[] = []
  for (const b of Array.isArray(data?.usageBreakdownList) ? data.usageBreakdownList : []) {
    const name = String(b.displayName ?? b.resourceType ?? "Usage")
    const used = num(b.currentUsageWithPrecision, b.currentUsage)
    const limit = num(b.usageLimitWithPrecision, b.usageLimit)
    if (used !== undefined && limit !== undefined) {
      buckets.push({ name, used, limit, resetsAt: date(b.nextDateReset ?? data.nextDateReset) })
    }
    const trial = b.freeTrialInfo
    const tUsed = num(trial?.currentUsageWithPrecision, trial?.currentUsage)
    const tLimit = num(trial?.usageLimitWithPrecision, trial?.usageLimit)
    if (tUsed !== undefined && tLimit !== undefined) {
      buckets.push({ name: `${name} (free trial)`, used: tUsed, limit: tLimit, resetsAt: date(trial.freeTrialExpiry) })
    }
  }
  return {
    email: data?.userInfo?.email,
    plan: data?.subscriptionInfo?.subscriptionTitle ?? data?.subscriptionInfo?.type,
    buckets,
  }
}

export async function fetchUsage(
  creds: KiroCredentials,
  opts: { fetch?: typeof fetch; signal?: AbortSignal } = {},
): Promise<KiroUsage> {
  const url = new URL(`${apiEndpoint(apiRegion(creds))}/getUsageLimits`)
  url.searchParams.set("origin", "AI_EDITOR")
  url.searchParams.set("resourceType", "AGENTIC_REQUEST")
  url.searchParams.set("isEmailRequired", "true")
  if (creds.profileArn) url.searchParams.set("profileArn", creds.profileArn)

  const res = await (opts.fetch ?? fetch)(url, {
    signal: opts.signal,
    headers: {
      authorization: `Bearer ${creds.accessToken}`,
      accept: "application/json",
      "user-agent": `KiroIDE-${KIRO_VERSION}`,
      "x-amzn-kiro-agent-mode": "vibe",
    },
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`Kiro usage request failed (HTTP ${res.status}): ${text.slice(0, 300)}`)
  return parseUsage(JSON.parse(text))
}

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(2))

export function formatUsage(u: KiroUsage): string {
  const who = [u.email, u.plan].filter(Boolean).join(", ")
  const lines = [`Kiro usage${who ? ` (${who})` : ""}:`]
  if (!u.buckets.length) lines.push("- No usage information returned for this account.")
  for (const b of u.buckets) {
    const left = Math.max(0, b.limit - b.used)
    lines.push(
      `- ${b.name}: ${fmt(b.used)} / ${fmt(b.limit)} used, ${fmt(left)} left${b.resetsAt ? ` (resets ${b.resetsAt})` : ""}`,
    )
  }
  return lines.join("\n")
}
