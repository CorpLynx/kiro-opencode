import { createHash, randomBytes } from "node:crypto"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import { DEFAULT_REGION, KIRO_VERSION, socialAuthBase } from "../constants.js"
import type { KiroCredentials } from "./credentials.js"
import { KiroAuthError } from "./oidc.js"

/**
 * Google / GitHub sign-in, the way the Kiro IDE and kiro-cli do it: Kiro's sign-in portal with a
 * PKCE challenge, a redirect back to a loopback listener, then a code exchange with Kiro's
 * desktop auth service. The resulting session refreshes through that same service.
 *
 * Kiro only accepts `http://localhost:3128` as the redirect, so the port is fixed. This flow is
 * based on third-party documentation of Kiro's protocol (it is not officially documented).
 */
export type SocialProvider = "google" | "github"

export const SOCIAL_PORT = 3128
const TIMEOUT_MS = 5 * 60 * 1000

const signinUrl = () => process.env.KIRO_SIGNIN_URL?.replace(/\/+$/, "") || "https://app.kiro.dev/signin"
const b64url = (buf: Buffer) => buf.toString("base64url")

export interface SocialLogin {
  /** Open this in the browser. */
  url: string
  /** Resolves once the browser comes back and the code is exchanged. */
  wait: () => Promise<KiroCredentials>
  close: () => void
}

function page(title: string, body: string) {
  return `<!doctype html><meta charset="utf-8"><title>${title}</title>
<body style="font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#222">
<h1 style="font-size:1.4rem">${title}</h1><p>${body}</p></body>`
}

export async function startSocialLogin(
  provider: SocialProvider,
  opts: { port?: number; timeoutMs?: number; fetch?: typeof fetch; now?: () => number } = {},
): Promise<SocialLogin> {
  const port = opts.port ?? SOCIAL_PORT
  const f = opts.fetch ?? fetch
  const now = opts.now ?? Date.now
  const verifier = b64url(randomBytes(32))
  const challenge = b64url(createHash("sha256").update(verifier).digest())
  const state = b64url(randomBytes(16))
  const redirectBase = `http://localhost:${port}`

  let settle!: { resolve: (c: KiroCredentials) => void; reject: (e: Error) => void }
  const result = new Promise<KiroCredentials>((resolve, reject) => (settle = { resolve, reject }))
  result.catch(() => {}) // surfaced through wait()
  let done = false

  const exchange = async (code: string, loginOption: string): Promise<KiroCredentials> => {
    const res = await f(`${socialAuthBase(DEFAULT_REGION)}/oauth/token`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json", "user-agent": `KiroIDE-${KIRO_VERSION}` },
      // Must repeat the redirect exactly as the portal registered it, or Kiro rejects (and burns) the code.
      body: JSON.stringify({
        code,
        code_verifier: verifier,
        redirect_uri: `${redirectBase}/oauth/callback?login_option=${encodeURIComponent(loginOption)}`,
      }),
    })
    const text = await res.text()
    let data: any = {}
    try {
      data = JSON.parse(text)
    } catch {
      /* not JSON */
    }
    const accessToken = data.accessToken ?? data.access_token
    const refreshToken = data.refreshToken ?? data.refresh_token
    if (!res.ok || !accessToken || !refreshToken) {
      throw new KiroAuthError(`Kiro sign-in failed (HTTP ${res.status}): ${data.message ?? text.slice(0, 200)}`, "exchange_failed", true)
    }
    return {
      accessToken,
      refreshToken,
      expiresAt: now() + (data.expiresIn ?? data.expires_in ?? 3600) * 1000,
      authMethod: "social",
      region: DEFAULT_REGION,
      ...(data.profileArn && { profileArn: data.profileArn }),
    }
  }

  const handle = async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url ?? "/", redirectBase)
    if (url.pathname !== "/oauth/callback") {
      res.writeHead(404).end()
      return
    }
    const reply = (status: number, title: string, body: string) =>
      res.writeHead(status, { "content-type": "text/html; charset=utf-8" }).end(page(title, body))
    if (url.searchParams.get("state") !== state) return reply(400, "Sign-in link expired", "Start the sign-in again from opencode.")
    if (done) return reply(200, "Already signed in", "You can close this tab.")
    const error = url.searchParams.get("error")
    const code = url.searchParams.get("code")
    if (error || !code) {
      done = true
      settle.reject(new KiroAuthError(`Kiro sign-in was not completed: ${error ?? "no code returned"}`, error ?? "no_code", true))
      return reply(400, "Sign-in not completed", "Return to opencode and try again.")
    }
    done = true // a code is single-use: never exchange twice, even if the browser retries
    try {
      const creds = await exchange(code, url.searchParams.get("login_option") ?? provider)
      reply(200, "Signed in to Kiro", "You can close this tab and return to opencode.")
      settle.resolve(creds)
    } catch (err) {
      reply(502, "Sign-in failed", "Return to opencode for details.")
      settle.reject(err as Error)
    }
  }

  // Listen on both loopback addresses (browsers resolve "localhost" either way), never on all interfaces.
  const servers: Server[] = []
  const listen = (host: string) =>
    new Promise<void>((resolve, reject) => {
      const server = createServer((req, res) => void handle(req, res))
      server.once("error", reject)
      server.listen(port, host, () => {
        servers.push(server)
        resolve()
      })
    })
  try {
    await listen("127.0.0.1")
  } catch (err) {
    const busy = (err as NodeJS.ErrnoException).code === "EADDRINUSE"
    throw new KiroAuthError(
      busy
        ? `Port ${port} is in use (Kiro only accepts sign-in redirects to localhost:${port}). Close whatever is using it, or sign in with the Kiro IDE and choose "Import existing login".`
        : `Could not start the sign-in listener: ${String(err)}`,
      busy ? "port_in_use" : "listen_failed",
    )
  }
  await listen("::1").catch(() => {}) // no IPv6 loopback: fine

  const close = () => {
    for (const s of servers) s.close()
    servers.length = 0
  }
  const timer = setTimeout(() => {
    if (done) return
    done = true
    settle.reject(new KiroAuthError("Timed out waiting for the browser sign-in.", "timeout", true))
  }, opts.timeoutMs ?? TIMEOUT_MS)
  timer.unref?.()
  void result.then(close, close).finally(() => clearTimeout(timer))

  const url = new URL(signinUrl())
  url.searchParams.set("redirect_uri", redirectBase)
  url.searchParams.set("code_challenge", challenge)
  url.searchParams.set("code_challenge_method", "S256")
  url.searchParams.set("state", state)
  url.searchParams.set("redirect_from", "KiroIDE")
  url.searchParams.set("signin_methods", provider)

  return { url: url.toString(), wait: () => result, close }
}
