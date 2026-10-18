import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { mkdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import type { PluginOptions } from "@opencode-ai/plugin"
import { enforce, type Policy } from "./policy.js"
import type { LoadedPolicy, SourceOptions } from "./source.js"

/** Plugin options, falling back to OPENCODE_POLICY_* environment variables (handy for device management). */
export function sourceOptions(options: PluginOptions | undefined, env: NodeJS.ProcessEnv = process.env): SourceOptions {
  const str = (key: string, envKey: string) => (typeof options?.[key] === "string" ? (options[key] as string) : env[envKey] || undefined)
  const failMode = str("failMode", "OPENCODE_POLICY_FAIL_MODE")
  return {
    host: str("host", "OPENCODE_POLICY_HOST"),
    repo: str("repo", "OPENCODE_POLICY_REPO"),
    path: str("path", "OPENCODE_POLICY_PATH"),
    ref: str("ref", "OPENCODE_POLICY_REF"),
    file: str("file", "OPENCODE_POLICY_FILE"),
    failMode: failMode === "open" ? "open" : "closed",
    maxStaleDays: typeof options?.maxStaleDays === "number" ? options.maxStaleDays : undefined,
  }
}

export function git(args: string[], cwd: string): string | undefined {
  try {
    return execFileSync("git", args, { cwd, timeout: 3000, stdio: ["ignore", "pipe", "ignore"] }).toString().trim() || undefined
  } catch {
    return undefined
  }
}

/** Write the policy's instructions where opencode can include them in every session's system prompt. */
function instructionsFile(policy: Policy, dir: string): string | undefined {
  if (!policy.instructions?.length) return undefined
  const text = `# Organization policy${policy.name ? `: ${policy.name}` : ""}\n\n${policy.instructions.join("\n\n")}\n`
  const file = join(dir, `instructions-${createHash("sha256").update(text).digest("hex").slice(0, 12)}.md`)
  mkdirSync(dir, { recursive: true })
  writeFileSync(file, text)
  return file
}

/** Apply a policy to opencode's resolved config, in place. */
export function applyPolicy(config: Record<string, any>, policy: Policy, dir: string): void {
  const merged = enforce(config, policy.opencode ?? {})
  for (const [key, value] of Object.entries(merged)) config[key] = value

  if (policy.mcp?.allow && config.mcp) {
    const allowed = new Set([...policy.mcp.allow, ...Object.keys((policy.opencode?.mcp as object) ?? {})])
    config.mcp = Object.fromEntries(Object.entries(config.mcp as Record<string, unknown>).filter(([name]) => allowed.has(name)))
  }

  const file = instructionsFile(policy, dir)
  if (file) config.instructions = [...new Set([...((config.instructions as string[]) ?? []), file])]
}

export function describe(loaded: LoadedPolicy): string {
  const p = loaded.policy
  const g = p.guard
  const lines = [
    `Policy: ${p.name ?? "(unnamed)"} — from ${loaded.detail}${loaded.sha ? ` (blob ${loaded.sha.slice(0, 12)})` : ""}`,
  ]
  if (loaded.warning) lines.push(`Warning: ${loaded.warning}`)
  const enforced = Object.keys(p.opencode ?? {})
  if (enforced.length) lines.push(`Enforced settings: ${enforced.join(", ")}`)
  if (p.mcp?.allow) lines.push(`MCP servers allowed: ${p.mcp.allow.length ? p.mcp.allow.join(", ") : "none"}`)
  if (g?.protectedBranches?.length) lines.push(`Protected branches: ${g.protectedBranches.join(", ")}`)
  if (g?.blockForcePush) lines.push("Force pushes: blocked")
  if (g?.allowedHosts) lines.push(`Network allowed to: ${g.allowedHosts.join(", ") || "local only"}`)
  if (g?.deny?.length) lines.push(`Custom command rules: ${g.deny.length}`)
  if (p.provenance?.enabled) lines.push(`Commit provenance: ${p.provenance.trailer ?? "Assisted-by"} trailers`)
  if (p.instructions?.length) lines.push(`Instructions: ${p.instructions.length} added to every session`)
  return lines.join("\n")
}
