import { randomUUID } from "node:crypto"

/* ---------- OpenAI chat-completions input (the subset opencode sends) ---------- */

type ContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } | string }
  | { type: string; [k: string]: unknown }

export interface OpenAIMessage {
  role: "system" | "developer" | "user" | "assistant" | "tool"
  content?: string | ContentPart[] | null
  tool_calls?: { id: string; type?: string; function: { name: string; arguments?: string } }[]
  tool_call_id?: string
}

export interface OpenAIRequest {
  model: string
  messages: OpenAIMessage[]
  tools?: { type: "function"; function: { name: string; description?: string; parameters?: unknown } }[]
  stream?: boolean
  stream_options?: { include_usage?: boolean }
  reasoning_effort?: string
}

/* ---------- Kiro wire types ---------- */

export interface KiroImage {
  format: string
  source: { bytes: string }
}
export interface KiroToolUse {
  toolUseId: string
  name: string
  input: unknown
}
export interface KiroToolResult {
  toolUseId: string
  status: "success" | "error"
  content: { text: string }[]
}
interface KiroUserInput {
  content: string
  modelId: string
  origin: "AI_EDITOR"
  images?: KiroImage[]
  userInputMessageContext?: {
    tools?: { toolSpecification: { name: string; description: string; inputSchema: { json: unknown } } }[]
    toolResults?: KiroToolResult[]
  }
}
type KiroHistoryItem =
  | { userInputMessage: KiroUserInput }
  | { assistantResponseMessage: { content: string; toolUses?: KiroToolUse[] } }

export interface KiroRequestBody {
  conversationState: {
    chatTriggerType: "MANUAL"
    conversationId: string
    currentMessage: { userInputMessage: KiroUserInput }
    history?: KiroHistoryItem[]
  }
  profileArn?: string
}

/* ---------- Result ---------- */

export interface TransformedRequest {
  body: KiroRequestBody
  /** Names sent to Kiro -> names opencode knows (differs only when we had to sanitize). */
  toolNames: Map<string, string>
  conversationId: string
  modelId: string
  stream: boolean
  /** Whether the model was asked to emit `<thinking>` blocks. */
  thinking: boolean
  /** Rough size of the prompt, used for usage estimates. */
  inputChars: number
}

export interface TransformOptions {
  profileArn?: string
  maxToolResultChars?: number
  conversationId?: string
}

const THINKING_BUDGETS: Record<string, number> = {
  minimal: 2000,
  low: 4000,
  medium: 12000,
  high: 24000,
  xhigh: 32000,
  max: 32000,
}
const MAX_TOOL_DESCRIPTION = 9000
const DEFAULT_MAX_TOOL_RESULT = 400_000

/* ---------- Turn model ---------- */

interface UserTurn {
  role: "user"
  text: string
  images: KiroImage[]
  toolResults: KiroToolResult[]
}
interface AssistantTurn {
  role: "assistant"
  text: string
  toolUses: KiroToolUse[]
}
type Turn = UserTurn | AssistantTurn

const textOf = (c: OpenAIMessage["content"]): string => {
  if (c == null) return ""
  if (typeof c === "string") return c
  return c
    .filter((p): p is { type: "text"; text: string } => p.type === "text" && typeof (p as any).text === "string")
    .map((p) => p.text)
    .join("\n")
}

function imagesOf(c: OpenAIMessage["content"]): KiroImage[] {
  if (!Array.isArray(c)) return []
  const out: KiroImage[] = []
  for (const p of c) {
    if (p.type !== "image_url") continue
    const raw = (p as any).image_url
    const url: string | undefined = typeof raw === "string" ? raw : raw?.url
    const m = url?.match(/^data:image\/([a-z0-9.+-]+);base64,(.+)$/i)
    if (!m) continue // remote URLs can't be forwarded; Kiro only accepts inline bytes
    const fmt = m[1]!.toLowerCase().replace("jpg", "jpeg")
    out.push({ format: fmt, source: { bytes: m[2]! } })
  }
  return out
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s
  const half = Math.floor(max / 2)
  return `${s.slice(0, half)}\n… [${s.length - max} characters truncated] …\n${s.slice(-half)}`
}

