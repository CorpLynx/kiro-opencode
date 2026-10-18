import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { homedir, platform } from "node:os"
import { join } from "node:path"
import { DEFAULT_REGION, normalizeRegion } from "../constants.js"
import type { KiroCredentials } from "./credentials.js"

/**
 * Pick up a session created by the official Kiro tooling so users who already ran
 * `kiro-cli login` (or signed in to the Kiro IDE) don't have to authenticate twice.
 *
 * Sources, in order:
 *  1. Kiro IDE:  ~/.aws/sso/cache/kiro-auth-token.json (+ <clientIdHash>.json for IdC clients)
 *  2. kiro-cli:  SQLite `auth_kv` table (keys `kirocli:odic:token` / `kirocli:odic:device-registration`),
 *                plus the active profile from the `state` table (`api.codewhisperer.profile`)
 */
export interface ImportOptions {
  home?: string
  /** Override the kiro-cli database path (tests, non-standard installs). */
  cliDbPath?: string
}

export async function importKiroCredentials(opts: ImportOptions = {}): Promise<KiroCredentials | undefined> {
  return (await importFromIde(opts)) ?? (await importFromCli(opts))
}

async function readJson(path: string): Promise<any | undefined> {
  try {
    return JSON.parse(await readFile(path, "utf8"))
  } catch {
    return undefined
  }
}

function parseExpiry(v: unknown): number {
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v
  if (typeof v === "string") {
    const t = Date.parse(v)
    if (!Number.isNaN(t)) return t
  }
  return 0 // unknown => treated as expired, which triggers a refresh on first use
}

export async function importFromIde(opts: ImportOptions = {}): Promise<KiroCredentials | undefined> {
  const dir = join(opts.home ?? homedir(), ".aws", "sso", "cache")
  const t = await readJson(join(dir, "kiro-auth-token.json"))
  if (!t?.refreshToken) return undefined

  const isIdc = String(t.authMethod ?? "").toLowerCase() === "idc"
  const creds: KiroCredentials = {
    accessToken: t.accessToken ?? "",
    refreshToken: t.refreshToken,
    expiresAt: parseExpiry(t.expiresAt),
    authMethod: isIdc ? "idc" : "social",
    // For IdC sessions the cached region is the Identity Center (OIDC) region, not the API region.
    region: isIdc ? DEFAULT_REGION : normalizeRegion(t.region),
    ...(isIdc && { oidcRegion: normalizeRegion(t.region) }),
    profileArn: t.profileArn,
  }
  if (isIdc) {
    const hash = t.clientIdHash as string | undefined
    const reg = hash ? await readJson(join(dir, `${hash}.json`)) : undefined
    if (!reg?.clientId || !reg?.clientSecret) return undefined
    creds.clientId = reg.clientId
    creds.clientSecret = reg.clientSecret
  }
  return creds
}

/** Where kiro-cli keeps its SQLite store: the OS's local data directory, as Rust's `dirs` crate resolves it. */
export function cliDbCandidates(
  home: string,
  os: NodeJS.Platform = platform(),
  env: NodeJS.ProcessEnv = process.env,
): string[] {
  const file = (dir: string) => join(dir, "kiro-cli", "data.sqlite3")
  if (os === "darwin") return [file(join(home, "Library", "Application Support"))]
  if (os === "win32") {
    return [
      file(env.LOCALAPPDATA || join(home, "AppData", "Local")),
      file(env.APPDATA || join(home, "AppData", "Roaming")),
    ]
  }
  return [...(env.XDG_DATA_HOME ? [file(env.XDG_DATA_HOME)] : []), file(join(home, ".local", "share"))]
}

/** Read keys from one table of kiro-cli's SQLite store using whichever built-in driver the runtime has. */
async function readCliRows(path: string, table: "auth_kv" | "state", keys: string[]): Promise<Record<string, string>> {
  const sql = `SELECT key, value FROM ${table} WHERE key IN (${keys.map(() => "?").join(",")})`
  const collect = (rows: { key: string; value: string }[]) => Object.fromEntries(rows.map((r) => [r.key, r.value]))
  const dynamicImport = (m: string) => import(/* @vite-ignore */ m)

  try {
    const { Database } = await dynamicImport("bun:sqlite")
    const db = new Database(path, { readonly: true })
    try {
      return collect(db.query(sql).all(...keys))
    } finally {
      db.close()
    }
  } catch {
    /* not running under bun */
  }
  try {
    const { DatabaseSync } = await dynamicImport("node:sqlite")
    const db = new DatabaseSync(path, { readOnly: true })
    try {
      return collect(db.prepare(sql).all(...keys) as any)
    } finally {
      db.close()
    }
  } catch {
    return {}
  }
}

export async function importFromCli(opts: ImportOptions = {}): Promise<KiroCredentials | undefined> {
  const home = opts.home ?? homedir()
  const path = opts.cliDbPath ?? cliDbCandidates(home).find((p) => existsSync(p))
  if (!path || !existsSync(path)) return undefined

  const TOKEN = "kirocli:odic:token"
  const REG = "kirocli:odic:device-registration"
  const rows = await readCliRows(path, "auth_kv", [TOKEN, REG])
  if (!rows[TOKEN]) return undefined

  let token: any, reg: any
  try {
    token = JSON.parse(rows[TOKEN])
    reg = rows[REG] ? JSON.parse(rows[REG]) : undefined
  } catch {
    return undefined
  }
  if (!token?.refresh_token || !reg?.client_id || !reg?.client_secret) return undefined

  const profileArn = await readCliProfileArn(path)
  return {
    accessToken: token.access_token ?? "",
    refreshToken: token.refresh_token,
    expiresAt: parseExpiry(token.expires_at),
    authMethod: "idc",
    region: DEFAULT_REGION, // API region; apiRegion() prefers the profile ARN's region
    oidcRegion: normalizeRegion(reg.region ?? token.region),
    clientId: reg.client_id,
    clientSecret: reg.client_secret,
    profileArn,
  }
}

/** The profile selected with `kiro-cli profile` (needed for IAM Identity Center sessions). */
async function readCliProfileArn(path: string): Promise<string | undefined> {
  const KEY = "api.codewhisperer.profile"
  try {
    const parsed = JSON.parse((await readCliRows(path, "state", [KEY]))[KEY] ?? "null")
    const arn = parsed?.arn ?? parsed?.profileArn ?? parsed?.profile_arn
    return typeof arn === "string" && arn.trim() ? arn.trim() : undefined
  } catch {
    return undefined
  }
}
