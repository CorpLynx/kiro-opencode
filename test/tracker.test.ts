import { existsSync, mkdtempSync, utimesSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it, vi } from "vitest"
import { accountLine, bar, chipText, formatCredits, formatPct, level, mainBucket } from "../src/tracker/format.js"
import { UsageRecorder } from "../src/tracker/recorder.js"
import { pruneSessions, readAccount, readSession, recordTurn, usageDir, writeAccount } from "../src/tracker/store.js"

const tmp = () => mkdtempSync(join(tmpdir(), "kiro-tracker-"))

describe("usage store", () => {
  it("lives under opencode's state directory", () => {
    expect(usageDir({ XDG_STATE_HOME: "/s" } as any)).toBe("/s/opencode/kiro")
    expect(usageDir({} as any)).toMatch(/\.local\/state\/opencode\/kiro$/)
  })

  it("accumulates credits and turns per session, keeping the latest context figure", () => {
    const dir = tmp()
    recordTurn(dir, { sessionID: "ses_1", credits: 0.1, contextPct: 10, model: "m" }, 1)
    recordTurn(dir, { sessionID: "ses_1", credits: 0.2 }, 2) // no context figure this time
    recordTurn(dir, { sessionID: "ses_2", credits: 5 }, 3)
    expect(readSession(dir, "ses_1")).toEqual({ credits: 0.3, turns: 2, contextPct: 10, model: "m", updated: 2 })
    expect(readSession(dir, "ses_2")?.credits).toBe(5)
    expect(readSession(dir, "missing")).toBeUndefined()
  })

  it("refuses session ids that could escape the directory", () => {
    const dir = tmp()
    expect(recordTurn(dir, { sessionID: "../../etc/passwd", credits: 1 })).toBeUndefined()
    expect(readSession(dir, "../x")).toBeUndefined()
    expect(existsSync(join(dir, "sessions"))).toBe(false)
  })

  it("stores the account snapshot and prunes stale sessions", () => {
    const dir = tmp()
    writeAccount(dir, { buckets: [{ name: "Credits", used: 1, limit: 2 }] }, 7)
    expect(readAccount(dir)).toEqual({ buckets: [{ name: "Credits", used: 1, limit: 2 }], updated: 7 })

    recordTurn(dir, { sessionID: "old" })
    recordTurn(dir, { sessionID: "new" })
    const old = join(dir, "sessions", "old.json")
    utimesSync(old, new Date(0), new Date(0))
    pruneSessions(dir)
    expect(readSession(dir, "old")).toBeUndefined()
    expect(readSession(dir, "new")).toBeDefined()
  })
})

describe("formatting", () => {
  it("draws proportional bars that never look empty or full by rounding", () => {
    expect(bar(50, 10)).toEqual({ filled: "█████", empty: "░░░░░" })
    expect(bar(0.4, 10).filled).toBe("█") // tiny but non-zero
    expect(bar(99.8, 10).empty).toBe("░") // almost, not quite, full
    expect(bar(100, 10).empty).toBe("")
    expect(bar(250, 10).filled.length).toBe(10)
    expect(bar(Number.NaN, 4)).toEqual({ filled: "", empty: "░░░░" })
  })

  it("bands context usage into ok / warn / high", () => {
    expect([0, 59.9, 60, 84.9, 85, 100].map(level)).toEqual(["ok", "ok", "warn", "warn", "high", "high"])
  })

  it("formats percentages and credits", () => {
    expect([formatPct(0), formatPct(0.3), formatPct(57.6), formatPct(140)]).toEqual(["0%", "<1%", "58%", "100%"])
    expect([formatCredits(0), formatCredits(3.214), formatCredits(1234.6)]).toEqual(["0.00", "3.21", "1,235"])
  })

  it("headlines the paid allowance over a free trial", () => {
    const account = {
      updated: 0,
      buckets: [
        { name: "Credits (free trial)", used: 10, limit: 500 },
        { name: "Credits", used: 42, limit: 1000 },
      ],
    }
    expect(mainBucket(account)?.name).toBe("Credits")
    expect(accountLine(account)).toBe("958 / 1,000 left")
    expect(accountLine({ updated: 0, buckets: [] })).toBeUndefined()
    expect(accountLine(undefined)).toBeUndefined()
  })

  it("builds the compact prompt-row text", () => {
    expect(chipText({ credits: 3.2, turns: 4, contextPct: 57.6, updated: 0 })).toBe("58% · 3.20 cr")
    expect(chipText({ credits: 1, turns: 1, updated: 0 })).toBe("1.00 cr")
    expect(chipText(undefined)).toBe("")
  })
})

describe("UsageRecorder", () => {
  it("records turns and refreshes the account at most once per interval", async () => {
    const dir = tmp()
    let now = 1_000_000
    const fetchAccount = vi.fn(async () => ({ buckets: [{ name: "Credits", used: 1, limit: 10 }] }))
    const rec = new UsageRecorder({ dir, fetchAccount, now: () => now, accountIntervalMs: 60_000 })

    rec.turn({ sessionID: "s", credits: 0.5, contextPct: 20, modelId: "m" })
    rec.turn({ sessionID: "s", credits: 0.5, modelId: "m" })
    await rec.refreshAccount()
    expect(readSession(dir, "s")).toMatchObject({ credits: 1, turns: 2, contextPct: 20 })
    expect(fetchAccount).toHaveBeenCalledTimes(1)
    expect(readAccount(dir)?.buckets[0]?.limit).toBe(10)

    now += 61_000
    await rec.refreshAccount()
    expect(fetchAccount).toHaveBeenCalledTimes(2)
  })

  it("swallows failures and skips turns without a session", async () => {
    const dir = tmp()
    const log = vi.fn()
    const rec = new UsageRecorder({ dir, log, fetchAccount: async () => Promise.reject(new Error("offline")) })
    rec.turn({ credits: 1, modelId: "m" })
    await rec.refreshAccount()
    expect(existsSync(join(dir, "sessions"))).toBe(false)
    expect(log).toHaveBeenCalledWith("warn", expect.stringMatching(/account usage/), expect.anything())
  })
})
