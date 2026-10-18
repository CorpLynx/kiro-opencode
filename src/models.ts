import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { apiRegion, type KiroCredentials } from "./auth/credentials.js"
import { apiEndpoint, KIRO_VERSION } from "./constants.js"

/** Model definition in the shape opencode's `provider.<id>.models` config expects. */
export interface ModelConfig {
  name: string
  attachment: boolean
  reasoning: boolean
  temperature: boolean
  tool_call: boolean
  limit: { context: number; output: number }
  /** Kiro bills in credits, not per token, so token pricing is reported as zero. */
  cost: { input: number; output: number; cache_read?: number; cache_write?: number }
  modalities: { input: string[]; output: string[] }
}

const FREE = { input: 0, output: 0, cache_read: 0, cache_write: 0 }

const claude = (name: string, output: number, context = 200_000): ModelConfig => ({
  name,
  attachment: true,
  reasoning: true,
  temperature: false,
  tool_call: true,
  limit: { context, output },
  cost: FREE,
  modalities: { input: ["text", "image"], output: ["text"] },
})

const textOnly = (name: string, context: number, output: number, reasoning = false): ModelConfig => ({
  name,
  attachment: false,
  reasoning,
  temperature: false,
  tool_call: true,
  limit: { context, output },
  cost: FREE,
  modalities: { input: ["text"], output: ["text"] },
})

/**
 * Fallback catalogue, used until live discovery has run (see `discoverModels`). Ids are Kiro's
 * own model ids, so the id opencode sends is exactly the `modelId` Kiro expects. Limits here are
 * conservative guesses; discovery replaces them with the account's real ones.
 */
export const DEFAULT_MODELS: Record<string, ModelConfig> = {
  auto: claude("Kiro Auto", 32_000),
  "claude-opus-5.5": claude("Claude Opus 5.5 (Kiro)", 32_000),
  "claude-opus-5": claude("Claude Opus 5 (Kiro)", 32_000),
  "claude-sonnet-5": claude("Claude Sonnet 5 (Kiro)", 64_000),
  "claude-sonnet-4.5": claude("Claude Sonnet 4.5 (Kiro)", 64_000),
  "claude-haiku-4.5": claude("Claude Haiku 4.5 (Kiro)", 64_000),
  "deepseek-3.2": textOnly("DeepSeek 3.2 (Kiro)", 128_000, 32_000),
  "minimax-m2.5": textOnly("MiniMax M2.5 (Kiro)", 128_000, 32_000),
  "qwen3-coder-next": textOnly("Qwen3 Coder Next (Kiro)", 128_000, 32_000),
}

/** Context window to assume when converting Kiro's usage percentage into tokens. */
export function contextWindowFor(modelId: string, models: Record<string, ModelConfig> = DEFAULT_MODELS): number {
  return models[modelId]?.limit.context ?? 200_000
}

/* ---------- Live discovery ---------- */

/** Thinking variants apply to Kiro's Claude models (and `auto`, which routes to them). */
const supportsThinking = (id: string) => id === "auto" || id.startsWith("claude")

/** Turn one `ListAvailableModels` entry into opencode's model format; `undefined` if unusable. */
export function modelFromListing(entry: unknown): [string, ModelConfig] | undefined {
  const m: any = typeof entry === "string" ? { modelId: entry } : entry
  const id = typeof m?.modelId === "string" ? m.modelId.trim() : typeof m?.id === "string" ? m.id.trim() : ""
  if (!id) return undefined
  const known = DEFAULT_MODELS[id]
  const base = known ?? (supportsThinking(id) ? claude(id, 32_000) : textOnly(id, 128_000, 32_000))
  const inputs: unknown = m.supportedInputTypes
  const image = Array.isArray(inputs) ? inputs.includes("IMAGE") : base.attachment
  const positive = (n: unknown) => (typeof n === "number" && Number.isFinite(n) && n > 0 ? n : undefined)
  const label = typeof m.modelName === "string" && m.modelName.trim() ? m.modelName.trim() : undefined
  return [
    id,
    {
      ...base,
      name: label ? `${label} (Kiro)` : (known?.name ?? id),
      reasoning: supportsThinking(id),
      attachment: image,
      modalities: { input: image ? ["text", "image"] : ["text"], output: ["text"] },
      limit: {
        context: positive(m.tokenLimits?.maxInputTokens) ?? base.limit.context,
        output: positive(m.tokenLimits?.maxOutputTokens) ?? base.limit.output,
      },
    },
  ]
}

/**
 * Ask Kiro which models this account can use (`GET /ListAvailableModels?origin=AI_EDITOR`,
 * paginated with `nextToken`). Throws on failure; callers keep their current catalogue.
 */
export async function discoverModels(
  creds: KiroCredentials,
  opts: { fetch?: typeof fetch; signal?: AbortSignal; maxPages?: number } = {},
): Promise<Record<string, ModelConfig>> {
  const f = opts.fetch ?? fetch
  const models: Record<string, ModelConfig> = {}
  let next: string | undefined
  for (let page = 0; page < (opts.maxPages ?? 5); page++) {
    const url = new URL(`${apiEndpoint(apiRegion(creds))}/ListAvailableModels`)
    url.searchParams.set("origin", "AI_EDITOR")
    if (creds.profileArn) url.searchParams.set("profileArn", creds.profileArn)
    if (next) url.searchParams.set("nextToken", next)
    const res = await f(url, {
      signal: opts.signal,
      headers: {
        authorization: `Bearer ${creds.accessToken}`,
        accept: "application/json",
        "user-agent": `KiroIDE-${KIRO_VERSION}`,
        "x-amzn-kiro-agent-mode": "vibe",
      },
    })
    const text = await res.text()
    if (!res.ok) throw new Error(`ListAvailableModels failed (HTTP ${res.status}): ${text.slice(0, 200)}`)
    const data: any = JSON.parse(text)
    const list = Array.isArray(data?.models) ? data.models : Array.isArray(data?.availableModels) ? data.availableModels : undefined
    if (!list) throw new Error("ListAvailableModels returned no model list")
    for (const entry of list) {
      const parsed = modelFromListing(entry)
      if (parsed && !models[parsed[0]]) models[parsed[0]] = parsed[1]
    }
    next = typeof data.nextToken === "string" && data.nextToken ? data.nextToken : undefined
    if (!next) break
  }
  if (!Object.keys(models).length) throw new Error("ListAvailableModels returned no usable models")
  return models
}

/* ---------- Cache (so the next launch shows the account's real models) ---------- */

export interface ModelCache {
  updated: number
  models: Record<string, ModelConfig>
}

const cacheFile = (dir: string) => join(dir, "models.json")

export function readModelCache(dir: string): ModelCache | undefined {
  try {
    const cache = JSON.parse(readFileSync(cacheFile(dir), "utf8")) as ModelCache
    return cache && typeof cache.models === "object" && Object.keys(cache.models).length ? cache : undefined
  } catch {
    return undefined
  }
}

export function writeModelCache(dir: string, models: Record<string, ModelConfig>, now = Date.now()) {
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  const tmp = `${cacheFile(dir)}.${process.pid}.tmp`
  writeFileSync(tmp, JSON.stringify({ updated: now, models } satisfies ModelCache))
  renameSync(tmp, cacheFile(dir))
}

/** The catalogue to offer at startup: the account's discovered models, else the built-in list. */
export function catalogue(dir: string): Record<string, ModelConfig> {
  return readModelCache(dir)?.models ?? DEFAULT_MODELS
}

/** Re-discover after this long. */
export const MODEL_CACHE_TTL_MS = 6 * 3600 * 1000

