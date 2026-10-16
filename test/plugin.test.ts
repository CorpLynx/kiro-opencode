import { describe, expect, it, vi } from "vitest"
import { toStored } from "../src/auth/credentials.js"
import { KiroPlugin } from "../src/index.js"

const input = () => {
  const set = vi.fn(async () => ({}))
  const log = vi.fn(async () => ({}))
  return { input: { client: { auth: { set }, app: { log } } } as any, set, log }
}

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

  it("offers Builder ID, Identity Center and import login methods", async () => {
    const hooks = await KiroPlugin(input().input)
    expect(hooks.auth!.provider).toBe("kiro")
    expect(hooks.auth!.methods.map((m) => m.type)).toEqual(["oauth", "oauth", "oauth"])
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
      const res: any = await (hooks.auth!.methods[2] as any).authorize()
      expect(await res.callback()).toEqual({ type: "failed" })
      expect(log).toHaveBeenCalled()
    } finally {
      process.env.HOME = prev
    }
  })
})
