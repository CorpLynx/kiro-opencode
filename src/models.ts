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
 * Fallback catalogue used before (or without) live discovery. Ids are Kiro's own model ids,
 * so the id opencode sends is exactly the `modelId` Kiro expects.
 */
export const DEFAULT_MODELS: Record<string, ModelConfig> = {
  auto: claude("Kiro Auto", 32_000),
  "claude-sonnet-4.5": claude("Claude Sonnet 4.5 (Kiro)", 64_000),
  "claude-sonnet-4": claude("Claude Sonnet 4 (Kiro)", 64_000),
  "claude-haiku-4.5": claude("Claude Haiku 4.5 (Kiro)", 64_000),
  "claude-opus-4.5": claude("Claude Opus 4.5 (Kiro)", 32_000),
  "deepseek-3.2": textOnly("DeepSeek 3.2 (Kiro)", 128_000, 32_000),
  "glm-5": textOnly("GLM 5 (Kiro)", 128_000, 32_000),
  "minimax-m2.5": textOnly("MiniMax M2.5 (Kiro)", 128_000, 32_000),
  "qwen3-coder-next": textOnly("Qwen3 Coder Next (Kiro)", 128_000, 32_000),
}

/** Context window to assume when converting Kiro's usage percentage into tokens. */
export function contextWindowFor(modelId: string, models: Record<string, ModelConfig> = DEFAULT_MODELS): number {
  return models[modelId]?.limit.context ?? 200_000
}
