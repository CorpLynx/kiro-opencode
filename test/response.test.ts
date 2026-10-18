import { describe, expect, it, vi } from "vitest"
import { chunksToCompletion, chunksToSSE, describeStreamError, ThinkingSplitter, toChatChunks } from "../src/transform/response.js"
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

  it("reports Kiro's own token counts when it sends them", async () => {
    const tokens = event("metadataEvent", {
      tokenUsage: { uncachedInputTokens: 900, cacheReadInputTokens: 4000, cacheWriteInputTokens: 100, outputTokens: 42, totalTokens: 5042 },
    })
    const usage = (await collect(concat(text("hi"), contextUsage(50), tokens), {}, 7)).at(-1)!.usage
    expect(usage).toEqual({ prompt_tokens: 5000, completion_tokens: 42, total_tokens: 5042, prompt_tokens_details: { cached_tokens: 4000 } })

    const plain = event("metadataEvent", { tokenUsage: { inputTokens: 1234, outputTokens: 5 } })
    expect((await collect(concat(text("hi"), plain))).at(-1)!.usage).toEqual({ prompt_tokens: 1234, completion_tokens: 5, total_tokens: 1239 })
  })

  it("reports exceptions as an error chunk", async () => {
    const chunks = await collect(concat(text("partial"), exception("ThrottlingException", "too fast")))
    const err = chunks.find((c) => c.error)!
    expect(err.error).toMatchObject({ code: "ThrottlingException" })
    expect(err.error!.message).toBe(
      "Kiro stopped mid-response. too fast. Kiro is rate-limiting requests; wait a moment and try again. (ThrottlingException)",
    )
    expect(chunks.at(-1)).toBe(err)
  })
})

describe("usage reporting", () => {
  const meter = (usage: number) => event("meteringEvent", { unit: "credit", usage })

  it("tallies Kiro's credit charges and context figure once per response", async () => {
    const onUsage = vi.fn()
    for await (const _ of toChatChunks(streamOf(concat(text("a"), meter(0.25), meter(0.5), contextUsage(42))), { request: req(), onUsage })) {
    }
    expect(onUsage).toHaveBeenCalledTimes(1)
    expect(onUsage).toHaveBeenCalledWith({ credits: 0.75, contextPct: 42 })
  })

  it("still reports when the response ends in an error or is abandoned", async () => {
    const onUsage = vi.fn()
    for await (const _ of toChatChunks(streamOf(concat(meter(0.1), exception("X", "boom"))), { request: req(), onUsage })) {
    }
    expect(onUsage).toHaveBeenLastCalledWith({ credits: 0.1, contextPct: undefined })

    const gen = toChatChunks(streamOf(concat(text("a"), text("b"), meter(0.2))), { request: req(), onUsage })
    await gen.next()
    await gen.next()
    await gen.return(undefined)
    expect(onUsage).toHaveBeenCalledTimes(2)
  })

  it("never lets a failing tracker break the response", async () => {
    const chunks = []
    for await (const c of toChatChunks(streamOf(concat(text("ok"))), {
      request: req(),
      onUsage: () => {
        throw new Error("disk full")
      },
    }))
      chunks.push(c)
    expect(chunks.at(-1)!.usage).toBeDefined()
  })
})

describe("describeStreamError", () => {
  it("explains known failures and still names the exception", () => {
    expect(describeStreamError("InternalServerException", "Encountered an unexpected error")).toBe(
      "Kiro stopped mid-response. Encountered an unexpected error. A temporary Kiro service error; try again. (InternalServerException)",
    )
    expect(describeStreamError("WeirdException", "WeirdException")).toBe("Kiro stopped mid-response. (WeirdException)")
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
    expect(err).toContain('data: {"error":{"message":"Kiro stopped mid-response. boom. (X)"}}')
  })

  it("aggregates a non-streaming completion", async () => {
    const res = await chunksToCompletion(toChatChunks(streamOf(concat(text("ok "), text("done"), toolUse("t1", "bash", '{"c":1}', true))), { request: req() }))
    expect(res.choices[0]!.message).toMatchObject({ role: "assistant", content: "ok done", tool_calls: [{ id: "t1", function: { name: "bash", arguments: '{"c":1}' } }] })
    expect(res.choices[0]!.finish_reason).toBe("tool_calls")
  })
})
