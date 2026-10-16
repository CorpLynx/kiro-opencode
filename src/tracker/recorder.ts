import type { KiroUsage } from "../usage.js"
import { pruneSessions, recordTurn, usageDir, writeAccount } from "./store.js"

export interface RecorderOptions {
  fetchAccount: () => Promise<KiroUsage>
  dir?: string
  log?: (level: "warn", message: string, extra?: Record<string, unknown>) => void
  /** Minimum gap between account refreshes. */
  accountIntervalMs?: number
  now?: () => number
}

/** Server-side writer for the usage files the TUI tracker reads. Never throws. */
export class UsageRecorder {
  readonly dir: string
  private lastAccount = -Infinity
  private inflight?: Promise<void>

  constructor(private opts: RecorderOptions) {
    this.dir = opts.dir ?? usageDir()
    try {
      pruneSessions(this.dir)
    } catch {
      /* best effort */
    }
  }

  turn(u: { sessionID?: string; credits: number; contextPct?: number; modelId: string }) {
    if (u.sessionID) {
      try {
        recordTurn(this.dir, { sessionID: u.sessionID, credits: u.credits, contextPct: u.contextPct, model: u.modelId })
      } catch (err) {
        this.opts.log?.("warn", "Could not record Kiro usage", { error: String(err) })
      }
    }
    void this.refreshAccount()
  }

  saveAccount(usage: KiroUsage) {
    try {
      // The tracker only shows plan and credits; keep the account's email out of the file.
      const { email: _email, ...rest } = usage
      writeAccount(this.dir, rest, this.now())
      this.lastAccount = this.now()
    } catch {
      /* best effort */
    }
  }

  /** Re-fetch account credits, at most once per interval and never concurrently. */
  refreshAccount(): Promise<void> {
    if (this.inflight) return this.inflight
    if (this.now() - this.lastAccount < (this.opts.accountIntervalMs ?? 60_000)) return Promise.resolve()
    this.lastAccount = this.now()
    this.inflight = this.opts
      .fetchAccount()
      .then((usage) => this.saveAccount(usage))
      .catch((err) => this.opts.log?.("warn", "Could not refresh Kiro account usage", { error: String(err) }))
      .finally(() => {
        this.inflight = undefined
      })
    return this.inflight
  }

  private now() {
    return this.opts.now?.() ?? Date.now()
  }
}
