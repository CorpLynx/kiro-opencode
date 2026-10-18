import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it, vi } from "vitest"
import type { KiroCredentials } from "../src/auth/credentials.js"
import { catalogue, DEFAULT_MODELS, discoverModels, modelFromListing, readModelCache, writeModelCache } from "../src/models.js"

const creds: KiroCredentials = { accessToken: "tok", refreshToken: "r", expiresAt: 9e12, authMethod: "idc", region: "us-east-1", profileArn: "arn:aws:codewhisperer:eu-central-1:1:profile/P" }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

describe("modelFromListing", () => {
  it("maps Kiro's listing to opencode's model format", () => {
    const [id, m] = modelFromListing({
      modelId: "claude-opus-5.5",
      modelName: "Claude Opus 5.5",
      supportedInputTypes: ["TEXT", "IMAGE"],
      tokenLimits: { maxInputTokens: 1_000_000, maxOutputTokens: 128_000 },
    })!
    expect(id).toBe("claude-opus-5.5")
    expect(m).toMatchObject({
      name: "Claude Opus 5.5 (Kiro)",
      reasoning: true,
      attachment: true,
      tool_call: true,
      limit: { context: 1_000_000, output: 128_000 },
      modalities: { input: ["text", "image"], output: ["text"] },
    })
  })

  it("falls back sensibly for sparse or odd entries", () => {
    expect(modelFromListing("deepseek-3.2")![1]).toMatchObject({ name: "DeepSeek 3.2 (Kiro)", reasoning: false, attachment: false })
    expect(modelFromListing({ modelId: "new-model", supportedInputTypes: ["TEXT"], tokenLimits: { maxInputTokens: -1 } })![1]).toMatchObject({
      name: "new-model",
      attachment: false,
      limit: { context: 128_000 },
    })
    expect(modelFromListing({ id: "x-model" })?.[0]).toBe("x-model")
    expect(modelFromListing({ modelId: "  " })).toBeUndefined()
    expect(modelFromListing(null)).toBeUndefined()
  })
})

describe("discoverModels", () => {
  it("pages through ListAvailableModels in the profile's region", async () => {
    const pages = [
      json({ models: [{ modelId: "claude-opus-5.5" }, { modelId: "auto" }], nextToken: "p2" }),
      json({ models: [{ modelId: "deepseek-3.2" }, { modelId: "auto" }] }),
    ]
    const f = vi.fn(async () => pages.shift()!)
    const models = await discoverModels(creds, { fetch: f as any })
    expect(Object.keys(models)).toEqual(["claude-opus-5.5", "auto", "deepseek-3.2"])
    const [first, init] = f.mock.calls[0] as any
    expect(first.origin + first.pathname).toBe("https://q.eu-central-1.amazonaws.com/ListAvailableModels")
    expect(first.searchParams.get("origin")).toBe("AI_EDITOR")
    expect(first.searchParams.get("profileArn")).toBe(creds.profileArn)
    expect(init.headers.authorization).toBe("Bearer tok")
    expect((f.mock.calls[1] as any)[0].searchParams.get("nextToken")).toBe("p2")
  })

  it("accepts the availableModels key and stops at maxPages", async () => {
    const f = vi.fn(async () => json({ availableModels: ["auto"], nextToken: "again" }))
    expect(Object.keys(await discoverModels(creds, { fetch: f as any, maxPages: 3 }))).toEqual(["auto"])
    expect(f).toHaveBeenCalledTimes(3)
  })

  it("throws on errors and empty answers", async () => {
    await expect(discoverModels(creds, { fetch: (async () => json({ message: "no" }, 403)) as any })).rejects.toThrow(/HTTP 403/)
    await expect(discoverModels(creds, { fetch: (async () => json({ models: [] })) as any })).rejects.toThrow(/no usable/)
    await expect(discoverModels(creds, { fetch: (async () => json({ other: 1 })) as any })).rejects.toThrow(/no model list/)
  })
})

describe("model cache", () => {
  it("round-trips and drives the startup catalogue", () => {
    const dir = mkdtempSync(join(tmpdir(), "kiro-models-"))
    expect(catalogue(dir)).toBe(DEFAULT_MODELS)
    const models = { "claude-opus-5.5": modelFromListing("claude-opus-5.5")![1] }
    writeModelCache(dir, models, 42)
    expect(readModelCache(dir)).toEqual({ updated: 42, models })
    expect(catalogue(dir)).toEqual(models)
  })
})
