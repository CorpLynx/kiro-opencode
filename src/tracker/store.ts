import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { KiroUsage } from "../usage.js"

/**
 * Hand-off between the provider (server half), which sees Kiro's per-response credit
 * charges, and the TUI half, which draws them. The two can run in different threads or
 * processes, so they meet on disk: one small JSON file per session plus one per account.
 */

export interface SessionUsage {
  /** Credits Kiro charged for this session's requests so far. */
  credits: number
  /** Number of Kiro responses recorded. */
  turns: number
  /** Kiro's own context-usage figure from the latest response, 0–100. */
  contextPct?: number
  model?: string
  updated: number
}

export interface AccountSnapshot extends KiroUsage {
  updated: number
}

export function usageDir(env: NodeJS.ProcessEnv = process.env): string {
  return join(env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "opencode", "kiro")
}

const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/
/** Kiro charges fractional credits; keep float noise out of the running total. */
const round4 = (n: number) => Math.round(n * 1e4) / 1e4
const sessionFile = (dir: string, sessionID: string) =>
  SAFE_ID.test(sessionID) ? join(dir, "sessions", `${sessionID}.json`) : undefined

function readJson<T>(path: string | undefined): T | undefined {
  if (!path) return undefined
  try {
    return JSON.parse(readFileSync(path, "utf8")) as T
  } catch {
    return undefined
  }
}

/** Write via rename so a reader never sees half a file. */
function writeJson(path: string, value: unknown) {
  mkdirSync(join(path, ".."), { recursive: true })
  const tmp = `${path}.${process.pid}.tmp`
  writeFileSync(tmp, JSON.stringify(value))
  renameSync(tmp, path)
}

export function readSession(dir: string, sessionID: string): SessionUsage | undefined {
  return readJson<SessionUsage>(sessionFile(dir, sessionID))
}

export function recordTurn(
  dir: string,
  turn: { sessionID: string; credits?: number; contextPct?: number; model?: string },
  now = Date.now(),
): SessionUsage | undefined {
  const path = sessionFile(dir, turn.sessionID)
  if (!path) return undefined
  const prev = readJson<SessionUsage>(path)
  const next: SessionUsage = {
    credits: round4((prev?.credits ?? 0) + (turn.credits ?? 0)),
    turns: (prev?.turns ?? 0) + 1,
    contextPct: turn.contextPct ?? prev?.contextPct,
    model: turn.model ?? prev?.model,
    updated: now,
  }
  writeJson(path, next)
  return next
}

export function readAccount(dir: string): AccountSnapshot | undefined {
  return readJson<AccountSnapshot>(join(dir, "account.json"))
}

export function writeAccount(dir: string, usage: KiroUsage, now = Date.now()) {
  writeJson(join(dir, "account.json"), { ...usage, updated: now } satisfies AccountSnapshot)
}

/** Drop session files nobody has touched for `maxAgeMs` (default 30 days). */
export function pruneSessions(dir: string, maxAgeMs = 30 * 24 * 3600 * 1000, now = Date.now()) {
  const sessions = join(dir, "sessions")
  if (!existsSync(sessions)) return
  for (const name of readdirSync(sessions)) {
    const path = join(sessions, name)
    try {
      if (now - statSync(path).mtimeMs > maxAgeMs) unlinkSync(path)
    } catch {
      /* raced with another process; fine */
    }
  }
}
