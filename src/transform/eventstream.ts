/**
 * Decoder for the `application/vnd.amazon.eventstream` binary framing that Kiro's
 * `generateAssistantResponse` endpoint streams its answer in.
 *
 * Frame layout (all integers big-endian):
 *   total length (4) | headers length (4) | prelude CRC (4) | headers | payload | message CRC (4)
 *
 * CRCs are not verified: the transport is TLS and a corrupt frame fails JSON parsing anyway.
 */

export interface EventStreamMessage {
  headers: Record<string, string | number | boolean>
  payload: Uint8Array
}

const MAX_MESSAGE_BYTES = 16 * 1024 * 1024
const decoder = new TextDecoder()

export class EventStreamDecoder {
  private buf: Uint8Array = new Uint8Array(0)

  push(chunk: Uint8Array): EventStreamMessage[] {
    if (this.buf.length === 0) this.buf = chunk
    else {
      const next = new Uint8Array(this.buf.length + chunk.length)
      next.set(this.buf)
      next.set(chunk, this.buf.length)
      this.buf = next
    }

    const out: EventStreamMessage[] = []
    while (this.buf.length >= 12) {
      const view = new DataView(this.buf.buffer, this.buf.byteOffset, this.buf.byteLength)
      const total = view.getUint32(0)
      const headersLen = view.getUint32(4)
      if (total < 16 || total > MAX_MESSAGE_BYTES || headersLen > total - 16) {
        throw new Error(`Corrupt event stream frame (length ${total})`)
      }
      if (this.buf.length < total) break

      const headers = parseHeaders(this.buf.subarray(12, 12 + headersLen))
      const payload = this.buf.slice(12 + headersLen, total - 4)
      out.push({ headers, payload })
      this.buf = this.buf.subarray(total)
    }
    return out
  }

  /** Bytes left over that did not form a complete frame (non-zero at EOF means truncation). */
  get pending(): number {
    return this.buf.length
  }
}

function parseHeaders(b: Uint8Array): EventStreamMessage["headers"] {
  const view = new DataView(b.buffer, b.byteOffset, b.byteLength)
  const headers: EventStreamMessage["headers"] = {}
  let o = 0
  while (o < b.length) {
    const nameLen = b[o++]!
    const name = decoder.decode(b.subarray(o, o + nameLen))
    o += nameLen
    const type = b[o++]!
    switch (type) {
      case 0:
        headers[name] = true
        break
      case 1:
        headers[name] = false
        break
      case 2:
        headers[name] = view.getInt8(o)
        o += 1
        break
      case 3:
        headers[name] = view.getInt16(o)
        o += 2
        break
      case 4:
        headers[name] = view.getInt32(o)
        o += 4
        break
      case 5:
      case 8:
        headers[name] = Number(view.getBigInt64(o))
        o += 8
        break
      case 6:
      case 7: {
        const len = view.getUint16(o)
        o += 2
        if (type === 7) headers[name] = decoder.decode(b.subarray(o, o + len))
        o += len
        break
      }
      case 9:
        o += 16
        break
      default:
        throw new Error(`Unknown event stream header type ${type}`)
    }
  }
  return headers
}

/* ---------- Kiro-level events ---------- */

export type KiroEvent =
  | { type: "text"; content: string }
  | { type: "reasoning"; text: string }
  | { type: "toolUse"; toolUseId: string; name?: string; input: string; stop: boolean }
  | { type: "contextUsage"; percentage: number }
  | { type: "metering"; usage: number }
  | { type: "tokens"; usage: TokenUsage }
  | { type: "error"; code: string; message: string }

/** Real token counts, from `metadataEvent.tokenUsage` (every field optional). */
export interface TokenUsage {
  inputTokens?: number
  uncachedInputTokens?: number
  cacheReadInputTokens?: number
  cacheWriteInputTokens?: number
  outputTokens?: number
  totalTokens?: number
}

/** Event types we read, or knowingly ignore; anything else is worth a line in the debug log. */
export const KNOWN_EVENT_TYPES = new Set([
  "assistantResponseEvent",
  "reasoningContentEvent",
  "toolUseEvent",
  "contextUsageEvent",
  "meteringEvent",
  "metadataEvent",
  "messageMetadataEvent",
  "followupPromptEvent",
  "supplementaryWebLinksEvent",
  "codeReferenceEvent",
  "citationEvent",
])

/** `:event-type`, or the exception type for error frames. */
export function frameType(msg: EventStreamMessage): string {
  const t = msg.headers[":event-type"] ?? msg.headers[":exception-type"] ?? msg.headers[":message-type"]
  return typeof t === "string" ? t : "unknown"
}

/** Top-level field names of a frame's JSON payload (never the values). */
export function payloadKeys(msg: EventStreamMessage): string[] {
  try {
    const data = JSON.parse(decoder.decode(msg.payload))
    return data && typeof data === "object" ? Object.keys(data).sort() : []
  } catch {
    return []
  }
}

export function toKiroEvent(msg: EventStreamMessage): KiroEvent | undefined {
  const messageType = msg.headers[":message-type"]
  let data: any
  try {
    data = msg.payload.length ? JSON.parse(decoder.decode(msg.payload)) : {}
  } catch {
    data = {}
  }

  if (messageType === "exception" || messageType === "error") {
    const code = String(msg.headers[":exception-type"] ?? msg.headers[":error-code"] ?? "UnknownError")
    return { type: "error", code, message: String(data.message ?? data.Message ?? data.reason ?? code) }
  }

  switch (msg.headers[":event-type"]) {
    case "assistantResponseEvent":
      return typeof data.content === "string" && data.content ? { type: "text", content: data.content } : undefined
    case "reasoningContentEvent":
      return typeof data.text === "string" && data.text ? { type: "reasoning", text: data.text } : undefined
    case "toolUseEvent":
      return {
        type: "toolUse",
        toolUseId: String(data.toolUseId ?? ""),
        name: data.name,
        input: typeof data.input === "string" ? data.input : data.input == null ? "" : JSON.stringify(data.input),
        stop: Boolean(data.stop),
      }
    case "contextUsageEvent":
      return typeof data.contextUsagePercentage === "number"
        ? { type: "contextUsage", percentage: data.contextUsagePercentage }
        : undefined
    case "meteringEvent":
      return typeof data.usage === "number" ? { type: "metering", usage: data.usage } : undefined
    case "metadataEvent": {
      const u = data.tokenUsage
      if (!u || typeof u !== "object") return undefined
      const usage: TokenUsage = {}
      for (const key of [
        "inputTokens",
        "uncachedInputTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "outputTokens",
        "totalTokens",
      ] as const) {
        if (typeof u[key] === "number" && Number.isFinite(u[key])) usage[key] = u[key]
      }
      return Object.keys(usage).length ? { type: "tokens", usage } : undefined
    }
    default:
      return undefined // followups, citations, metadata… not needed for chat completion
  }
}
