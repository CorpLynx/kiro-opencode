import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { DatabaseSync } from "node:sqlite"
import { describe, expect, it, vi } from "vitest"
import { decodeRefresh, encodeRefresh, fromStored, isExpired, toStored, type KiroCredentials } from "../src/auth/credentials.js"
import { importFromCli, importFromIde } from "../src/auth/import.js"
import { TokenManager } from "../src/auth/manager.js"
import { KiroAuthError, pollDeviceToken, refreshCredentials, startDeviceAuthorization } from "../src/auth/oidc.js"

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const creds = (over: Partial<KiroCredentials> = {}): KiroCredentials => ({
  accessToken: "acc",
  refreshToken: "ref",
  expiresAt: 10_000_000,
  authMethod: "idc",
  region: "eu-central-1",
  clientId: "cid",
  clientSecret: "sec",
  profileArn: "arn:aws:codewhisperer:eu-central-1:1:profile/P",
  ...over,
})

describe("credentials", () => {
  it("round-trips through the opencode auth store format", () => {
    const c = creds({ oidcRegion: "us-east-1" })
    expect(fromStored(toStored(c))).toEqual(c)
  })
  it("treats a bare refresh token as a social session and rejects bad regions", () => {
    expect(decodeRefresh("plain-token")).toMatchObject({ refreshToken: "plain-token", authMethod: "social", region: "us-east-1" })
    expect(decodeRefresh(encodeRefresh({ ...creds(), region: "nonsense" })).region).toBe("us-east-1")
  })
  it("flags tokens inside the expiry skew as expired", () => {
    expect(isExpired(creds({ expiresAt: 1_000_000 + 60_000 }), 1_000_000)).toBe(true)
    expect(isExpired(creds({ expiresAt: 1_000_000 + 10 * 60_000 }), 1_000_000)).toBe(false)
  })
})

describe("device flow", () => {
  it("registers a client, requests a code and polls through pending/slow_down", async () => {
    const calls: string[] = []
    const replies = [
      json({ clientId: "cid", clientSecret: "sec" }),
      json({ deviceCode: "dc", userCode: "ABCD-1234", verificationUri: "https://v", verificationUriComplete: "https://v?c=ABCD-1234", interval: 1, expiresIn: 600 }),
      json({ error: "authorization_pending" }, 400),
      json({ error: "slow_down" }, 400),
      json({ accessToken: "at", refreshToken: "rt", expiresIn: 3600 }),
    ]
    const f = vi.fn(async (url: any, init: any) => {
      calls.push(String(url))
      return replies.shift()!
    }) as unknown as typeof fetch

    const auth = await startDeviceAuthorization({ fetch: f, region: "us-west-2", startUrl: "https://org.awsapps.com/start" })
    expect(auth.userCode).toBe("ABCD-1234")
    expect(calls[0]).toBe("https://oidc.us-west-2.amazonaws.com/client/register")
    expect(JSON.parse((f as any).mock.calls[1][1].body).startUrl).toBe("https://org.awsapps.com/start")

    const waits: number[] = []
    let t = 0
    const result = await pollDeviceToken(auth, { fetch: f, sleep: async (ms) => void waits.push(ms), now: () => (t += 1) })
    expect(waits).toEqual([1000, 1000, 6000]) // slow_down adds 5s
    expect(result).toMatchObject({ accessToken: "at", refreshToken: "rt", authMethod: "idc", clientId: "cid", region: "us-west-2" })
  })

  it("stops on denial and expiry", async () => {
    const auth = { deviceCode: "d", clientId: "c", clientSecret: "s", intervalSec: 1, expiresInSec: 600, region: "us-east-1" } as any
    for (const error of ["access_denied", "expired_token"]) {
      await expect(pollDeviceToken(auth, { fetch: (async () => json({ error }, 400)) as any, sleep: async () => {} })).rejects.toMatchObject({ fatal: true })
    }
  })
})

describe("refreshCredentials", () => {
  it("uses the OIDC token endpoint with client credentials for idc sessions", async () => {
    const f = vi.fn(async () => json({ accessToken: "new", refreshToken: "rot", expiresIn: 100 }))
    const out = await refreshCredentials(creds({ oidcRegion: "us-east-1" }), { fetch: f as any, now: () => 5 })
    const [url, init] = f.mock.calls[0] as any
    expect(url).toBe("https://oidc.us-east-1.amazonaws.com/token")
    expect(JSON.parse(init.body)).toEqual({ grantType: "refresh_token", refreshToken: "ref", clientId: "cid", clientSecret: "sec" })
    expect(out).toMatchObject({ accessToken: "new", refreshToken: "rot", expiresAt: 100_005 })
  })

  it("uses the Kiro desktop endpoint for social sessions", async () => {
    const f = vi.fn(async () => json({ accessToken: "n", expiresIn: 10, profileArn: "arn:x" }))
    const out = await refreshCredentials(creds({ authMethod: "social", clientId: undefined, clientSecret: undefined, profileArn: undefined }), { fetch: f as any })
    expect((f.mock.calls[0] as any)[0]).toBe("https://prod.eu-central-1.auth.desktop.kiro.dev/refreshToken")
    expect(out.refreshToken).toBe("ref") // unchanged when the server does not rotate it
    expect(out.profileArn).toBe("arn:x")
  })

  it("marks rejected refresh tokens as fatal", async () => {
    await expect(refreshCredentials(creds(), { fetch: (async () => json({ error: "invalid_grant" }, 400)) as any })).rejects.toBeInstanceOf(KiroAuthError)
    await expect(refreshCredentials(creds({ clientId: undefined }))).rejects.toMatchObject({ fatal: true })
  })
})

