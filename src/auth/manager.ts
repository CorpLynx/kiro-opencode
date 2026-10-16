import { fromStored, isExpired, toStored, type KiroCredentials, type StoredOAuth } from "./credentials.js"
import { KiroAuthError, refreshCredentials } from "./oidc.js"

export interface TokenManagerOptions {
  /** Read the credential opencode currently has stored for this provider. */
  load: () => Promise<{ type: string } & Partial<StoredOAuth>>
  /** Write refreshed credentials back to opencode's auth store. */
  save: (stored: StoredOAuth) => Promise<void>
  fetch?: typeof fetch
  now?: () => number
  /** Fallback profile ARN (e.g. from KIRO_PROFILE_ARN) for sessions that don't carry one. */
  profileArn?: string
}

/**
 * Hands out a valid access token, refreshing and persisting it when needed.
 * Concurrent callers share one in-flight refresh.
 */
export class TokenManager {
  private inflight?: Promise<KiroCredentials>
  /** Freshest credentials we hold, remembered in case persisting them failed. */
  private latest?: { from: string; creds: KiroCredentials }

  constructor(private opts: TokenManagerOptions) {}

  /**
   * @param rejected an access token the API just refused; it is refreshed even if it looks
   * unexpired, unless a newer token has already replaced it.
   */
  async get(rejected?: string): Promise<KiroCredentials> {
    const stored = await this.opts.load()
    if (stored.type !== "oauth" || !stored.refresh || stored.access === undefined) {
      throw new KiroAuthError("Not logged in to Kiro. Run `opencode auth login` and choose Kiro.", "not_logged_in", true)
    }
    const s = stored as StoredOAuth
    let creds = this.latest && this.latest.from === s.refresh ? this.latest.creds : fromStored(s)
    if (!creds.profileArn && this.opts.profileArn) creds = { ...creds, profileArn: this.opts.profileArn }

    const now = this.opts.now?.() ?? Date.now()
    const stale = rejected !== undefined && creds.accessToken === rejected
    if (!stale && !isExpired(creds, now)) return creds

    this.inflight ??= this.refresh(s.refresh, creds).finally(() => {
      this.inflight = undefined
    })
    return this.inflight
  }

  private async refresh(from: string, creds: KiroCredentials): Promise<KiroCredentials> {
    const next = await refreshCredentials(creds, { fetch: this.opts.fetch, now: this.opts.now })
    this.latest = { from, creds: next }
    try {
      const stored = toStored(next)
      await this.opts.save(stored)
      this.latest = { from: stored.refresh, creds: next }
    } catch {
      /* keep serving from memory; the next refresh will try to persist again */
    }
    return next
  }
}