function parseArgs(raw: string | undefined): unknown {
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

/** Kiro tool names: [A-Za-z0-9_-]{1,64}. */
function safeToolName(name: string, taken: Map<string, string>): string {
  let safe = name.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || "tool"
  if (taken.has(safe) && taken.get(safe) !== name) {
    let i = 2
    const base = safe.slice(0, 60)
    while (taken.has(`${base}_${i}`)) i++
    safe = `${base}_${i}`
  }
  taken.set(safe, name)
  return safe
}

/** Kiro rejects `$schema` and empty `required` arrays; make sure the root is an object schema. */
function cleanSchema(schema: unknown): unknown {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk)
    if (!node || typeof node !== "object") return node
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(node)) {
      if (k === "$schema") continue
      if (k === "required" && Array.isArray(v) && v.length === 0) continue
      out[k] = walk(v)
    }
    return out
  }
  const cleaned = walk(schema) as Record<string, unknown> | undefined
  if (!cleaned || typeof cleaned !== "object" || Array.isArray(cleaned)) return { type: "object", properties: {} }
  return { type: "object", properties: {}, ...cleaned }
}

/* ---------- Main transform ---------- */

export function transformRequest(req: OpenAIRequest, opts: TransformOptions = {}): TransformedRequest {
  const maxResult = opts.maxToolResultChars ?? DEFAULT_MAX_TOOL_RESULT
  const modelId = req.model
  const conversationId = opts.conversationId ?? randomUUID()

  const systemParts: string[] = []
  const turns: Turn[] = []
  const last = () => turns[turns.length - 1]

  for (const m of req.messages ?? []) {
    switch (m.role) {
      case "system":
      case "developer": {
        const t = textOf(m.content)
        if (t) systemParts.push(t)
        break
      }
      case "user": {
        const text = textOf(m.content)
        const images = imagesOf(m.content)
        const prev = last()
        if (prev?.role === "user") {
          prev.text = [prev.text, text].filter(Boolean).join("\n\n")
          prev.images.push(...images)
        } else turns.push({ role: "user", text, images, toolResults: [] })
        break
      }
      case "tool": {
        const result: KiroToolResult = {
          toolUseId: m.tool_call_id ?? "",
          status: "success",
          content: [{ text: truncate(textOf(m.content) || "(no output)", maxResult) }],
        }
        const prev = last()
        if (prev?.role === "user") prev.toolResults.push(result)
        else turns.push({ role: "user", text: "", images: [], toolResults: [result] })
        break
      }
      case "assistant": {
        const text = textOf(m.content)
        const toolUses = (m.tool_calls ?? []).map<KiroToolUse>((tc) => ({
          toolUseId: tc.id,
          name: tc.function.name,
          input: parseArgs(tc.function.arguments),
        }))
        if (!text && toolUses.length === 0) break // empty turns confuse Kiro; drop them
        const prev = last()
        if (prev?.role === "assistant") {
          prev.text = [prev.text, text].filter(Boolean).join("\n\n")
          prev.toolUses.push(...toolUses)
        } else turns.push({ role: "assistant", text, toolUses })
        break
      }
    }
  }

  /* Thinking mode is requested through the system prompt, as the Kiro IDE does. */
  const effort = req.reasoning_effort?.toLowerCase()
  const budget = effort ? THINKING_BUDGETS[effort] : undefined
  if (budget) {
    systemParts.unshift(`<thinking_mode>enabled</thinking_mode><max_thinking_length>${budget}</max_thinking_length>`)
  }
  const system = systemParts.join("\n\n")

  /* Kiro needs: starts with a user turn, strictly alternating, tool results paired with tool uses. */
  if (turns.length === 0 || turns[0]!.role === "assistant") {
    turns.unshift({ role: "user", text: "", images: [], toolResults: [] })
  }
  if (turns[turns.length - 1]!.role === "assistant") {
    turns.push({ role: "user", text: "Continue", images: [], toolResults: [] })
  }
  reconcileToolPairs(turns)

  // The system prompt has no home in Kiro's schema; it leads the first user message.
  const first = turns[0] as UserTurn
  if (system) first.text = first.text ? `${system}\n\n${first.text}` : system

  /* Tool catalogue */
  const toolNames = new Map<string, string>()
  const tools = (req.tools ?? []).map((t) => {
    const name = safeToolName(t.function.name, toolNames)
    return {
      toolSpecification: {
        name,
        description: truncate(t.function.description?.trim() || name, MAX_TOOL_DESCRIPTION),
        inputSchema: { json: cleanSchema(t.function.parameters) },
      },
    }
  })
  const kiroName = (original: string) =>
    [...toolNames.entries()].find(([, o]) => o === original)?.[0] ?? safeToolName(original, toolNames)

  // Tools referenced by history but absent from this request (e.g. compaction) need a stub declaration.
  const declared = new Set(tools.map((t) => t.toolSpecification.name))
  for (const t of turns) {
    if (t.role !== "assistant") continue
    for (const u of t.toolUses) {
      u.name = kiroName(u.name)
      if (!declared.has(u.name)) {
        declared.add(u.name)
        tools.push({
          toolSpecification: {
            name: u.name,
            description: u.name,
            inputSchema: { json: { type: "object", properties: {} } },
          },
        })
      }
    }
  }

  const toUserInput = (t: UserTurn, withTools: boolean): KiroUserInput => {
    const ctx: NonNullable<KiroUserInput["userInputMessageContext"]> = {}
    if (t.toolResults.length) ctx.toolResults = t.toolResults
    if (withTools && tools.length) ctx.tools = tools
    return {
      content: t.text || (t.toolResults.length ? "Tool results provided." : "Continue"),
      modelId,
      origin: "AI_EDITOR",
      ...(t.images.length && { images: t.images }),
      ...(Object.keys(ctx).length && { userInputMessageContext: ctx }),
    }
  }

  const current = turns[turns.length - 1] as UserTurn
  const history: KiroHistoryItem[] = turns.slice(0, -1).map((t) =>
    t.role === "user"
      ? { userInputMessage: toUserInput(t, false) }
      : {
          assistantResponseMessage: {
            content: t.text || "Using tools.",
            ...(t.toolUses.length && { toolUses: t.toolUses }),
          },
        },
  )

  const body: KiroRequestBody = {
    conversationState: {
      chatTriggerType: "MANUAL",
      conversationId,
      currentMessage: { userInputMessage: toUserInput(current, true) },
      ...(history.length && { history }),
    },
    ...(opts.profileArn && { profileArn: opts.profileArn }),
  }

  return {
    body,
    toolNames,
    conversationId,
    modelId,
    stream: req.stream !== false,
    thinking: Boolean(budget),
    inputChars: JSON.stringify(body).length,
  }
}

