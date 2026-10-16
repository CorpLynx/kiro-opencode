import { describe, expect, it, vi } from "vitest"
import { fetchUsage, formatUsage, parseUsage } from "../src/usage.js"

const sample = {
  userInfo: { email: "dev@example.com" },
  subscriptionInfo: { subscriptionTitle: "KIRO PRO" },
  nextDateReset: 1_793_491_200, // epoch seconds
  usageBreakdownList: [
    {
      resourceType: "CREDIT",
      displayName: "Credits",
      currentUsage: 120,
      currentUsageWithPrecision: 120.5,
      usageLimit: 1000,
      usageLimitWithPrecision: 1000,
      freeTrialInfo: { currentUsage: 10, usageLimit: 500, freeTrialExpiry: "2026-12-01T00:00:00Z" },
    },
    { resourceType: "BROKEN" },
  ],
}

describe("usage", () => {
  it("parses usage buckets, preferring precise numbers", () => {
    const u = parseUsage(sample)
    expect(u.email).toBe("dev@example.com")
    expect(u.plan).toBe("KIRO PRO")
    expect(u.buckets).toEqual([
      { name: "Credits", used: 120.5, limit: 1000, resetsAt: "2026-11-01" },
      { name: "Credits (free trial)", used: 10, limit: 500, resetsAt: "2026-12-01" },
    ])
  })

  it("formats a readable summary", () => {
    const text = formatUsage(parseUsage(sample))
    expect(text).toContain("Kiro usage (dev@example.com, KIRO PRO):")
    expect(text).toContain("- Credits: 120.50 / 1000 used, 879.50 left (resets 2026-11-01)")
    expect(formatUsage(parseUsage({}))).toContain("No usage information")
  })

  it("calls getUsageLimits with the token and profile", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify(sample)))
    await fetchUsage(
      { accessToken: "tok", refreshToken: "r", expiresAt: 0, authMethod: "idc", region: "us-east-1", profileArn: "arn:aws:codewhisperer:eu-central-1:1:profile/P" },
      { fetch: f as any },
    )
    const [url, init] = f.mock.calls[0] as any
    expect(String(url)).toMatch(/^https:\/\/q\.eu-central-1\.amazonaws\.com\/getUsageLimits\?/)
    expect(url.searchParams.get("profileArn")).toBe("arn:aws:codewhisperer:eu-central-1:1:profile/P")
    expect(init.headers.authorization).toBe("Bearer tok")
  })

  it("throws a descriptive error on failure", async () => {
    const f = async () => new Response("denied", { status: 403 })
    await expect(
      fetchUsage({ accessToken: "t", refreshToken: "r", expiresAt: 0, authMethod: "social", region: "us-east-1" }, { fetch: f as any }),
    ).rejects.toThrow(/HTTP 403/)
  })
})