describe("importers", () => {
  it("reads a Kiro IDE social session", async () => {
    const home = mkdtempSync(join(tmpdir(), "kiro-ide-"))
    const dir = join(home, ".aws", "sso", "cache")
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, "kiro-auth-token.json"), JSON.stringify({ accessToken: "a", refreshToken: "r", expiresAt: "2030-01-01T00:00:00.000Z", authMethod: "social", provider: "Github", region: "us-east-1", profileArn: "arn:p" }))
    expect(await importFromIde({ home })).toMatchObject({ authMethod: "social", accessToken: "a", refreshToken: "r", profileArn: "arn:p", expiresAt: Date.parse("2030-01-01T00:00:00.000Z") })
  })

  it("reads a Kiro IDE IdC session together with its client registration", async () => {
    const home = mkdtempSync(join(tmpdir(), "kiro-ide-"))
    const dir = join(home, ".aws", "sso", "cache")
    mkdirSync(dir, { recursive: true })
    writeFileSync(join(dir, "kiro-auth-token.json"), JSON.stringify({ accessToken: "a", refreshToken: "r", expiresAt: "2030-01-01T00:00:00Z", authMethod: "IdC", region: "eu-west-1", clientIdHash: "abc" }))
    expect(await importFromIde({ home })).toBeUndefined() // registration file missing
    writeFileSync(join(dir, "abc.json"), JSON.stringify({ clientId: "cid", clientSecret: "sec" }))
    expect(await importFromIde({ home })).toMatchObject({ authMethod: "idc", clientId: "cid", clientSecret: "sec", region: "eu-west-1" })
  })

  it("reads a kiro-cli SQLite session and its selected profile", async () => {
    const home = mkdtempSync(join(tmpdir(), "kiro-cli-"))
    const path = join(home, "data.sqlite3")
    const db = new DatabaseSync(path)
    db.exec("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
    const put = (t: string, k: string, v: unknown) => db.prepare(`INSERT INTO ${t} VALUES (?, ?)`).run(k, JSON.stringify(v))
    put("auth_kv", "kirocli:odic:token", { access_token: "a", refresh_token: "r", expires_at: "2030-01-01T00:00:00Z", region: "us-east-1" })
    put("auth_kv", "kirocli:odic:device-registration", { client_id: "cid", client_secret: "sec", region: "us-east-1" })
    put("state", "api.codewhisperer.profile", { arn: "arn:aws:codewhisperer:us-east-1:1:profile/P", profile_name: "p" })
    db.close()
    expect(await importFromCli({ home, cliDbPath: path })).toMatchObject({ authMethod: "idc", accessToken: "a", clientId: "cid", clientSecret: "sec", profileArn: "arn:aws:codewhisperer:us-east-1:1:profile/P" })
    expect(await importFromCli({ home, cliDbPath: join(home, "missing.sqlite3") })).toBeUndefined()
  })
})

describe("TokenManager", () => {
  const setup = (initial: KiroCredentials, refreshed = creds({ accessToken: "fresh", expiresAt: 9e12 })) => {
    let stored = { type: "oauth", ...toStored(initial) }
    const save = vi.fn(async (s: any) => void (stored = { type: "oauth", ...s }))
    const f = vi.fn(async () => json({ accessToken: refreshed.accessToken, refreshToken: "rot", expiresIn: 3600 }))
    const mgr = new TokenManager({ load: async () => stored, save, fetch: f as any, now: () => 1_000_000 })
    return { mgr, save, f }
  }

  it("returns valid tokens untouched", async () => {
    const { mgr, f } = setup(creds({ expiresAt: 9e12 }))
    expect((await mgr.get()).accessToken).toBe("acc")
    expect(f).not.toHaveBeenCalled()
  })

  it("refreshes expired tokens once for concurrent callers and persists the result", async () => {
    const { mgr, f, save } = setup(creds({ expiresAt: 1 }))
    const [a, b] = await Promise.all([mgr.get(), mgr.get()])
    expect(a.accessToken).toBe("fresh")
    expect(b.accessToken).toBe("fresh")
    expect(f).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledTimes(1)
    expect(decodeRefresh(save.mock.calls[0]![0].refresh).refreshToken).toBe("rot")
  })

  it("refreshes a rejected token but not one that was already replaced", async () => {
    const { mgr, f } = setup(creds({ expiresAt: 9e12 }))
    await mgr.get("acc") // the API refused "acc"
    expect(f).toHaveBeenCalledTimes(1)
    await mgr.get("acc") // another request that also saw "acc" fail: already replaced
    expect(f).toHaveBeenCalledTimes(1)
  })

  it("keeps working from memory when persisting fails", async () => {
    const { mgr, save, f } = setup(creds({ expiresAt: 1 }))
    save.mockRejectedValue(new Error("disk"))
    await mgr.get()
    expect((await mgr.get()).accessToken).toBe("fresh")
    expect(f).toHaveBeenCalledTimes(1)
  })

  it("fails clearly when not logged in", async () => {
    const mgr = new TokenManager({ load: async () => ({ type: "api" }), save: async () => {} })
    await expect(mgr.get()).rejects.toMatchObject({ code: "not_logged_in" })
  })
})
