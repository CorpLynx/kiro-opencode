/**
 * The organization policy: one JSON document, normally kept in a GitHub Enterprise repo so
 * changes go through review. Everything is optional; an empty policy changes nothing.
 *
 * {
 *   "version": 1,
 *   "name": "agency-baseline",
 *   "opencode": { ...any opencode.json keys, enforced over user/project config... },
 *   "mcp": { "allow": ["github"] },
 *   "instructions": ["Rules every agent session must follow…"],
 *   "guard": { "protectedBranches": ["main"], "blockForcePush": true, "allowedHosts": ["*.agency.gov"],
 *              "deny": [{ "pattern": "\\brm\\s+-rf\\s+/", "reason": "…" }] },
 *   "provenance": { "enabled": true, "trailer": "Assisted-by", "session": true }
 * }
 */

export interface DenyRule {
  /** JavaScript regular expression, matched against the whole shell command. */
  pattern: string
  reason: string
}

export interface GuardPolicy {
  /** Branch globs agents may not push to directly (`*` matches within a path segment, `**` across). */
  protectedBranches?: string[]
  blockForcePush?: boolean
  /** If set, network commands and web fetches may only reach these hosts (`*.example.gov` allowed). */
  allowedHosts?: string[]
  deny?: DenyRule[]
}

export interface ProvenancePolicy {
  enabled?: boolean
  /** Trailer key for AI-assisted commits. */
  trailer?: string
  /** Also record the opencode session id (`Opencode-Session:` trailer). */
  session?: boolean
}

export interface Policy {
  version: 1
  name?: string
  /** opencode.json settings that override user and project config. */
  opencode?: Record<string, unknown>
  mcp?: { allow?: string[] }
  instructions?: string[]
  guard?: GuardPolicy
  provenance?: ProvenancePolicy
}

export class PolicyError extends Error {
  override name = "PolicyError"
}

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v)
const strings = (v: unknown, where: string): string[] | undefined => {
  if (v === undefined) return undefined
  if (!Array.isArray(v) || v.some((s) => typeof s !== "string")) throw new PolicyError(`${where} must be a list of strings`)
  return v as string[]
}

/** Validate untrusted JSON into a Policy, with messages that point at the offending field. */
export function parsePolicy(raw: unknown): Policy {
  if (!isObject(raw)) throw new PolicyError("policy must be a JSON object")
  if (raw.version !== 1) throw new PolicyError(`unsupported policy version ${JSON.stringify(raw.version)} (expected 1)`)
  const p: Policy = { version: 1 }
  if (raw.name !== undefined) {
    if (typeof raw.name !== "string") throw new PolicyError("name must be a string")
    p.name = raw.name
  }
  if (raw.opencode !== undefined) {
    if (!isObject(raw.opencode)) throw new PolicyError("opencode must be an object of opencode.json settings")
    if ("plugin" in raw.opencode) throw new PolicyError("opencode.plugin cannot be set by policy (plugins load before policy applies)")
    p.opencode = raw.opencode
  }
  if (raw.mcp !== undefined) {
    if (!isObject(raw.mcp)) throw new PolicyError("mcp must be an object")
    p.mcp = { allow: strings(raw.mcp.allow, "mcp.allow") }
  }
  p.instructions = strings(raw.instructions, "instructions")
  if (raw.guard !== undefined) {
    const g = raw.guard
    if (!isObject(g)) throw new PolicyError("guard must be an object")
    const deny = g.deny === undefined ? undefined : (() => {
      if (!Array.isArray(g.deny)) throw new PolicyError("guard.deny must be a list")
      return g.deny.map((r, i): DenyRule => {
        if (!isObject(r) || typeof r.pattern !== "string" || typeof r.reason !== "string") {
          throw new PolicyError(`guard.deny[${i}] needs a "pattern" and a "reason"`)
        }
        try {
          new RegExp(r.pattern)
        } catch (err) {
          throw new PolicyError(`guard.deny[${i}].pattern is not a valid regular expression: ${(err as Error).message}`)
        }
        return { pattern: r.pattern, reason: r.reason }
      })
    })()
    if (g.blockForcePush !== undefined && typeof g.blockForcePush !== "boolean") throw new PolicyError("guard.blockForcePush must be true or false")
    p.guard = {
      protectedBranches: strings(g.protectedBranches, "guard.protectedBranches"),
      blockForcePush: g.blockForcePush as boolean | undefined,
      allowedHosts: strings(g.allowedHosts, "guard.allowedHosts"),
      deny,
    }
  }
  if (raw.provenance !== undefined) {
    const v = raw.provenance
    if (!isObject(v)) throw new PolicyError("provenance must be an object")
    if (v.trailer !== undefined && (typeof v.trailer !== "string" || !/^[A-Za-z][A-Za-z0-9-]*$/.test(v.trailer))) {
      throw new PolicyError("provenance.trailer must be a trailer key like Assisted-by")
    }
    p.provenance = { enabled: v.enabled !== false, trailer: v.trailer as string | undefined, session: v.session !== false }
  }
  return p
}

/**
 * Used when no policy can be loaded and the plugin is configured to fail closed: no sharing,
 * no updates, no MCP servers, every edit and command needs approval, no web access.
 */
export const LOCKDOWN_POLICY: Policy = {
  version: 1,
  name: "lockdown (policy unavailable)",
  opencode: {
    share: "disabled",
    autoupdate: false,
    permission: { edit: "ask", bash: "ask", webfetch: "deny" },
  },
  mcp: { allow: [] },
  guard: { protectedBranches: ["main", "master"], blockForcePush: true },
  provenance: { enabled: true },
}

/**
 * Deep-merge `over` onto `base`; `over` wins on every key it sets. Objects merge, everything
 * else (including arrays) is replaced, so a policy list is the whole list.
 */
export function enforce(base: Record<string, unknown>, over: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base }
  for (const [key, value] of Object.entries(over)) {
    const current = out[key]
    out[key] = isObject(value) && isObject(current) ? enforce(current, value) : value
  }
  return out
}
