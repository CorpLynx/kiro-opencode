/** Test-only encoder for AWS event stream frames, mirroring what Kiro sends. */
const enc = new TextEncoder()

function header(name: string, value: string): Uint8Array {
  const n = enc.encode(name)
  const v = enc.encode(value)
  const out = new Uint8Array(1 + n.length + 1 + 2 + v.length)
  let o = 0
  out[o++] = n.length
  out.set(n, o)
  o += n.length
  out[o++] = 7 // string
  new DataView(out.buffer).setUint16(o, v.length)
  o += 2
  out.set(v, o)
  return out
}

export function frame(headers: Record<string, string>, payload: unknown): Uint8Array {
  const hb = Object.entries(headers).map(([k, v]) => header(k, v))
  const headersLen = hb.reduce((n, h) => n + h.length, 0)
  const body = enc.encode(typeof payload === "string" ? payload : JSON.stringify(payload))
  const total = 12 + headersLen + body.length + 4
  const out = new Uint8Array(total)
  const view = new DataView(out.buffer)
  view.setUint32(0, total)
  view.setUint32(4, headersLen)
  // CRCs are zero: the decoder does not verify them.
  let o = 12
  for (const h of hb) {
    out.set(h, o)
    o += h.length
  }
  out.set(body, o)
  return out
}

export const event = (type: string, payload: unknown) =>
  frame({ ":message-type": "event", ":event-type": type, ":content-type": "application/json" }, payload)

export const exception = (type: string, message: string) =>
  frame({ ":message-type": "exception", ":exception-type": type }, { message })

export const text = (content: string) => event("assistantResponseEvent", { content })
export const toolUse = (toolUseId: string, name: string, input: string, stop = false) =>
  event("toolUseEvent", { toolUseId, name, input, ...(stop && { stop: true }) })
export const contextUsage = (pct: number) => event("contextUsageEvent", { contextUsagePercentage: pct })

export function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let o = 0
  for (const p of parts) {
    out.set(p, o)
    o += p.length
  }
  return out
}

/** Stream the bytes in arbitrary-sized pieces to exercise frame reassembly. */
export function streamOf(bytes: Uint8Array, pieceSize = bytes.length): ReadableStream<Uint8Array> {
  let i = 0
  return new ReadableStream({
    pull(c) {
      if (i >= bytes.length) return c.close()
      c.enqueue(bytes.slice(i, i + pieceSize))
      i += pieceSize
    },
  })
}
