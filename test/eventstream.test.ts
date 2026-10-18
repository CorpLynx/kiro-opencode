import { describe, expect, it } from "vitest"
import { EventStreamDecoder, toKiroEvent } from "../src/transform/eventstream.js"
import { concat, contextUsage, event, exception, text, toolUse } from "./helpers/eventstream.js"

describe("EventStreamDecoder", () => {
  it("decodes frames delivered in one piece", () => {
    const d = new EventStreamDecoder()
    const msgs = d.push(concat(text("a"), text("b")))
    expect(msgs.map((m) => toKiroEvent(m))).toEqual([
      { type: "text", content: "a" },
      { type: "text", content: "b" },
    ])
    expect(d.pending).toBe(0)
  })

  it("reassembles frames split at arbitrary byte boundaries", () => {
    const bytes = concat(text("héllo wörld"), toolUse("t1", "bash", '{"a":1}', true), contextUsage(2.5))
    for (const size of [1, 3, 7, 50]) {
      const d = new EventStreamDecoder()
      const out = []
      for (let i = 0; i < bytes.length; i += size) out.push(...d.push(bytes.slice(i, i + size)))
      expect(out.map((m) => toKiroEvent(m)?.type)).toEqual(["text", "toolUse", "contextUsage"])
      expect((toKiroEvent(out[0]!) as any).content).toBe("héllo wörld")
    }
  })

  it("maps tool use, usage and exceptions", () => {
    const d = new EventStreamDecoder()
    const [tu, ex, other] = d.push(concat(toolUse("t1", "bash", "{}", true), exception("ThrottlingException", "slow down"), event("followupPromptEvent", {})))
    expect(toKiroEvent(tu!)).toEqual({ type: "toolUse", toolUseId: "t1", name: "bash", input: "{}", stop: true })
    expect(toKiroEvent(ex!)).toEqual({ type: "error", code: "ThrottlingException", message: "slow down" })
    expect(toKiroEvent(other!)).toBeUndefined()
  })

  it("rejects corrupt frame lengths", () => {
    expect(() => new EventStreamDecoder().push(new Uint8Array(16).fill(0xff))).toThrow(/Corrupt/)
  })
})
