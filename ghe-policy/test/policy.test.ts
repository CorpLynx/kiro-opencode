import { describe, expect, it } from "vitest"
import { enforce, LOCKDOWN_POLICY, parsePolicy } from "../src/policy.js"

describe("parsePolicy", () => {
  it("accepts a full policy", () => {
    const p = parsePolicy({
      version: 1,
      name: "baseline",
      opencode: { share: "disabled" },
      mcp: { allow: ["github"] },
      instructions: ["Be careful."],
      guard: { protectedBranches: ["main"], blockForcePush: true, allowedHosts: ["*.gov"], deny: [{ pattern: "x", reason: "no x" }] },
      provenance: { trailer: "AI-Assisted" },
    })
    expect(p.provenance).toEqual({ enabled: true, trailer: "AI-Assisted", session: true })
    expect(p.guard?.deny).toEqual([{ pattern: "x", reason: "no x" }])
  })

  it("rejects malformed policies with a pointed message", () => {
    expect(() => parsePolicy(null)).toThrow(/JSON object/)
    expect(() => parsePolicy({ version: 2 })).toThrow(/unsupported policy version 2/)
    expect(() => parsePolicy({ version: 1, guard: { protectedBranches: "main" } })).toThrow(/guard.protectedBranches must be a list/)
    expect(() => parsePolicy({ version: 1, guard: { deny: [{ pattern: "(" , reason: "r" }] } })).toThrow(/guard.deny\[0\].pattern is not a valid regular expression/)
    expect(() => parsePolicy({ version: 1, guard: { deny: [{ pattern: "x" }] } })).toThrow(/needs a "pattern" and a "reason"/)
    expect(() => parsePolicy({ version: 1, opencode: { plugin: ["x"] } })).toThrow(/opencode.plugin cannot be set/)
    expect(() => parsePolicy({ version: 1, provenance: { trailer: "bad key!" } })).toThrow(/trailer key/)
  })

  it("has a strict lockdown fallback", () => {
    expect(LOCKDOWN_POLICY.opencode).toMatchObject({ share: "disabled", permission: { bash: "ask", webfetch: "deny" } })
    expect(LOCKDOWN_POLICY.mcp?.allow).toEqual([])
  })
})

describe("enforce", () => {
  it("lets the policy win on every key it sets, and only those", () => {
    const user = { share: "auto", model: "x/y", permission: { bash: "allow", edit: "allow" }, instructions: ["mine.md"] }
    const out = enforce(user, { share: "disabled", permission: { bash: { "git push*": "deny", "*": "ask" } }, instructions: ["org.md"] })
    expect(out).toEqual({
      share: "disabled",
      model: "x/y",
      permission: { bash: { "git push*": "deny", "*": "ask" }, edit: "allow" },
      instructions: ["org.md"], // lists are replaced, not merged
    })
    expect(user.share).toBe("auto") // input untouched
  })
})
