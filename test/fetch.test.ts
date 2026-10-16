import { describe, expect, it, vi } from "vitest"
import { toStored, type KiroCredentials } from "../src/auth/credentials.js"
import { TokenManager } from "../src/auth/manager.js"
import { createKiroFetch } from "../src/fetch.js"
import { concat, text } from "./helpers/eventstream.js"

const creds: KiroCredentials = { accessToken: "tok1", refreshToken: "r", expiresAt: 9e12, authMethod: "social", region: "us-east-1" }
const chat = (over: object = {}, signal?: AbortSignal) => ({
  method: "POST",
  signal,
  body: JSON.stringify({ model: "claude-sonnet-4.5", stream: true, messages: [{ role: "user", content: "hi" }], ...over }),
})
const URL_ = "https://kiro.invalid/v1/chat/completions"
const ok = () => new Response(concat(text("hello")) as BodyInit, { status: 200 })
const fail = (status: number, body: unknown = { message: "nope" }, headers: HeadersInit = {}) =>
  new Response(JSON.stringify(body), { status, headers })

function harness(responses: (() => Response)[]) {
  let stored = { type: "oauth", ...toStored(creds) }
  const refresh = vi.fn(async () => new Response(JSON.stringify({ accessToken: "tok2", expiresIn: 3600 })))
  const tokens = new TokenManager({
    load: async () => stored,
    save: async (s) => void (stored = { type: "oauth", ...s }),
    fetch: refresh as any,
  })
  const transport = vi.fn(async () => (responses.shift() ?? ok)())
  const sleep = vi.fn(async () => {})
  const kiro = createKiroFetch({ tokens, fetch: transport as any, sleep, maxRetries: 2 })
  return { kiro, transport, refresh, sleep }
}

describe("createKiroFetch", () => {
  it("posts to generateAssistantResponse with the bearer token and returns an SSE stream", async () => {
    const { kiro, transport } = harness([ok])
    const res = await kiro(URL_, chat())
    expect(res.headers.get("content-type")).toBe("text/event-stream")
    expect(await res.text()).toContain('"content":"hello"')
    const [url, init] = transport.mock.calls[0] as any
    expect(url).toBe("https://q.us-east-1.amazonaws.com/generateAssistantResponse")
    expect(init.headers.authorization).toBe("Bearer tok1")
    expect(JSON.parse(init.body).conversationState.currentMessage.userInputMessage.content).toBe("hi")
  })

  it("returns a plain chat.completion for non-streaming requests", async () => {
    const { kiro } = harness([ok])
    const body = await (await kiro(URL_, chat({ stream: false }))).json()
    expect(body.object).toBe("chat.completion")
    expect(body.choices[0].message.content).toBe("hello")
  })

  it("refreshes the token once on 401 and retries", async () => {
    const { kiro, transport, refresh } = harness([() => fail(401), ok])
    expect((await kiro(URL_, chat())).status).toBe(200)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect((transport.mock.calls[1] as any)[1].headers.authorization).toBe("Bearer tok2")
  })

  it("surfaces persistent auth failures with a login hint", async () => {
    const { kiro } = harness([() => fail(403), () => fail(403)])
    const res = await kiro(URL_, chat())
    expect(res.status).toBe(403)
    expect((await res.json()).error.message).toMatch(/opencode auth login/)
  })

  it("backs off and retries 429/5xx, honouring Retry-After", async () => {
    const { kiro, transport, sleep } = harness([() => fail(429, {}, { "retry-after": "2" }), () => fail(503), ok])
    expect((await kiro(URL_, chat())).status).toBe(200)
    expect(transport).toHaveBeenCalledTimes(3)
    expect((sleep.mock.calls[0] as any)[0]).toBe(2000)
  })

  it("gives up after maxRetries and maps the final error", async () => {
    const { kiro, transport } = harness([() => fail(500), () => fail(500), () => fail(500, { message: "boom" })])
    const res = await kiro(URL_, chat())
    expect(transport).toHaveBeenCalledTimes(3)
    expect(res.status).toBe(500)
    expect((await res.json()).error.message).toContain("boom")
  })

  it("maps oversized prompts to a context-overflow error", async () => {
    const { kiro } = harness([() => fail(400, { message: "Input is too long.", reason: "CONTENT_LENGTH_EXCEEDS_THRESHOLD" })])
    const res = await kiro(URL_, chat())
    expect(res.status).toBe(400)
    expect((await res.json()).error.code).toBe("context_length_exceeded")
  })

  it("does not retry client errors, and rejects other endpoints", async () => {
    const { kiro, transport } = harness([() => fail(400, { message: "Improperly formed request." })])
    expect((await kiro(URL_, chat())).status).toBe(400)
    expect(transport).toHaveBeenCalledTimes(1)
    expect((await kiro("https://kiro.invalid/v1/embeddings", chat())).status).toBe(404)
  })

  it("propagates aborts instead of retrying", async () => {
    const ac = new AbortController()
    const { kiro, transport } = harness([])
    transport.mockImplementation(async () => {
      ac.abort()
      throw new DOMException("aborted", "AbortError")
    })
    await expect(kiro(URL_, chat({}, ac.signal))).rejects.toThrow(/abort/i)
    expect(transport).toHaveBeenCalledTimes(1)
  })
})
