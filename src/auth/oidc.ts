import {
  BUILDER_ID_START_URL,
  CLIENT_NAME,
  KIRO_VERSION,
  OIDC_SCOPES,
  normalizeRegion,
  oidcEndpoint,
  socialRefreshEndpoint,
} from "../constants.js"
import type { KiroCredentials } from "./credentials.js"

type Fetch = typeof fetch

export class KiroAuthError extends Error {
  constructor(
    message: string,
    readonly code?: string,
    /** True when the refresh token itself is dead and the user must log in again. */
    readonly fatal = false,
  ) {
    super(message)
    this.name = "KiroAuthError"
  }
}

const JSON_HEADERS = {
  "content-type": "application/json",
  accept: "application/json",
  "user-agent": `KiroIDE-${KIRO_VERSION}`,
}

async function postJson(f: Fetch, url: string, body: unknown, signal?: AbortSignal) {
  const res = await f(url, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body), signal })
  const text = await res.text().catch(() => "")
  let data: any = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    /* non-JSON error page */
  }
  return { res, data, text }
}

export interface DeviceAuthorization {
  verificationUri: string
  verificationUriComplete: string
  userCode: string
  deviceCode: string
  clientId: string
  clientSecret: string
  intervalSec: number
  expiresInSec: number
  region: string
  startUrl: string
}

/** Step 1 of the OIDC device flow: register a public client and ask for a user code. */
export async function startDeviceAuthorization(
  opts: { region?: string; startUrl?: string; fetch?: Fetch } = {},
): Promise<DeviceAuthorization> {
  const f = opts.fetch ?? fetch
  const region = normalizeRegion(opts.region)
  const startUrl = opts.startUrl?.trim() || BUILDER_ID_START_URL
  const base = oidcEndpoint(region)

  const reg = await postJson(f, `${base}/client/register`, {
    clientName: CLIENT_NAME,
    clientType: "public",
    scopes: OIDC_SCOPES,
    grantTypes: ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
  })
  if (!reg.res.ok || !reg.data.clientId || !reg.data.clientSecret) {
    throw new KiroAuthError(`Client registration failed (HTTP ${reg.res.status}): ${reg.text.slice(0, 300)}`)
  }

  const dev = await postJson(f, `${base}/device_authorization`, {
    clientId: reg.data.clientId,
    clientSecret: reg.data.clientSecret,
    startUrl,
  })
  const d = dev.data
  if (!dev.res.ok || !d.deviceCode || !d.userCode || !d.verificationUriComplete) {
    throw new KiroAuthError(`Device authorization failed (HTTP ${dev.res.status}): ${dev.text.slice(0, 300)}`)
  }
  return {
    verificationUri: d.verificationUri,
    verificationUriComplete: d.verificationUriComplete,
    userCode: d.userCode,
    deviceCode: d.deviceCode,
    clientId: reg.data.clientId,
    clientSecret: reg.data.clientSecret,
    intervalSec: d.interval ?? 5,
    expiresInSec: d.expiresIn ?? 600,
    region,
    startUrl,
  }
}

/** Step 2: poll the token endpoint until the user approves (or the code expires). */
export async function pollDeviceToken(
  auth: DeviceAuthorization,
  opts: { fetch?: Fetch; signal?: AbortSignal; sleep?: (ms: number) => Promise<void>; now?: () => number } = {},
): Promise<KiroCredentials> {
  const f = opts.fetch ?? fetch
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
  const now = opts.now ?? Date.now
  const deadline = now() + auth.expiresInSec * 1000
  let interval = auth.intervalSec * 1000

  while (now() < deadline) {
    await sleep(interval)
    if (opts.signal?.aborted) throw new KiroAuthError("Login cancelled")
    const { res, data, text } = await postJson(
      f,
      `${oidcEndpoint(auth.region)}/token`,
      {
        clientId: auth.clientId,
        clientSecret: auth.clientSecret,
        deviceCode: auth.deviceCode,
        grantType: "urn:ietf:params:oauth:grant-type:device_code",
      },
      opts.signal,
    )
    if (data.accessToken && data.refreshToken) {
      return {
        accessToken: data.accessToken,
        refreshToken: data.refreshToken,
        expiresAt: now() + (data.expiresIn ?? 3600) * 1000,
        authMethod: "idc",
        region: auth.region,
        clientId: auth.clientId,
        clientSecret: auth.clientSecret,
      }
    }
    switch (data.error) {
      case "authorization_pending":
        continue
      case "slow_down":
        interval += 5000
        continue
      case "expired_token":
        throw new KiroAuthError("The login code expired. Please start again.", data.error, true)
      case "access_denied":
        throw new KiroAuthError("Login was denied.", data.error, true)
      default:
        throw new KiroAuthError(
          `Token request failed (HTTP ${res.status}): ${data.error ?? ""} ${data.error_description ?? text.slice(0, 200)}`,
          data.error,
        )
    }
  }
  throw new KiroAuthError("Timed out waiting for login approval.", "expired_token", true)
}

/** Exchange the refresh token for a fresh access token. */
export async function refreshCredentials(
  current: KiroCredentials,
  opts: { fetch?: Fetch; signal?: AbortSignal; now?: () => number } = {},
): Promise<KiroCredentials> {
  const f = opts.fetch ?? fetch
  const now = opts.now ?? Date.now
  const isIdc = current.authMethod === "idc"

  if (isIdc && (!current.clientId || !current.clientSecret)) {
    throw new KiroAuthError("Missing OIDC client registration; please log in again.", "missing_client", true)
  }
  const url = isIdc
    ? `${oidcEndpoint(current.oidcRegion ?? current.region)}/token`
    : socialRefreshEndpoint(current.region)
  const body = isIdc
    ? {
        grantType: "refresh_token",
        refreshToken: current.refreshToken,
        clientId: current.clientId,
        clientSecret: current.clientSecret,
      }
    : { refreshToken: current.refreshToken }

  const { res, data, text } = await postJson(f, url, body, opts.signal)
  const accessToken = data.accessToken ?? data.access_token
  if (!res.ok || !accessToken) {
    const fatal = res.status === 400 || res.status === 401 || res.status === 403
    throw new KiroAuthError(
      `Token refresh failed (HTTP ${res.status}): ${data.error ?? data.message ?? text.slice(0, 200)}`,
      data.error ?? `http_${res.status}`,
      fatal,
    )
  }
  return {
    ...current,
    accessToken,
    refreshToken: data.refreshToken ?? data.refresh_token ?? current.refreshToken,
    expiresAt: now() + (data.expiresIn ?? data.expires_in ?? 3600) * 1000,
    profileArn: data.profileArn ?? current.profileArn,
  }
}
