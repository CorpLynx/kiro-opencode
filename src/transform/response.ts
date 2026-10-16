import { EventStreamDecoder, toKiroEvent, type KiroEvent } from "./eventstream.js"
import type { TransformedRequest } from "./request.js"

export interface ChunkContext {
  request: Pick<TransformedRequest, "toolNames" | "thinking" | "inputChars" | "modelId">
  /** Used to turn Kiro's context-usage percentage into a token count. */
  contextWindow?: number
  signal?: AbortSignal
}

interface Usage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface ChatChunk {
  id: string
  object: "chat.completion.chunk"
  created: number
  model: string
  choices: { index: 0; delta: Record<string, unknown>; finish_reason: string | null }[]
  usage?: Usage
  error?: { message: string; type: string; code: string }
}

/** Splits `<thinking>…</thinking>` out of the text stream, tolerating tags cut across chunks. */
export class ThinkingSplitter {
  private inside = false
  private buf = ""
  private trimNext = false

  constructor(private enabled: boolean) {}

  push(text: string): { text?: string; reasoning?: string }[] {
    if (!this.enabled) return [{ text }]
    this.buf += text
    return this.drain(false)
  }

  flush(): { text?: string; reasoning?: string }[] {
    return this.enabled ? this.drain(true) : []
  }

  private emit(out: { text?: string; reasoning?: string }[], s: string) {
    if (!s) return
    if (this.inside) return void out.push({ reasoning: s })
    if (this.trimNext) {
      s = s.replace(/^\s+/, "")
      if (!s) return
      this.trimNext = false
    }
    out.push({ text: s })
  }

  private drain(final: boolean) {
    const out: { text?: string; reasoning?: string }[] = []
    for (;;) {
      const tag = this.inside ? "</thinking>" : "<thinking>"
      const i = this.buf.indexOf(tag)
      if (i >= 0) {
        this.emit(out, this.buf.slice(0, i))
        this.buf = this.buf.slice(i + tag.length)
        if (this.inside) this.trimNext = true
        this.inside = !this.inside
        continue
      }
      // Keep back any suffix that might be the start of a tag.
      let keep = 0
      if (!final) {
        for (let n = Math.min(tag.length - 1, this.buf.length); n > 0; n--) {
          if (tag.startsWith(this.buf.slice(-n))) {
            keep = n
            break
          }
        }
      }
      this.emit(out, this.buf.slice(0, this.buf.length - keep))
      this.buf = this.buf.slice(this.buf.length - keep)
      return out
    }
  }
}

