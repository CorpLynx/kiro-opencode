import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it, vi } from "vitest"
import { apiBase, loadPolicy } from "../src/source.js"

const tmp = () => mkdtempSync(join(tmpdir(), "ghe-policy-"))
const policyJson = { version: 1, name: "baseline", opencode: { share: "disabled" } }
const contents = (raw: unknown, sha = "abc123") =>
  new Response(JSON.stringify({ type: "file", encoding: "base64", sha, content: Buffer.from(JSON.stringify(raw)).toString("base64") }))
const ghe = { host: "github.agency.gov", repo: "platform/opencode-policy" }

describe("apiBase", () => {
  it("knows Server, Cloud-with-data-residency and github.com", () => {
    expect(apiBase("github.agency.gov")).toBe("https://github.agency.gov/api/v3")
    expect(apiBase("https://github.agency.gov/")).toBe("https://github.agency.gov/api/v3")
    expect(apiBase("agency.ghe.com")).toBe("https://api.agency.ghe.com")
    expect(apiBase("github.com")).toBe("https://api.github.com")
    expect(apiBase("http://127.0.0.1:9999")).toBe("http://127.0.0.1:9999/api/v3")
  })
})

describe("loadPolicy", () => {
  it("fetches from GHE with the token, and caches it", async () => {
    const dir = tmp()
    const f = vi.fn(async () => contents(policyJson))
    const loaded = await loadPolicy({ ...ghe, path: "policies/opencode.json", ref: "v3" }, { stateDir: dir, fetch: f as any, env: { OPENCODE_POLICY_TOKEN: "tok" }, ghToken: async () => undefined })
    expect(loaded).toMatchObject({ origin: "ghe", sha: "abc123", detail: "github.agency.gov/platform/opencode-policy/policies/opencode.json@v3" })
    expect(loaded.policy.name).toBe("baseline")
    const [url, init] = f.mock.calls[0] as any
    expect(String(url)).toBe("https://github.agency.gov/api/v3/repos/platform/opencode-policy/contents/policies/opencode.json?ref=v3")
    expect(init.headers.authorization).toBe("Bearer tok")

    // GHE down: the cached copy is used, with a warning
    const down = await loadPolicy(ghe, { stateDir: dir, fetch: (async () => { throw new TypeError("fetch failed") }) as any, ghToken: async () => undefined, env: {} })
    expect(down).toMatchObject({ origin: "cache", sha: "abc123" })
    expect(down.warning).toMatch(/fetch failed; using the copy cached 0h ago/)
  })

  it("falls back to the gh CLI for a token", async () => {
    const f = vi.fn(async () => contents(policyJson))
    await loadPolicy(ghe, { stateDir: tmp(), fetch: f as any, env: {}, ghToken: async (host) => (host === "github.agency.gov" ? "from-gh" : undefined) })
    expect((f.mock.calls[0] as any)[1].headers.authorization).toBe("Bearer from-gh")
  })

  it("never caches or applies an invalid policy", async () => {
    const dir = tmp()
    await loadPolicy(ghe, { stateDir: dir, fetch: (async () => contents(policyJson)) as any, env: {}, ghToken: async () => undefined })
    const bad = await loadPolicy(ghe, { stateDir: dir, fetch: (async () => contents({ version: 9 })) as any, env: {}, ghToken: async () => undefined })
    expect(bad.origin).toBe("cache") // the good copy, not the broken one
    expect(bad.warning).toMatch(/unsupported policy version 9/)
  })

  it("locks down when nothing is available, unless told to fail open", async () => {
    const offline = { stateDir: tmp(), fetch: (async () => new Response("nope", { status: 404 })) as any, env: {}, ghToken: async () => undefined }
    const closed = await loadPolicy(ghe, offline)
    expect(closed.origin).toBe("lockdown")
    expect(closed.warning).toMatch(/HTTP 404.*check the token.*locked down/)
    const open = await loadPolicy({ ...ghe, failMode: "open" }, offline)
    expect(open).toMatchObject({ origin: "none", policy: { version: 1 } })
  })

  it("ignores a cache older than maxStaleDays", async () => {
    const dir = tmp()
    let t = 0
    await loadPolicy(ghe, { stateDir: dir, fetch: (async () => contents(policyJson)) as any, env: {}, ghToken: async () => undefined, now: () => t })
    t = 8 * 24 * 3600 * 1000
    const stale = await loadPolicy(ghe, { stateDir: dir, fetch: (async () => { throw new Error("down") }) as any, env: {}, ghToken: async () => undefined, now: () => t })
    expect(stale.origin).toBe("lockdown")
  })

  it("reads a local policy file instead of GHE when one is configured", async () => {
    const file = join(tmp(), "policy.json")
    writeFileSync(file, JSON.stringify(policyJson))
    const f = vi.fn()
    expect(await loadPolicy({ ...ghe, file }, { stateDir: tmp(), fetch: f as any })).toMatchObject({ origin: "file", detail: file })
    expect(f).not.toHaveBeenCalled()
    writeFileSync(file, "{ not json")
    expect((await loadPolicy({ file }, { stateDir: tmp() })).origin).toBe("lockdown")
  })

  it("reports a missing source", async () => {
    expect((await loadPolicy({ failMode: "open" }, { stateDir: tmp() })).origin).toBe("none")
    expect((await loadPolicy({}, { stateDir: tmp() })).origin).toBe("lockdown")
  })
})
