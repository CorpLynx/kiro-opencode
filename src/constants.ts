export const PROVIDER_ID = "kiro"
export const PROVIDER_NAME = "Kiro"

export const DEFAULT_REGION = "us-east-1"
export const BUILDER_ID_START_URL = "https://view.awsapps.com/start"

/** Scopes requested for the device-authorization grant (same set the Kiro IDE asks for). */
export const OIDC_SCOPES = [
  "codewhisperer:completions",
  "codewhisperer:analysis",
  "codewhisperer:conversations",
  "codewhisperer:transformations",
  "codewhisperer:taskassist",
]

export const KIRO_VERSION = "0.8.0"
export const CLIENT_NAME = "Kiro IDE"

/** Refresh a token this long before it actually expires. */
export const EXPIRY_SKEW_MS = 2 * 60 * 1000

const override = (name: string) => process.env[name]?.replace(/\/+$/, "") || undefined

/**
 * Endpoint overrides, for corporate proxies and for local mock servers in tests:
 * `KIRO_API_ENDPOINT` (chat + usage API) and `KIRO_AUTH_ENDPOINT` (OIDC + desktop token refresh).
 */
export const oidcEndpoint = (region: string) => override("KIRO_AUTH_ENDPOINT") ?? `https://oidc.${region}.amazonaws.com`
export const socialRefreshEndpoint = (region: string) =>
  `${override("KIRO_AUTH_ENDPOINT") ?? `https://prod.${region}.auth.desktop.kiro.dev`}/refreshToken`
export const apiEndpoint = (region: string) => override("KIRO_API_ENDPOINT") ?? `https://q.${region}.amazonaws.com`

/** The "baseURL" handed to the AI SDK. Never contacted directly: our fetch intercepts everything. */
export const VIRTUAL_BASE_URL = "https://kiro.invalid/v1"

const REGION_RE = /^[a-z]{2}(?:-[a-z]+)+-\d+$/
export function normalizeRegion(region: string | undefined | null): string {
  const r = region?.trim().toLowerCase()
  return r && REGION_RE.test(r) ? r : DEFAULT_REGION
}
