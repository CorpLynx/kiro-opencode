import { randomUUID } from "node:crypto"
import { platform, release } from "node:os"
import { apiEndpoint, KIRO_VERSION } from "./constants.js"
import { apiRegion, type KiroCredentials } from "./auth/credentials.js"
import type { TokenManager } from "./auth/manager.js"
import { KiroAuthError } from "./auth/oidc.js"
import { transformRequest, type OpenAIRequest } from "./transform/request.js"
import { chunksToCompletion, chunksToSSE, toChatChunks } from "./transform/response.js"

export interface KiroFetchOptions {
  tokens: TokenManager
  /** Underlying transport; defaults to global fetch. */
  fetch?: typeof fetch
  contextWindow?: (modelId: string) => number
  log?: (level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>) => void
  maxRetries?: number
  sleep?: (ms: number) => Promise<void>
}

function userAgent() {
  const os = `${platform()}#${release()}`
  return `aws-sdk-js/1.0.27 ua/2.1 os/${os} lang/js md/nodejs#${process.versions.node} api/codewhispererstreaming#1.0.27 m/E KiroIDE-${KIRO_VERSION}`
}

function errorResponse(status: number, message: string, code: string, extra?: HeadersInit) {
  return new Response(JSON.stringify({ error: { message, type: code, code } }), {
    status,
    headers: { "content-type": "application/json", ...extra },
  })
}

/** A token that can't be obtained or refreshed becomes a 401 the user can act on, not a crash. */
function authFailure(err: unknown, log: NonNullable<KiroFetchOptions["log"]>): Response {
  const message = err instanceof Error ? err.message : String(err)
  log("error", "Kiro authentication failed", { error: message })
  if (!(err instanceof KiroAuthError && err.fatal)) {
    // Network trouble or a transient refresh failure: the session itself may still be fine.
    return errorResponse(503, `Could not refresh the Kiro session: ${message}`, "authentication_unavailable")
  }
  const hint = /opencode auth login/.test(message) ? "" : " Run `opencode auth login` and choose Kiro."
  return errorResponse(401, `${message}${hint}`, "authentication_error")
}

/** Translate a failed Kiro response into an OpenAI-style error opencode can display. */
async function mapError(res: Response): Promise<Response> {
  const text = await res.text().catch(() => "")
  let data: any = {}
  try {
    data = JSON.parse(text)
  } catch {
    /* plain text */
  }
  const raw: string = data.message ?? data.Message ?? (text.slice(0, 500) || res.statusText)
  const reason: string = data.reason ?? ""
  const retryAfter = res.headers.get("retry-after")
  const extra = retryAfter ? { "retry-after": retryAfter } : undefined

  if (reason === "CONTENT_LENGTH_EXCEEDS_THRESHOLD" || /input is too long|content length exceeds/i.test(raw)) {
    // Worded to match opencode's context-overflow patterns, so it compacts the session and retries.
    return errorResponse(400, "Prompt is too long: the request exceeds the context window of this Kiro model", "context_length_exceeded")
  }
  if (res.status === 401 || res.status === 403) {
    return errorResponse(res.status, `Kiro rejected the credentials: ${raw}. Re-run \`opencode auth login\`.`, "authentication_error", extra)
  }
  if (res.status === 429 || reason === "MONTHLY_REQUEST_COUNT") {
    return errorResponse(429, `Kiro rate limit or quota reached: ${raw}`, "rate_limit_exceeded", extra)
  }
  return errorResponse(res.status, `Kiro API error (${res.status}): ${raw}`, reason.toLowerCase() || "api_error", extra)
}

