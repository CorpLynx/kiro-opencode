import { EXPIRY_SKEW_MS, normalizeRegion } from "../constants.js"

/**
 * Everything Kiro needs to call the API and to refresh the session.
 *
 * opencode's auth store only has room for `refresh`/`access`/`expires`, so the extra
 * fields (OIDC client registration, region, profile ARN) ride along inside the `refresh`
 * string as a versioned, base64url-encoded JSON blob.
 */
export interface KiroCredentials {
  accessToken: string
  refreshToken: string
  /** Epoch milliseconds. */
  expiresAt: number
  /** `idc` = AWS SSO OIDC (Builder ID / IAM Identity Center); `social` = Kiro desktop login (Google/GitHub). */
  authMethod: "idc" | "social"
  /** Region of Kiro's API (and of the desktop auth service for social logins). */
  region: string
  /** Region of the OIDC endpoint (the Identity Center region), when it differs from `region`. */
  oidcRegion?: string
  clientId?: string
  clientSecret?: string
  profileArn?: string
}

const PREFIX = "kiro1."

interface Blob {
  r: string
  m: "idc" | "social"
  g: string
  o?: string
  c?: string
  s?: string
  p?: string
}

export function encodeRefresh(c: Omit<KiroCredentials, "accessToken" | "expiresAt">): string {
  const blob: Blob = {
    r: c.refreshToken,
    m: c.authMethod,
    g: c.region,
    ...(c.oidcRegion && { o: c.oidcRegion }),
    ...(c.clientId && { c: c.clientId }),
    ...(c.clientSecret && { s: c.clientSecret }),
    ...(c.profileArn && { p: c.profileArn }),
  }
  return PREFIX + Buffer.from(JSON.stringify(blob)).toString("base64url")
}

export function decodeRefresh(
  refresh: string,
): Omit<KiroCredentials, "accessToken" | "expiresAt"> {
  if (!refresh.startsWith(PREFIX)) {
    // A bare refresh token (e.g. pasted by hand): assume the social flow.
    return { refreshToken: refresh, authMethod: "social", region: normalizeRegion(undefined) }
  }
  const blob = JSON.parse(Buffer.from(refresh.slice(PREFIX.length), "base64url").toString("utf8")) as Blob
  return {
    refreshToken: blob.r,
    authMethod: blob.m,
    region: normalizeRegion(blob.g),
    oidcRegion: blob.o,
    clientId: blob.c,
    clientSecret: blob.s,
    profileArn: blob.p,
  }
}

/** Shape stored in opencode's auth.json (`type: "oauth"`). */
export interface StoredOAuth {
  refresh: string
  access: string
  expires: number
}

export function toStored(c: KiroCredentials): StoredOAuth {
  return { refresh: encodeRefresh(c), access: c.accessToken, expires: c.expiresAt }
}

export function fromStored(s: StoredOAuth): KiroCredentials {
  return { ...decodeRefresh(s.refresh), accessToken: s.access, expiresAt: s.expires }
}

/**
 * Region to send API calls to. Kiro's API is only served from a few regions, and a
 * profile ARN (arn:aws:codewhisperer:<region>:…) pins the one this account lives in.
 */
export function apiRegion(c: Pick<KiroCredentials, "region" | "profileArn">): string {
  const fromArn = c.profileArn?.split(":")[3]
  return fromArn ? normalizeRegion(fromArn) : c.region
}

export function isExpired(c: KiroCredentials, now = Date.now()): boolean {
  return !c.accessToken || c.expiresAt - EXPIRY_SKEW_MS <= now
}
