import type { ProvenancePolicy } from "./policy.js"

/**
 * Commit provenance: every `git commit` the agent runs gets trailers recording that it was
 * AI-assisted, by which model, in which opencode session, e.g.
 *
 *     Assisted-by: opencode (github-copilot/claude-sonnet-5)
 *     Opencode-Session: ses_abc123
 *
 * Trailers are added with `git commit --trailer` (git 2.32+), so the agent's own message is kept.
 */

const SAFE = /^[A-Za-z0-9._:/@-]+$/

/** `git [global opts] commit` — the subcommand itself, not `commit-tree` and friends. */
const GIT_COMMIT = /\bgit((?:\s+(?:-C\s+\S+|-c\s+\S+|--[\w-]+=\S+|--[\w-]+))*)\s+commit(?![\w-])/g

export function trailersFor(
  policy: ProvenancePolicy | undefined,
  info: { model?: string; sessionID?: string },
): string[] {
  if (!policy?.enabled) return []
  const key = policy.trailer ?? "Assisted-by"
  const model = info.model && SAFE.test(info.model) ? info.model : undefined
  const out = [`${key}: opencode${model ? ` (${model})` : ""}`]
  if (policy.session !== false && info.sessionID && SAFE.test(info.sessionID)) out.push(`Opencode-Session: ${info.sessionID}`)
  return out
}

/** Rewrite a shell command so its `git commit`s carry the trailers. Unchanged if none apply. */
export function addTrailers(command: string, trailers: string[]): string {
  if (!trailers.length) return command
  const key = trailers[0]!.slice(0, trailers[0]!.indexOf(":"))
  if (command.includes(`${key}:`)) return command // already stamped (e.g. the agent retried)
  const flags = trailers.map((t) => ` --trailer '${t}'`).join("")
  return command.replace(GIT_COMMIT, (match) => `${match}${flags}`)
}

/** git >= 2.32 understands `commit --trailer`. */
export function supportsTrailers(gitVersion: string | undefined): boolean {
  const m = gitVersion?.match(/(\d+)\.(\d+)/)
  if (!m) return false
  const [major, minor] = [Number(m[1]), Number(m[2])]
  return major > 2 || (major === 2 && minor >= 32)
}
