import { randomUUID } from "node:crypto"
import { platform, release } from "node:os"
import { apiEndpoint, KIRO_VERSION, SESSION_HEADER } from "./constants.js"
import { apiRegion, type KiroCredentials } from "./auth/credentials.js"
import type { TokenManager } from "./auth/manager.js"
import { KiroAuthError } from "./auth/oidc.js"
import { transformRequest, type OpenAIRequest } from "./transform/request.js"
import { noDebug, redact, type Debug } from "./debug.js"
import {
  chunksToCompletion,
  chunksToSSE,
  RETRYABLE_STREAM_ERRORS,
  toChatChunks,
  type ChatChunk,
  type TurnUsage,
} from "./transform/response.js"

export interface KiroFetchOptions {
  tokens: TokenManager
  /** Underlying transport; defaults to global fetch. */
  fetch?: typeof fetch
  contextWindow?: (modelId: string) => number
  log?: (level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>) => void
  maxRetries?: number
  sleep?: (ms: number) => Promise<void>
  /** Reports what Kiro charged for each response, tagged with the opencode session. */
  onUsage?: (usage: TurnUsage & { sessionID?: string; modelId: string }) => void
  debug?: Debug
}

/**
 * A prompt longer than this many characters per token of context window is certainly too big:
 * text and code run at roughly 3–4 characters per token, so this leaves generous headroom
 * against false alarms (which would only cost an early compaction).
 */
const MAX_CHARS_PER_TOKEN = 5

/** Same wording as Kiro's own overflow error, so opencode treats both alike (compact and retry). */
const OVERFLOW_MESSAGE = "Prompt is too long: the request exceeds the context window of this Kiro model"

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
function authFailure(err: unknown, log: NonNullable<KiroFetchOptions["log"]>, debug: Debug = noDebug): Response {
  const message = err instanceof Error ? err.message : String(err)
  log("error", "Kiro authentication failed", { error: message })
  debug("auth.failed", {
    code: err instanceof KiroAuthError ? err.code : undefined,
    fatal: err instanceof KiroAuthError ? err.fatal : false,
    message: redact(message),
  })
  if (!(err instanceof KiroAuthError && err.fatal)) {
    // Network trouble or a transient refresh failure: the session itself may still be fine.
    return errorResponse(503, `Could not refresh the Kiro session: ${message}`, "authentication_unavailable")
  }
  const hint = /opencode auth login/.test(message) ? "" : " Run `opencode auth login` and choose Kiro."
  return errorResponse(401, `${message}${hint}`, "authentication_error")
}

