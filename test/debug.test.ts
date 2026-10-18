import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { toStored } from "../src/auth/credentials.js"
import { TokenManager } from "../src/auth/manager.js"
import { createDebug, debugEnabled, redact } from "../src/debug.js"
import { createKiroFetch } from "../src/fetch.js"
import { concat, contextUsage, event, text } from "./helpers/eventstream.js"

const tmp = () => mkdtempSync(join(tmpdir(), "kiro-debug-"))
const lines = (file: string) => readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l))

describe("debug log", () => {
  it("is off unless asked for", () => {
    expect(debugEnabled(undefined, {})).toBe(false)
    expect(debugEnabled(true, {})).toBe(true)
    expect(debugEnabled(undefined, { KIRO_DEBUG: "1" })).toBe(true)
    expect(debugEnabled(undefined, { KIRO_DEBUG: "0" })).toBe(false)
    expect(createDebug({ enabled: false, dir: tmp() }).enabled).toBe(false)
  })

  it("writes JSON lines and rotates past 5 MB", () => {
    const dir = tmp()
    const debug = createDebug({ enabled: true, dir })
    debug("one", { a: 1 })
    expect(lines(debug.file!)).toEqual([{ t: expect.any(String), event: "one", a: 1 }])
    writeFileSync(debug.file!, "x".repeat(5 * 1024 * 1024 + 1))
    debug("two")
    expect(statSync(`${debug.file}.1`).size).toBeGreaterThan(5 * 1024 * 1024)
    expect(lines(debug.file!).map((l) => l.event)).toEqual(["two"])
  })

  it("masks anything token-shaped", () => {
    expect(redact("Authorization: Bearer abc.def-ghi")).toBe("Authorization: Bearer [redacted]")
    expect(redact("token aoaAAAAAGlongrefreshtokenvalue12345 rejected")).toBe("token [redacted] rejected")
  })

  it("records the shape of a request and its stream, never its content", async () => {
    const dir = tmp()
    const debug = createDebug({ enabled: true, dir })
    let stored = { type: "oauth", ...toStored({ accessToken: "secret-token", refreshToken: "r", expiresAt: 9e12, authMethod: "social", region: "us-east-1" }) }
    const tokens = new TokenManager({ load: async () => stored, save: async (s) => void (stored = { type: "oauth", ...s }) })
    const body = concat(
      text("the secret answer"),
      event("meteringEvent", { unit: "credit", usage: 0.4 }),
      event("brandNewEvent", { foo: 1, bar: "x" }),
      contextUsage(12),
    )
    const kiro = createKiroFetch({
      tokens,
      debug,
      fetch: (async () => new Response(body as BodyInit, { headers: { "x-amzn-requestid": "req-123" } })) as any,
    })
    const res = await kiro("https://kiro.invalid/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({ model: "claude-sonnet-4.5", messages: [{ role: "user", content: "the secret question" }] }),
    })
    await res.text()

    const log = readFileSync(debug.file!, "utf8")
    expect(log).not.toContain("secret") // neither prompt, answer nor token
    const events = lines(debug.file!)
    expect(events.map((e) => e.event)).toEqual(["request", "response", "stream"])
    expect(events[0]).toMatchObject({ model: "claude-sonnet-4.5", history: 0, tools: 0, images: 0 })
    expect(events[1]).toMatchObject({ status: 200, requestId: "req-123", host: "q.us-east-1.amazonaws.com" })
    expect(events[2]).toMatchObject({
      outcome: "complete",
      credits: 0.4,
      contextPct: 12,
      frames: { assistantResponseEvent: 1, meteringEvent: 1, brandNewEvent: 1, contextUsageEvent: 1 },
      unknownFrames: { brandNewEvent: ["bar", "foo"] },
    })
  })
})
