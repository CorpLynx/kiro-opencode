import { createServer } from "node:net"
import { describe, expect, it, vi } from "vitest"
import { startSocialLogin } from "../src/auth/social.js"

/** A free port, so tests never collide with a real sign-in on 3128. */
const freePort = () =>
  new Promise<number>((resolve) => {
    const s = createServer().listen(0, "127.0.0.1", () => {
      const port = (s.address() as { port: number }).port
      s.close(() => resolve(port))
    })
  })
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

describe("Google / GitHub sign-in", () => {
  it("builds the Kiro sign-in URL with a PKCE challenge", async () => {
    const port = await freePort()
    const login = await startSocialLogin("github", { port, fetch: vi.fn() as any })
    try {
      const url = new URL(login.url)
      expect(url.origin + url.pathname).toBe("https://app.kiro.dev/signin")
      expect(url.searchParams.get("redirect_uri")).toBe(`http://localhost:${port}`)
      expect(url.searchParams.get("code_challenge_method")).toBe("S256")
      expect(url.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/)
      expect(url.searchParams.get("signin_methods")).toBe("github")
      expect(url.searchParams.get("redirect_from")).toBe("KiroIDE")
      expect(url.searchParams.get("state")).toBeTruthy()
    } finally {
      login.close()
    }
  })

  it("exchanges the code from the browser redirect for a social session", async () => {
    const port = await freePort()
    const exchange = vi.fn(async () => json({ accessToken: "acc", refreshToken: "ref", expiresIn: 3600, profileArn: "arn:p" }))
    const login = await startSocialLogin("google", { port, fetch: exchange as any, now: () => 1000 })
    const state = new URL(login.url).searchParams.get("state")!

    const page = await fetch(`http://127.0.0.1:${port}/oauth/callback?login_option=google&code=the-code&state=${state}`)
    expect(page.status).toBe(200)
    expect(await page.text()).toContain("Signed in to Kiro")
    expect(await login.wait()).toEqual({
      accessToken: "acc",
      refreshToken: "ref",
      expiresAt: 3_601_000,
      authMethod: "social",
      region: "us-east-1",
      profileArn: "arn:p",
    })
    const [url, init] = exchange.mock.calls[0] as any
    expect(url).toBe("https://prod.us-east-1.auth.desktop.kiro.dev/oauth/token")
    const body = JSON.parse(init.body)
    expect(body).toMatchObject({ code: "the-code", redirect_uri: `http://localhost:${port}/oauth/callback?login_option=google` })
    expect(body.code_verifier).toMatch(/^[A-Za-z0-9_-]{43}$/)
    // the listener is gone once the sign-in completes
    await expect(fetch(`http://127.0.0.1:${port}/oauth/callback`)).rejects.toThrow()
  })

  it("ignores redirects with the wrong state and reports provider errors", async () => {
    const port = await freePort()
    const exchange = vi.fn()
    const login = await startSocialLogin("google", { port, fetch: exchange as any })
    const state = new URL(login.url).searchParams.get("state")!
    expect((await fetch(`http://127.0.0.1:${port}/oauth/callback?code=x&state=forged`)).status).toBe(400)
    expect((await fetch(`http://127.0.0.1:${port}/elsewhere`)).status).toBe(404)
    await fetch(`http://127.0.0.1:${port}/oauth/callback?error=access_denied&state=${state}`)
    await expect(login.wait()).rejects.toThrow(/access_denied/)
    expect(exchange).not.toHaveBeenCalled()
  })

  it("explains a busy port instead of failing obscurely", async () => {
    const port = await freePort()
    const blocker = createServer().listen(port, "127.0.0.1")
    await new Promise((r) => blocker.once("listening", r))
    try {
      await expect(startSocialLogin("google", { port })).rejects.toThrow(/in use.*Import existing login/)
    } finally {
      blocker.close()
    }
  })

  it("times out if the browser never comes back", async () => {
    const port = await freePort()
    const login = await startSocialLogin("google", { port, timeoutMs: 50, fetch: vi.fn() as any })
    await expect(login.wait()).rejects.toThrow(/Timed out/)
  })
})
