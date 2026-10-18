import { execFile } from "node:child_process"
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { LOCKDOWN_POLICY, parsePolicy, type Policy } from "./policy.js"

export interface SourceOptions {
  /** GHE host: `github.agency.gov` (Server), `agency.ghe.com` (Cloud), `github.com`, or a full URL. */
  host?: string
  /** `owner/repo` holding the policy. */
  repo?: string
  path?: string
  ref?: string
  /** A local policy file (e.g. pushed by device management). Used instead of the repo when set. */
  file?: string
  /** What to do when no policy can be loaded: `closed` (lock down, default) or `open` (no policy). */
  failMode?: "open" | "closed"
  /** How old a cached copy may be when GHE is unreachable. */
  maxStaleDays?: number
  timeoutMs?: number
}

export interface LoadedPolicy {
  policy: Policy
  origin: "file" | "ghe" | "cache" | "lockdown" | "none"
  /** Human-readable description of where the policy came from. */
  detail: string
  /** Blob sha of the policy file in GHE, when known. */
  sha?: string
  /** Problem worth surfacing (stale cache, fallback). */
  warning?: string
}

export interface LoadContext {
  stateDir?: string
  env?: NodeJS.ProcessEnv
  fetch?: typeof fetch
  /** Resolves a token for the host from the `gh` CLI; injectable for tests. */
  ghToken?: (host: string) => Promise<string | undefined>
  now?: () => number
}

export function stateDir(env: NodeJS.ProcessEnv = process.env): string {
  return join(env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "opencode", "ghe-policy")
}

/** REST API root for a GitHub host. */
export function apiBase(host: string): string {
  const url = new URL(/^https?:\/\//.test(host) ? host : `https://${host}`)
  const h = url.host.toLowerCase()
  if (h === "github.com" || h === "api.github.com") return "https://api.github.com"
  if (h.endsWith(".ghe.com")) return `${url.protocol}//api.${h.replace(/^api\./, "")}` // GHE Cloud with data residency
  return `${url.protocol}//${h}/api/v3` // GHE Server
}

const hostname = (host: string) => new URL(/^https?:\/\//.test(host) ? host : `https://${host}`).host

/** `gh auth token --hostname <host>`, if the GitHub CLI is installed and signed in. */
export const ghCliToken = (host: string) =>
  new Promise<string | undefined>((resolve) => {
    execFile("gh", ["auth", "token", "--hostname", hostname(host)], { timeout: 3000 }, (err, stdout) =>
      resolve(err ? undefined : stdout.trim() || undefined),
    )
  })

async function token(host: string, ctx: LoadContext): Promise<string | undefined> {
  const env = ctx.env ?? process.env
  return env.OPENCODE_POLICY_TOKEN || env.GH_ENTERPRISE_TOKEN || (await (ctx.ghToken ?? ghCliToken)(host))
}

interface CacheEntry {
  raw: unknown
  sha?: string
  fetchedAt: number
  detail: string
}

const cacheFile = (dir: string) => join(dir, "policy-cache.json")

function readCache(dir: string): CacheEntry | undefined {
  try {
    return JSON.parse(readFileSync(cacheFile(dir), "utf8")) as CacheEntry
  } catch {
    return undefined
  }
}

function writeCache(dir: string, entry: CacheEntry) {
  try {
    mkdirSync(dir, { recursive: true })
    const tmp = `${cacheFile(dir)}.${process.pid}.tmp`
    writeFileSync(tmp, JSON.stringify(entry))
    renameSync(tmp, cacheFile(dir))
  } catch {
    /* the cache is an optimisation for outages; never fatal */
  }
}

async function fetchFromGhe(opts: SourceOptions, ctx: LoadContext): Promise<{ raw: unknown; sha?: string; detail: string }> {
  const host = opts.host!
  const repo = opts.repo!
  const path = opts.path ?? "opencode-policy.json"
  const ref = opts.ref ?? "main"
  const auth = await token(host, ctx)
  const url = new URL(`${apiBase(host)}/repos/${repo}/contents/${path.split("/").map(encodeURIComponent).join("/")}`)
  url.searchParams.set("ref", ref)
  const res = await (ctx.fetch ?? fetch)(url, {
    headers: {
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "opencode-ghe-policy",
      ...(auth && { authorization: `Bearer ${auth}` }),
    },
    signal: AbortSignal.timeout(opts.timeoutMs ?? 5000),
  })
  const body = await res.text()
  if (!res.ok) {
    const hint = res.status === 401 || res.status === 404 ? " (check the token: set OPENCODE_POLICY_TOKEN or run `gh auth login`)" : ""
    throw new Error(`GHE returned HTTP ${res.status} for ${repo}/${path}@${ref}${hint}`)
  }
  const data = JSON.parse(body)
  if (data?.type !== "file" || typeof data.content !== "string") throw new Error(`${repo}/${path} is not a file`)
  const raw = JSON.parse(Buffer.from(data.content, data.encoding === "base64" ? "base64" : "utf8").toString("utf8"))
  return { raw, sha: data.sha, detail: `${hostname(host)}/${repo}/${path}@${ref}` }
}

/** Load the policy, from the first source that works. Never throws. */
export async function loadPolicy(opts: SourceOptions, ctx: LoadContext = {}): Promise<LoadedPolicy> {
  const dir = ctx.stateDir ?? stateDir(ctx.env)
  const now = ctx.now ?? Date.now
  const failed = (why: string): LoadedPolicy =>
    opts.failMode === "open"
      ? { policy: { version: 1 }, origin: "none", detail: "no policy", warning: `${why}; continuing without a policy (failMode: open)` }
      : { policy: LOCKDOWN_POLICY, origin: "lockdown", detail: LOCKDOWN_POLICY.name!, warning: `${why}; locked down until a policy can be loaded` }

  if (opts.file) {
    try {
      return { policy: parsePolicy(JSON.parse(readFileSync(opts.file, "utf8"))), origin: "file", detail: opts.file }
    } catch (err) {
      return failed(`policy file ${opts.file} unusable: ${(err as Error).message}`)
    }
  }

  if (!opts.host || !opts.repo) return failed("no policy source configured (set host and repo, or file)")

  try {
    const got = await fetchFromGhe(opts, ctx)
    const policy = parsePolicy(got.raw) // validate before caching: a broken policy never replaces a good one
    writeCache(dir, { raw: got.raw, sha: got.sha, fetchedAt: now(), detail: got.detail })
    return { policy, origin: "ghe", detail: got.detail, sha: got.sha }
  } catch (err) {
    const why = `could not load policy from GHE: ${(err as Error).message}`
    const cached = readCache(dir)
    const maxAge = (opts.maxStaleDays ?? 7) * 24 * 3600 * 1000
    if (cached && now() - cached.fetchedAt <= maxAge) {
      try {
        const age = Math.round((now() - cached.fetchedAt) / 3600_000)
        return {
          policy: parsePolicy(cached.raw),
          origin: "cache",
          detail: cached.detail,
          sha: cached.sha,
          warning: `${why}; using the copy cached ${age}h ago`,
        }
      } catch {
        /* corrupt cache: fall through */
      }
    }
    return failed(why)
  }
}