export function createKiroFetch(opts: KiroFetchOptions): typeof fetch {
  const transport = opts.fetch ?? fetch
  const maxRetries = opts.maxRetries ?? 3
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
  const log = opts.log ?? (() => {})

  return (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url
    if (!new URL(url).pathname.endsWith("/chat/completions")) {
      return errorResponse(404, `Kiro provider does not support ${new URL(url).pathname}`, "not_found")
    }

    const raw = init?.body ?? (input instanceof Request ? await input.text() : undefined)
    let request: OpenAIRequest
    try {
      request = JSON.parse(typeof raw === "string" ? raw : "")
    } catch {
      return errorResponse(400, "Kiro provider expects a JSON chat completion request", "invalid_request_error")
    }
    const signal = init?.signal ?? undefined

    let creds: KiroCredentials
    try {
      creds = await opts.tokens.get()
    } catch (err) {
      return authFailure(err, log)
    }
    const transformed = transformRequest(request, { profileArn: creds.profileArn })
    // A refresh can hand us a profile ARN the first attempt didn't have.
    const payload = () =>
      JSON.stringify(creds.profileArn ? { ...transformed.body, profileArn: creds.profileArn } : transformed.body)

    /** Back off without outliving an abort. */
    const pause = (ms: number) =>
      new Promise<void>((resolve, reject) => {
        if (signal?.aborted) return reject(signal.reason)
        const onAbort = () => reject(signal!.reason)
        signal?.addEventListener("abort", onAbort, { once: true })
        sleep(ms).then(() => {
          signal?.removeEventListener("abort", onAbort)
          resolve()
        })
      })

    let refreshedOnAuthError = false
    let res: Response
    for (let attempt = 0; ; attempt++) {
      const endpoint = `${apiEndpoint(apiRegion(creds))}/generateAssistantResponse`
      try {
        res = await transport(endpoint, {
          method: "POST",
          signal,
          body: payload(),
          headers: {
            authorization: `Bearer ${creds.accessToken}`,
            "content-type": "application/json",
            accept: "application/json",
            "amz-sdk-invocation-id": randomUUID(),
            "amz-sdk-request": `attempt=${attempt + 1}; max=${maxRetries + 1}`,
            "x-amzn-kiro-agent-mode": "vibe",
            "x-amz-user-agent": `aws-sdk-js/1.0.27 KiroIDE-${KIRO_VERSION}`,
            "user-agent": userAgent(),
          },
        })
      } catch (err) {
        if (signal?.aborted || attempt >= maxRetries) throw err
        log("warn", "Kiro request failed, retrying", { attempt, error: String(err) })
        await pause(backoff(attempt))
        continue
      }

      if ((res.status === 401 || res.status === 403) && !refreshedOnAuthError) {
        refreshedOnAuthError = true
        await res.body?.cancel().catch(() => {})
        try {
          creds = await opts.tokens.get(creds.accessToken)
        } catch (err) {
          return authFailure(err, log)
        }
        continue
      }
      if ((res.status === 429 || res.status >= 500) && res.status !== 501 && attempt < maxRetries) {
        const wait = retryAfterMs(res) ?? backoff(attempt)
        log("warn", `Kiro returned ${res.status}, retrying`, { attempt, wait })
        await res.body?.cancel().catch(() => {})
        await pause(wait)
        continue
      }
      break
    }

    if (!res.ok || !res.body) return mapError(res)

    const chunks = toChatChunks(res.body, {
      request: transformed,
      contextWindow: opts.contextWindow?.(transformed.modelId),
      signal,
    })
    if (!transformed.stream) {
      try {
        return Response.json(await chunksToCompletion(chunks))
      } catch (err) {
        return errorResponse(502, err instanceof Error ? err.message : String(err), "stream_error")
      }
    }
    return new Response(chunksToSSE(chunks, signal), {
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
    })
  }) as typeof fetch
}

const backoff = (attempt: number) => Math.min(1000 * 2 ** attempt, 8000) + Math.floor(Math.random() * 250)

function retryAfterMs(res: Response): number | undefined {
  const v = res.headers.get("retry-after")
  if (!v) return undefined
  const secs = Number(v)
  if (Number.isFinite(secs)) return Math.min(secs * 1000, 30_000)
  const at = Date.parse(v)
  return Number.isNaN(at) ? undefined : Math.min(Math.max(at - Date.now(), 0), 30_000)
}
