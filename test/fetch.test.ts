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
    const { error } = await res.json()
    expect(error.code).toBe("context_length_exceeded")
    // opencode detects overflow by message; these are two of its patterns
    expect(error.message).toMatch(/prompt is too long/i)
    expect(error.message).toMatch(/exceeds the context window/i)
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

  it("rejects malformed request bodies", async () => {
    const { kiro, transport } = harness([])
    expect((await kiro(URL_, { method: "POST", body: "not json" })).status).toBe(400)
    expect(transport).not.toHaveBeenCalled()
  })

  it("turns a dead refresh token into a 401 with a login hint", async () => {
    const tokens = new TokenManager({
      load: async () => ({ type: "oauth", ...toStored({ ...creds, expiresAt: 0 }) }),
      save: async () => {},
      fetch: (async () => new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 })) as any,
    })
    const res = await createKiroFetch({ tokens, fetch: vi.fn() as any })(URL_, chat())
    expect(res.status).toBe(401)
    expect((await res.json()).error.message).toMatch(/opencode auth login/)
  })

  it("reports a network failure during refresh as retryable, not as a logout", async () => {
    const tokens = new TokenManager({
      load: async () => ({ type: "oauth", ...toStored({ ...creds, expiresAt: 0 }) }),
      save: async () => {},
      fetch: (async () => {
        throw new TypeError("fetch failed")
      }) as any,
    })
    const res = await createKiroFetch({ tokens, fetch: vi.fn() as any })(URL_, chat())
    expect(res.status).toBe(503)
    expect((await res.json()).error.message).not.toMatch(/auth login/)
  })

  it("closes the upstream connection when the consumer cancels the stream", async () => {
    let cancelled = false
    const upstream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(concat(text("first")))
      },
      cancel() {
        cancelled = true
      },
    })
    const { kiro } = harness([() => new Response(upstream, { status: 200 })])
    const res = await kiro(URL_, chat())
    const reader = res.body!.getReader()
    await reader.read() // role chunk
    await reader.read() // "first"
    await reader.cancel()
    expect(cancelled).toBe(true)
  })

  it("stops backing off as soon as the request is aborted", async () => {
    const ac = new AbortController()
    let stored = { type: "oauth", ...toStored(creds) }
    const tokens = new TokenManager({ load: async () => stored, save: async (s) => void (stored = { type: "oauth", ...s }) })
    const transport = vi.fn(async () => fail(429, {}, { "retry-after": "30" }))
    const kiro = createKiroFetch({ tokens, fetch: transport as any, sleep: () => new Promise(() => {}) }) // never wakes
    const pending = kiro(URL_, chat({}, ac.signal))
    await vi.waitFor(() => expect(transport).toHaveBeenCalledTimes(1))
    ac.abort(new DOMException("user cancelled", "AbortError"))
    await expect(pending).rejects.toThrow(/cancelled/)
  })

  it("adds a profile ARN learned during a refresh to the retried request", async () => {
    let stored = { type: "oauth", ...toStored(creds) }
    const tokens = new TokenManager({
      load: async () => stored,
      save: async (s) => void (stored = { type: "oauth", ...s }),
      fetch: (async () =>
        new Response(JSON.stringify({ accessToken: "tok2", expiresIn: 3600, profileArn: "arn:aws:codewhisperer:eu-central-1:1:profile/NEW" }))) as any,
    })
    const responses = [() => fail(403), ok]
    const transport = vi.fn(async () => responses.shift()!())
    await createKiroFetch({ tokens, fetch: transport as any })(URL_, chat())
    const [url, init] = transport.mock.calls[1] as any
    expect(JSON.parse(init.body).profileArn).toBe("arn:aws:codewhisperer:eu-central-1:1:profile/NEW")
    expect(url).toBe("https://q.eu-central-1.amazonaws.com/generateAssistantResponse") // region follows the ARN
  })

  it("tags usage with the session from the plugin's header", async () => {
    const onUsage = vi.fn()
    let stored = { type: "oauth", ...toStored(creds) }
    const tokens = new TokenManager({ load: async () => stored, save: async (s) => void (stored = { type: "oauth", ...s }) })
    const transport = vi.fn(async () => ok())
    const kiro = createKiroFetch({ tokens, fetch: transport as any, onUsage })
    const res = await kiro(URL_, { ...chat(), headers: { "x-opencode-kiro-session": "ses_abc" } })
    await res.text()
    expect(onUsage).toHaveBeenCalledWith(expect.objectContaining({ sessionID: "ses_abc", modelId: "claude-sonnet-4.5" }))
    // the tag is ours: it must not leak to Kiro
    expect(JSON.stringify((transport.mock.calls[0] as any)[1].headers)).not.toContain("ses_abc")
  })
})
