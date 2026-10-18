import { describe, expect, it } from "vitest"
import { addTrailers, supportsTrailers, trailersFor } from "../src/provenance.js"

const T = trailersFor({ enabled: true }, { model: "github-copilot/claude-sonnet-5", sessionID: "ses_abc123" })

describe("commit provenance", () => {
  it("builds trailers from the model and session", () => {
    expect(T).toEqual(["Assisted-by: opencode (github-copilot/claude-sonnet-5)", "Opencode-Session: ses_abc123"])
    expect(trailersFor({ enabled: true, trailer: "AI-Generated", session: false }, { model: "m" })).toEqual(["AI-Generated: opencode (m)"])
    expect(trailersFor({ enabled: false }, { model: "m" })).toEqual([])
    expect(trailersFor(undefined, {})).toEqual([])
    // anything shell-unsafe is left out rather than quoted into a command
    expect(trailersFor({ enabled: true }, { model: "x'; rm -rf ~", sessionID: "s$(id)" })).toEqual(["Assisted-by: opencode"])
  })

  it("adds the trailers to each git commit and nothing else", () => {
    const flags = " --trailer 'Assisted-by: opencode (github-copilot/claude-sonnet-5)' --trailer 'Opencode-Session: ses_abc123'"
    expect(addTrailers(`git commit -m "fix it"`, T)).toBe(`git commit${flags} -m "fix it"`)
    expect(addTrailers(`git add . && git -C repo commit -am wip`, T)).toBe(`git add . && git -C repo commit${flags} -am wip`)
    expect(addTrailers(`git commit-tree abc`, T)).toBe(`git commit-tree abc`)
    expect(addTrailers(`git status`, T)).toBe(`git status`)
  })

  it("doesn't stamp a command twice", () => {
    const once = addTrailers(`git commit -m x`, T)
    expect(addTrailers(once, T)).toBe(once)
  })

  it("knows which git versions support --trailer", () => {
    expect(supportsTrailers("git version 2.43.0")).toBe(true)
    expect(supportsTrailers("git version 2.32.0.windows.1")).toBe(true)
    expect(supportsTrailers("git version 2.31.1")).toBe(false)
    expect(supportsTrailers(undefined)).toBe(false)
  })
})