/**
 * Every tool_use must be answered by a tool_result in the very next user turn, and a
 * tool_result must answer a tool_use in the turn right before it. Anything else makes
 * Kiro reject the whole request, so repair mismatches (interrupted tool calls, compaction).
 */
function reconcileToolPairs(turns: Turn[]): void {
  for (let i = 0; i < turns.length; i++) {
    const t = turns[i]!
    if (t.role !== "user") continue
    const prev = turns[i - 1]
    const offered = new Set(prev?.role === "assistant" ? prev.toolUses.map((u) => u.toolUseId) : [])

    const kept: KiroToolResult[] = []
    for (const r of t.toolResults) {
      if (offered.has(r.toolUseId) && !kept.some((k) => k.toolUseId === r.toolUseId)) kept.push(r)
      else t.text = [t.text, `[Result of tool call ${r.toolUseId}]\n${r.content[0]?.text ?? ""}`].filter(Boolean).join("\n\n")
    }
    if (prev?.role === "assistant") {
      for (const u of prev.toolUses) {
        if (!kept.some((k) => k.toolUseId === u.toolUseId)) {
          kept.push({
            toolUseId: u.toolUseId,
            status: "error",
            content: [{ text: "Tool call was interrupted before producing a result." }],
          })
        }
      }
    }
    t.toolResults = kept
  }
}