async function* kiroEvents(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<KiroEvent> {
  const decoder = new EventStreamDecoder()
  const reader = body.getReader()
  let finished = false
  try {
    for (;;) {
      if (signal?.aborted) return
      const { done, value } = await reader.read()
      if (done) {
        finished = true
        break
      }
      for (const msg of decoder.push(value)) {
        const ev = toKiroEvent(msg)
        if (ev) yield ev
      }
    }
    if (decoder.pending > 0) throw new Error("connection closed mid-message")
  } finally {
    // Stopping early (abort, consumer gone, error event) must close the upstream connection.
    if (!finished) await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

const estimateTokens = (chars: number) => Math.ceil(chars / 4)

/** Convert Kiro's event stream into OpenAI `chat.completion.chunk` objects. */
export async function* toChatChunks(
  body: ReadableStream<Uint8Array>,
  ctx: ChunkContext,
): AsyncGenerator<ChatChunk> {
  const id = `chatcmpl-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`
  const created = Math.floor(Date.now() / 1000)
  const model = ctx.request.modelId
  const chunk = (delta: Record<string, unknown>, finish: string | null = null): ChatChunk => ({
    id,
    object: "chat.completion.chunk",
    created,
    model,
    choices: [{ index: 0, delta, finish_reason: finish }],
  })

  const splitter = new ThinkingSplitter(ctx.request.thinking)
  const toolIndex = new Map<string, { index: number; hasArgs: boolean }>()
  let outputChars = 0
  let contextPct: number | undefined
  let started = false

  const start = function* () {
    if (started) return
    started = true
    yield chunk({ role: "assistant", content: "" })
  }

  const parts = function* (items: { text?: string; reasoning?: string }[]) {
    for (const p of items) {
      if (p.text) {
        outputChars += p.text.length
        yield chunk({ content: p.text })
      } else if (p.reasoning) {
        outputChars += p.reasoning.length
        yield chunk({ reasoning_content: p.reasoning })
      }
    }
  }

  try {
    for await (const ev of kiroEvents(body, ctx.signal)) {
      switch (ev.type) {
        case "text":
          yield* start()
          yield* parts(splitter.push(ev.content))
          break
        case "reasoning":
          yield* start()
          yield* parts([{ reasoning: ev.text }])
          break
        case "toolUse": {
          yield* start()
          yield* parts(splitter.flush())
          let call = toolIndex.get(ev.toolUseId)
          if (!call) {
            call = { index: toolIndex.size, hasArgs: false }
            toolIndex.set(ev.toolUseId, call)
            const name = ctx.request.toolNames.get(ev.name ?? "") ?? ev.name ?? ""
            yield chunk({
              tool_calls: [
                { index: call.index, id: ev.toolUseId, type: "function", function: { name, arguments: "" } },
              ],
            })
          }
          if (ev.input) {
            call.hasArgs = true
            outputChars += ev.input.length
            yield chunk({ tool_calls: [{ index: call.index, function: { arguments: ev.input } }] })
          }
          if (ev.stop && !call.hasArgs) {
            call.hasArgs = true
            yield chunk({ tool_calls: [{ index: call.index, function: { arguments: "{}" } }] })
          }
          break
        }
        case "contextUsage":
          contextPct = ev.percentage
          break
        case "error":
          yield* start()
          yield {
            ...chunk({}, "error"),
            error: { message: ev.message, type: ev.code, code: ev.code },
          }
          return
        case "metering":
          break
      }
    }
  } catch (err) {
    if (ctx.signal?.aborted) return
    yield* start()
    yield {
      ...chunk({}, "error"),
      error: {
        message: `Kiro stream interrupted: ${err instanceof Error ? err.message : String(err)}`,
        type: "stream_error",
        code: "stream_error",
      },
    }
    return
  }

  yield* start()
  yield* parts(splitter.flush())

  const completion = estimateTokens(outputChars)
  const prompt =
    contextPct != null && ctx.contextWindow
      ? Math.max(0, Math.round((contextPct / 100) * ctx.contextWindow) - completion)
      : estimateTokens(ctx.request.inputChars)

  yield chunk({}, toolIndex.size > 0 ? "tool_calls" : "stop")
  yield {
    ...chunk({}),
    choices: [],
    usage: { prompt_tokens: prompt, completion_tokens: completion, total_tokens: prompt + completion },
  }
}

const sse = (o: unknown) => `data: ${JSON.stringify(o)}\n\n`

/** `text/event-stream` body in the OpenAI wire format. */
export function chunksToSSE(chunks: AsyncGenerator<ChatChunk>, signal?: AbortSignal): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await chunks.next()
        if (done) {
          controller.enqueue(enc.encode("data: [DONE]\n\n"))
          controller.close()
          return
        }
        // An error chunk is surfaced as a bare `{error}` payload, which the AI SDK turns into a thrown error.
        controller.enqueue(enc.encode(sse(value.error ? { error: value.error } : value)))
      } catch (err) {
        controller.error(err)
      }
    },
    async cancel() {
      await chunks.return(undefined)
    },
  })
}

/** Collapse the chunk stream into a single non-streaming `chat.completion` response. */
export async function chunksToCompletion(chunks: AsyncGenerator<ChatChunk>) {
  let content = ""
  let reasoning = ""
  let finish = "stop"
  let usage: Usage | undefined
  let id = ""
  let created = 0
  let model = ""
  const calls: { id: string; type: "function"; function: { name: string; arguments: string } }[] = []

  for await (const c of chunks) {
    id ||= c.id
    created ||= c.created
    model ||= c.model
    if (c.error) throw Object.assign(new Error(c.error.message), { code: c.error.code })
    if (c.usage) usage = c.usage
    const choice = c.choices[0]
    if (!choice) continue
    if (choice.finish_reason) finish = choice.finish_reason
    const d = choice.delta as any
    if (d.content) content += d.content
    if (d.reasoning_content) reasoning += d.reasoning_content
    for (const tc of d.tool_calls ?? []) {
      const slot = (calls[tc.index] ??= { id: "", type: "function", function: { name: "", arguments: "" } })
      if (tc.id) slot.id = tc.id
      if (tc.function?.name) slot.function.name = tc.function.name
      if (tc.function?.arguments) slot.function.arguments += tc.function.arguments
    }
  }

  return {
    id,
    object: "chat.completion",
    created,
    model,
    choices: [
      {
        index: 0,
        finish_reason: finish,
        message: {
          role: "assistant",
          content,
          ...(reasoning && { reasoning_content: reasoning }),
          ...(calls.length && { tool_calls: calls }),
        },
      },
    ],
    usage,
  }
}
