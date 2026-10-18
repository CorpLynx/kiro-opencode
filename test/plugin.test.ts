import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest"
import { toStored } from "../src/auth/credentials.js"
import { KiroPlugin } from "../src/index.js"

const input = () => {
  const set = vi.fn(async () => ({}))
  const log = vi.fn(async () => ({}))
  return { input: { client: { auth: { set }, app: { log } } } as any, set, log }
}

// The provider loader refreshes account usage in the background and the tracker writes files:
// keep both inside the sandbox (temp state dir, no real network).
let fetchSpy: MockInstance<typeof fetch>
beforeEach(() => {
  vi.stubEnv("XDG_STATE_HOME", mkdtempSync(join(tmpdir(), "kiro-plugin-state-")))
  fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ usageBreakdownList: [] })))
})
afterEach(() => {
  fetchSpy.mockRestore()
  vi.unstubAllEnvs()
})

describe("KiroPlugin", () => {
  it("registers the provider and models via the config hook", async () => {
    const hooks = await KiroPlugin(input().input)
    const config: any = {}
    await hooks.config!(config)
    expect(config.provider.kiro).toMatchObject({ name: "Kiro", npm: "@ai-sdk/openai-compatible" })
    expect(Object.keys(config.provider.kiro.models)).toContain("claude-sonnet-4.5")
    expect(config.provider.kiro.models["claude-sonnet-4.5"].cost).toEqual({ input: 0, output: 0, cache_read: 0, cache_write: 0 })
  })

  it("lets user config override and extend the defaults", async () => {
    const hooks = await KiroPlugin(input().input)
    const config: any = {
      provider: {
        kiro: {
          options: { timeout: 1000 },
          models: { "claude-sonnet-4.5": { name: "Mine" }, "brand-new": { name: "New" } },
        },
      },
    }
    await hooks.config!(config)
    const k = config.provider.kiro
    expect(k.models["claude-sonnet-4.5"]).toMatchObject({ name: "Mine", tool_call: true })
    expect(k.models["brand-new"]).toEqual({ name: "New" })
    expect(k.options).toMatchObject({ timeout: 1000, baseURL: expect.any(String) })
  })

  it("offers Builder ID, Identity Center, Google, GitHub and import login methods", async () => {
    const hooks = await KiroPlugin(input().input)
    expect(hooks.auth!.provider).toBe("kiro")
    expect(hooks.auth!.methods.map((m) => m.label)).toEqual([
      "AWS Builder ID (browser device login)",
      "AWS IAM Identity Center (organization login)",
      "Google account (browser sign-in, experimental)",
      "GitHub account (browser sign-in, experimental)",
      "Import existing login (Kiro IDE or kiro-cli)",
    ])
    const idc = hooks.auth!.methods[1] as any
    expect(idc.prompts.map((p: any) => p.key)).toEqual(["startUrl", "region", "profileArn"])
    expect(idc.prompts[0].validate("http://insecure")).toBeTruthy()
    expect(idc.prompts[0].validate("https://org.awsapps.com/start")).toBeUndefined()
  })

  it("loader supplies a custom fetch only for OAuth credentials and zeroes costs", async () => {
    const hooks = await KiroPlugin(input().input)
    const provider: any = { models: { m: { cost: { input: 3, output: 15, cache: { read: 1, write: 2 } }, limit: { context: 1 } } } }
    expect(await hooks.auth!.loader!(async () => ({ type: "api", key: "x" }) as any, provider)).toEqual({})
    const auth = { type: "oauth", ...toStored({ accessToken: "a", refreshToken: "r", expiresAt: 9e12, authMethod: "social", region: "us-east-1" }) }
    const opts = await hooks.auth!.loader!(async () => auth as any, provider)
    expect(typeof opts.fetch).toBe("function")
    expect(opts.baseURL).toMatch(/^https:\/\//)
    expect(provider.models.m.cost).toEqual({ input: 0, output: 0, cache: { read: 0, write: 0 } })
  })

  it("import login fails cleanly when no Kiro session exists", async () => {
    const prev = process.env.HOME
    process.env.HOME = "/nonexistent-home-for-test"
    try {
      const { input: i, log } = input()
      const hooks = await KiroPlugin(i)
      const res: any = await (hooks.auth!.methods[4] as any).authorize()
      expect(await res.callback()).toEqual({ type: "failed" })
      expect(log).toHaveBeenCalled()
    } finally {
      process.env.HOME = prev
    }
  })

  it("exposes a kiro_usage tool that works once the provider is loaded", async () => {
    const hooks = await KiroPlugin(input().input)
    const usage = hooks.tool!.kiro_usage!
    const ctx = { abort: new AbortController().signal } as any
    expect(await usage.execute({}, ctx)).toMatchObject({ title: "Kiro usage", output: expect.stringMatching(/not signed in/) })

    const auth = { type: "oauth", ...toStored({ accessToken: "a", refreshToken: "r", expiresAt: 9e12, authMethod: "social", region: "us-east-1" }) }
    await hooks.auth!.loader!(async () => auth as any, { models: {} } as any)
    fetchSpy.mockImplementation(
      async () => new Response(JSON.stringify({ usageBreakdownList: [{ displayName: "Credits", currentUsage: 1, usageLimit: 50 }] })),
    )
    expect((await usage.execute({}, ctx) as any).output).toContain("Credits: 1 / 50 used, 49 left")
    // the tool also refreshes the snapshot the TUI tracker shows
    const { readAccount, usageDir } = await import("../src/tracker/store.js")
    expect(readAccount(usageDir())?.buckets[0]).toMatchObject({ used: 1, limit: 50 })
  })

  it("tags only Kiro requests with the session id", async () => {
    const hooks = await KiroPlugin(input().input)
    const run = async (providerID: string) => {
      const output = { headers: {} as Record<string, string> }
      await hooks["chat.headers"]!({ sessionID: "ses_1", provider: { info: { id: providerID } } } as any, output)
      return output.headers
    }
    expect(await run("kiro")).toEqual({ "x-opencode-kiro-session": "ses_1" })
    expect(await run("anthropic")).toEqual({})
    const off = await KiroPlugin(input().input, { tracker: false })
    const output = { headers: {} as Record<string, string> }
    await off["chat.headers"]!({ sessionID: "s", provider: { info: { id: "kiro" } } } as any, output)
    expect(output.headers).toEqual({})
  })

  it("ships a server entry in opencode's module format", async () => {
    const mod = (await import("../src/server.js")).default
    expect(mod.id).toBe("opencode-kiro-provider")
    expect(mod.server).toBe(KiroPlugin)
    expect("tui" in mod).toBe(false) // a module may export server() or tui(), never both
  })

  it("discovers models after login and offers them from the next launch", async () => {
    fetchSpy.mockImplementation(async (url: any) =>
      String(url).includes("ListAvailableModels")
        ? new Response(JSON.stringify({ models: [{ modelId: "claude-opus-9", modelName: "Claude Opus 9", tokenLimits: { maxInputTokens: 500_000 } }] }))
        : new Response(JSON.stringify({ usageBreakdownList: [] })),
    )
    const hooks = await KiroPlugin(input().input)
    const auth = { type: "oauth", ...toStored({ accessToken: "a", refreshToken: "r", expiresAt: 9e12, authMethod: "social", region: "us-east-1" }) }
    await hooks.auth!.loader!(async () => auth as any, { models: {} } as any)
    const { readModelCache } = await import("../src/models.js")
    const { usageDir } = await import("../src/tracker/store.js")
    await vi.waitFor(() => expect(readModelCache(usageDir())).toBeDefined())

    const next = await KiroPlugin(input().input) // a later launch
    const config: any = {}
    await next.config!(config)
    expect(Object.keys(config.provider.kiro.models)).toEqual(["claude-opus-9"])
    expect(config.provider.kiro.models["claude-opus-9"]).toMatchObject({ name: "Claude Opus 9 (Kiro)", limit: { context: 500_000 } })

    const off = await KiroPlugin(input().input, { modelDiscovery: false })
    const config2: any = {}
    await off.config!(config2)
    expect(Object.keys(config2.provider.kiro.models)).toContain("claude-sonnet-5")
  })

  it("keeps thinking off for opencode's background title requests", async () => {
    const hooks = await KiroPlugin(input().input)
    const run = async (agent: string, providerID = "kiro") => {
      const output = { temperature: 0, topP: 1, topK: 0, maxOutputTokens: undefined, options: { reasoningEffort: "low", other: 1 } as Record<string, any> }
      await hooks["chat.params"]!({ agent, provider: { info: { id: providerID } } } as any, output)
      return output.options
    }
    expect(await run("title")).toEqual({ other: 1 })
    expect(await run("build")).toEqual({ reasoningEffort: "low", other: 1 }) // a user's chosen variant stands
    expect(await run("title", "openai")).toEqual({ reasoningEffort: "low", other: 1 })
  })
})
