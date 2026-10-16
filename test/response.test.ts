import { describe, expect, it } from "vitest"
import { chunksToCompletion, chunksToSSE, ThinkingSplitter, toChatChunks } from "../src/transform/response.js"
import { concat, contextUsage, exception, event, text, toolUse, streamOf } from "./helpers/eventstream.js"

const req = (over: object = {}) => ({
  toolNames: new Map<string, string>(),
  thinking: false,
  inputChars: 400,
  modelId: "claude-sonnet-4.5",
  ...over,
})
const collect = async (bytes: Uint8Array, ctx: object = {}, piece = bytes.length) => {
  const out = []
  for await (const c of toChatChunks(streamOf(bytes, piece), { request: req(ctx) })) out.push(c)
  return out
}
const deltas = (chunks: Awaited<ReturnType<typeof collect>>) => chunks.flatMap((c) => c.choices.map((x) => x.delta))

describe("toChatChunks", () => {
  it("streams text then a stop chunk and a usage chunk", async () => {
    const chunks = await collect(concat(text("Hel"), text("lo"), contextUsage(5)), {}, 11)
    expect(deltas(chunks).filter((d) => d.content).map((d) => d.content)).toEqual(["", "Hel", "lo"].filter(Boolean))
    expect(deltas(chunks)[0]).toEqual({ role: "assistant", content: "" })
    const finish = chunks.find((c) => c.choices[0]?.finish_reason)!
    expect(finish.choices[0]!.finish_reason).toBe("stop")
    const usage = chunks.at(-1)!
    expect(usage.choices).toEqual([])
    expect(usage.usage!.completion_tokens).toBe(2) // ceil(5 chars / 4)
    expect(usage.usage!.total_tokens).toBe(usage.usage!.prompt_tokens + usage.usage!.completion_tokens)
  })

  it("derives prompt tokens from context usage when the window is known", async () => {
    const out = []
    for await (const c of toChatChunks(streamOf(concat(text("x".repeat(40)), contextUsage(10))), {
      request: req(),
      contextWindow: 200_000,
    }))
      out.push(c)
    expect(out.at(-1)!.usage!.prompt_tokens).toBe(20_000 - 10)
  })

  it("streams tool calls with incremental arguments and maps sanitized names back", async () => {
    const names = new Map([["mcp_do", "mcp.do"]])
    const chunks = await collect(
      concat(toolUse("t1", "mcp_do", ""), toolUse("t1", "mcp_do", '{"a":'), toolUse("t1", "mcp_do", "1}"), toolUse("t1", "mcp_do", "", true), toolUse("t2", "bash", "", true)),
      { toolNames: names },
    )
    const calls = deltas(chunks).flatMap((d: any) => d.tool_calls ?? [])
    expect(calls[0]).toEqual({ index: 0, id: "t1", type: "function", function: { name: "mcp.do", arguments: "" } })
    expect(calls.filter((c: any) => c.index === 0).map((c: any) => c.function.arguments).join("")).toBe('{"a":1}')
    // a call that never streamed arguments is closed with an empty object
    expect(calls.at(-1)).toEqual({ index: 1, function: { arguments: "{}" } })
    expect(chunks.find((c) => c.choices[0]?.finish_reason)!.choices[0]!.finish_reason).toBe("tool_calls")
  })

  it("separates <thinking> blocks into reasoning_content, even when tags are split", async () => {
    const chunks = await collect(concat(text("<thin"), text("king>plan "), text("it</think"), text("ing>\n\nAnswer")), { thinking: true })
    const d = deltas(chunks) as any[]
    expect(d.filter((x) => x.reasoning_content).map((x) => x.reasoning_content).join("")).toBe("plan it")
    expect(d.filter((x) => x.content).map((x) => x.content).join("")).toBe("Answer")
  })

  it("reports a stream cut off mid-frame as an error", async () => {
    const whole = concat(text("ok"), text("never finished"))
    const chunks = await collect(whole.slice(0, whole.length - 5))
    expect(chunks.at(-1)!.error).toMatchObject({ code: "stream_error" })
    expect(chunks.at(-1)!.error!.message).toMatch(/mid-message/)
  })

  it("reports exceptions as an error chunk", async () => {
    const chunks = await collect(concat(text("partial"), exception("ThrottlingException", "too fast")))
    const err = chunks.find((c) => c.error)!
    expect(err.error).toMatchObject({ message: "too fast", code: "ThrottlingException" })
    expect(chunks.at(-1)).toBe(err)
  })
})

describe("ThinkingSplitter", () => {
  it("passes text through when disabled", () => {
    expect(new ThinkingSplitter(false).push("<thinking>x</thinking>")).toEqual([{ text: "<thinking>x</thinking>" }])
  })
  it("flushes a dangling partial tag as text", () => {
    const s = new ThinkingSplitter(true)
    expect([...s.push("a <thi"), ...s.flush()].map((x) => x.text).join("")).toBe("a <thi")
  })
})

describe("serialisation", () => {
  it("encodes SSE with a [DONE] terminator and turns errors into bare error payloads", async () => {
    const body = chunksToSSE(toChatChunks(streamOf(concat(text("hi"))), { request: req() }))
    const raw = await new Response(body).text()
    expect(raw.trimEnd().endsWith("data: [DONE]")).toBe(true)
    expect(raw).toContain('"content":"hi"')

    const err = await new Response(chunksToSSE(toChatChunks(streamOf(exception("X", "boom")), { request: req() }))).text()
    expect(err).toContain('data: {"error":{"message":"boom"')
  })

  it("aggregates a non-streaming completion", async () => {
    const res = await chunksToCompletion(toChatChunks(streamOf(concat(text("ok "), text("done"), toolUse("t1", "bash", '{"c":1}', true))), { request: req() }))
    expect(res.choices[0]!.message).toMatchObject({ role: "assistant", content: "ok done", tool_calls: [{ id: "t1", function: { name: "bash", arguments: '{"c":1}' } }] })
    expect(res.choices[0]!.finish_reason).toBe("tool_calls")
  })
})