/** Translate a failed Kiro response into an OpenAI-style error opencode can display. */
async function mapError(res: Response, debug: Debug = noDebug): Promise<Response> {
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
  debug("response.error", {
    status: res.status,
    reason: reason || undefined,
    errorType: res.headers.get("x-amzn-errortype") ?? undefined,
    requestId: res.headers.get("x-amzn-requestid") ?? undefined,
    message: redact(raw),
  })

  if (reason === "CONTENT_LENGTH_EXCEEDS_THRESHOLD" || /input is too long|content length exceeds/i.test(raw)) {
    // Worded to match opencode's context-overflow patterns, so it compacts the session and retries.
    return errorResponse(400, OVERFLOW_MESSAGE, "context_length_exceeded")
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
  const debug = opts.debug ?? noDebug

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
    const sessionID =
      new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined)).get(SESSION_HEADER) ??
      undefined

    let creds: KiroCredentials
    try {
      creds = await opts.tokens.get()
    } catch (err) {
      return authFailure(err, log, debug)
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

    const contextWindow = opts.contextWindow?.(transformed.modelId)
    const state = transformed.body.conversationState
    const images = [state.currentMessage.userInputMessage, ...(state.history ?? []).map((h) => ("userInputMessage" in h ? h.userInputMessage : undefined))]
      .flatMap((m) => m?.images ?? [])
    const totalChars = payload().length
    // Image bytes are base64 and say little about token count; measure the text alone.
    const textChars = totalChars - images.reduce((n, img) => n + img.source.bytes.length, 0)
    const summary = {
      model: transformed.modelId,
      conversationId: transformed.conversationId,
      history: state.history?.length ?? 0,
      tools: state.currentMessage.userInputMessage.userInputMessageContext?.tools?.length ?? 0,
      toolResults: state.currentMessage.userInputMessage.userInputMessageContext?.toolResults?.length ?? 0,
      images: images.length,
      chars: totalChars,
      thinking: transformed.thinking,
      tracked: sessionID !== undefined,
    }

    if (contextWindow && textChars > contextWindow * MAX_CHARS_PER_TOKEN) {
      // Certain to be rejected: answer locally so opencode compacts without a wasted Kiro request.
      debug("request.oversized", { ...summary, textChars, contextWindow })
      return errorResponse(400, OVERFLOW_MESSAGE, "context_length_exceeded")
    }
    debug("request", summary)

    let refreshedOnAuthError = false
    for (let attempt = 0; ; attempt++) {
      const endpoint = `${apiEndpoint(apiRegion(creds))}/generateAssistantResponse`
      const sentAt = Date.now()
      let res: Response
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
        debug("retry", { attempt, reason: "network", message: redact(String(err)) })
        await pause(backoff(attempt))
        continue
      }
      debug("response", {
        attempt,
        status: res.status,
        ms: Date.now() - sentAt,
        host: new URL(endpoint).host,
        requestId: res.headers.get("x-amzn-requestid") ?? undefined,
      })

      if ((res.status === 401 || res.status === 403) && !refreshedOnAuthError) {
        refreshedOnAuthError = true
        await res.body?.cancel().catch(() => {})
        debug("retry", { attempt, reason: `http_${res.status}`, action: "refresh token" })
        try {
          creds = await opts.tokens.get(creds.accessToken)
        } catch (err) {
          return authFailure(err, log, debug)
        }
        continue
      }
      if ((res.status === 429 || res.status >= 500) && res.status !== 501 && attempt < maxRetries) {
        const wait = retryAfterMs(res) ?? backoff(attempt)
        log("warn", `Kiro returned ${res.status}, retrying`, { attempt, wait })
        debug("retry", { attempt, reason: `http_${res.status}`, waitMs: wait })
        await res.body?.cancel().catch(() => {})
        await pause(wait)
        continue
      }
      if (!res.ok || !res.body) return mapError(res, debug)

      const chunks = toChatChunks(res.body, {
        request: transformed,
        contextWindow,
        signal,
        onUsage: opts.onUsage && ((u) => opts.onUsage!({ ...u, sessionID, modelId: transformed.modelId })),
        debug: debug.enabled ? debug : undefined,
      })

      // A transient failure before anything reached the user can simply be retried.
      const head = await peek(chunks)
      if (head.failure && RETRYABLE_STREAM_ERRORS.has(head.failure) && attempt < maxRetries) {
        await chunks.return(undefined)
        const wait = backoff(attempt)
        log("warn", `Kiro stream failed before any output (${head.failure}), retrying`, { attempt, wait })
        debug("retry", { attempt, reason: head.failure, waitMs: wait })
        await pause(wait)
        continue
      }
      const all = replay(head.chunks, chunks)

      if (!transformed.stream) {
        try {
          return Response.json(await chunksToCompletion(all))
        } catch (err) {
          return errorResponse(502, err instanceof Error ? err.message : String(err), "stream_error")
        }
      }
      return new Response(chunksToSSE(all, signal), {
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      })
    }
  }) as typeof fetch
}

/** Read chunks up to the first one that carries output, an error or usage, or the end. */
async function peek(chunks: AsyncGenerator<ChatChunk>): Promise<{ chunks: ChatChunk[]; failure?: string }> {
  const head: ChatChunk[] = []
  for (;;) {
    const { done, value } = await chunks.next()
    if (done) return { chunks: head }
    head.push(value)
    if (value.error) return { chunks: head, failure: value.error.code }
    const d = value.choices[0]?.delta as Record<string, unknown> | undefined
    if (value.usage || d?.tool_calls || d?.reasoning_content || (typeof d?.content === "string" && d.content)) {
      return { chunks: head }
    }
  }
}

/** The peeked chunks followed by the rest of the stream; closing it closes the stream too. */
async function* replay(head: ChatChunk[], rest: AsyncGenerator<ChatChunk>): AsyncGenerator<ChatChunk> {
  try {
    yield* head
    yield* rest
  } finally {
    await rest.return(undefined)
  }
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
