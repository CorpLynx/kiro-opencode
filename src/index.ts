import type { AuthHook, Hooks, Plugin, PluginInput, PluginOptions, ToolDefinition } from "@opencode-ai/plugin"
import { toStored, type KiroCredentials } from "./auth/credentials.js"
import { importKiroCredentials } from "./auth/import.js"
import { TokenManager } from "./auth/manager.js"
import { pollDeviceToken, refreshCredentials, startDeviceAuthorization } from "./auth/oidc.js"
import {
  BUILDER_ID_START_URL,
  DEFAULT_REGION,
  PROVIDER_ID,
  PROVIDER_NAME,
  SESSION_HEADER,
  VIRTUAL_BASE_URL,
  normalizeRegion,
} from "./constants.js"
import { createKiroFetch } from "./fetch.js"
import { DEFAULT_MODELS, contextWindowFor } from "./models.js"
import { UsageRecorder } from "./tracker/recorder.js"
import { fetchUsage, formatUsage } from "./usage.js"

type AuthMethod = AuthHook["methods"][number]
type OAuthMethod = Extract<AuthMethod, { type: "oauth" }>

const IMPORT_DOCS_URL = "https://kiro.dev/docs/cli/authentication/"

function success(creds: KiroCredentials) {
  return { type: "success" as const, ...toStored(creds) }
}

export const KiroPlugin: Plugin = async (input: PluginInput, options?: PluginOptions) => {
  const { client } = input
  const defaultRegion = normalizeRegion(typeof options?.region === "string" ? options.region : DEFAULT_REGION)
  const profileArn =
    (typeof options?.profileArn === "string" && options.profileArn) || process.env.KIRO_PROFILE_ARN || undefined
  // Feeds the TUI usage tracker; `"tracker": false` in the plugin options turns it off.
  const trackUsage = options?.tracker !== false

  const log = (level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>) => {
    client.app.log({ body: { service: PROVIDER_ID, level, message, extra } }).catch(() => {})
  }

  async function deviceLogin(opts: { startUrl?: string; region?: string; profileArn?: string }) {
    const auth = await startDeviceAuthorization({ startUrl: opts.startUrl, region: opts.region ?? defaultRegion })
    return {
      url: auth.verificationUriComplete,
      instructions: `Approve the sign-in in your browser. Confirmation code: ${auth.userCode}`,
      method: "auto" as const,
      async callback() {
        try {
          const creds = await pollDeviceToken(auth)
          if (opts.profileArn?.trim()) creds.profileArn = opts.profileArn.trim()
          return success(creds)
        } catch (err) {
          log("error", "Kiro login failed", { error: String(err) })
          return { type: "failed" as const }
        }
      },
    }
  }

  /** Set once opencode has loaded the provider with Kiro credentials. */
  let activeTokens: TokenManager | undefined
  let recorder: UsageRecorder | undefined

  // Declared without the `tool()` helper so the plugin has no runtime dependency on zod.
  const usageTool: ToolDefinition = {
    description:
      "Show the signed-in Kiro account's credit usage and remaining quota. Use when the user asks about Kiro usage, credits or limits.",
    args: {},
    async execute(_args, ctx) {
      const title = "Kiro usage"
      if (!activeTokens) return { title, output: "Kiro is not signed in. Run `opencode auth login` and choose Kiro." }
      try {
        const usage = await fetchUsage(await activeTokens.get(), { signal: ctx.abort })
        recorder?.saveAccount(usage)
        return { title, output: formatUsage(usage) }
      } catch (err) {
        return { title, output: `Could not fetch Kiro usage: ${err instanceof Error ? err.message : String(err)}` }
      }
    },
  }

  const methods: AuthMethod[] = [
    {
      type: "oauth",
      label: "AWS Builder ID (browser device login)",
      authorize: () => deviceLogin({ startUrl: BUILDER_ID_START_URL }),
    } satisfies OAuthMethod,
    {
      type: "oauth",
      label: "AWS IAM Identity Center (organization login)",
      prompts: [
        {
          type: "text",
          key: "startUrl",
          message: "IAM Identity Center start URL",
          placeholder: "https://my-org.awsapps.com/start",
          validate: (v) => (/^https:\/\/\S+$/.test(v.trim()) ? undefined : "Enter an https:// URL"),
        },
        { type: "text", key: "region", message: "Identity Center region", placeholder: DEFAULT_REGION },
        {
          type: "text",
          key: "profileArn",
          message: "Kiro profile ARN (optional, from `kiro-cli profile`)",
          placeholder: "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL",
        },
      ],
      authorize: (inputs = {}) =>
        deviceLogin({
          startUrl: inputs.startUrl,
          region: inputs.region?.trim() || undefined,
          profileArn: inputs.profileArn,
        }),
    } satisfies OAuthMethod,
    {
      type: "oauth",
      label: "Import existing login (Kiro IDE or kiro-cli)",
      async authorize() {
        return {
          url: IMPORT_DOCS_URL,
          instructions: "Reading the session created by `kiro-cli login` / the Kiro IDE… (no browser step needed)",
          method: "auto" as const,
          async callback() {
            try {
              const found = await importKiroCredentials()
              if (!found) {
                log("error", "No Kiro IDE or kiro-cli session found. Run `kiro-cli login` first.")
                return { type: "failed" as const }
              }
              // Validate (and normalise) the session right away; expired access tokens get refreshed.
              const creds = found.expiresAt > Date.now() + 60_000 ? found : await refreshCredentials(found)
              if (!creds.profileArn && profileArn) creds.profileArn = profileArn
              return success(creds)
            } catch (err) {
              log("error", "Importing Kiro login failed", { error: String(err) })
              return { type: "failed" as const }
            }
          },
        }
      },
    } satisfies OAuthMethod,
  ]

  const hooks: Hooks = {
    tool: { kiro_usage: usageTool },

    /** Tag Kiro requests with their session so per-session usage can be tracked. */
    async "chat.headers"(input, output) {
      const providerID = input.provider?.info?.id ?? (input.model as { providerID?: string } | undefined)?.providerID
      if (trackUsage && providerID === PROVIDER_ID) output.headers[SESSION_HEADER] = input.sessionID
    },

    /**
     * Declare the provider so it shows up without a models.dev entry. User config wins:
     * anything set under `provider.kiro` in opencode.json is layered on top of our defaults.
     */
    async config(config) {
      const existing = config.provider?.[PROVIDER_ID]
      const models: Record<string, any> = { ...DEFAULT_MODELS }
      for (const [id, override] of Object.entries(existing?.models ?? {})) {
        models[id] = { ...models[id], ...(override as object) }
      }
      config.provider = {
        ...config.provider,
        [PROVIDER_ID]: {
          name: PROVIDER_NAME,
          npm: "@ai-sdk/openai-compatible",
          ...existing,
          options: { baseURL: VIRTUAL_BASE_URL, ...existing?.options },
          models,
        },
      }
    },

    auth: {
      provider: PROVIDER_ID,
      methods,
      async loader(getAuth, provider) {
        const auth = await getAuth()
        if (auth.type !== "oauth") return {}

        // Usage is billed in Kiro credits, so don't show per-token dollar costs.
        for (const model of Object.values(provider.models ?? {})) {
          model.cost = { input: 0, output: 0, cache: { read: 0, write: 0 } }
        }

        const tokens = (activeTokens = new TokenManager({
          load: async () => (await getAuth()) as any,
          save: async (stored) => {
            await client.auth.set({ path: { id: PROVIDER_ID }, body: { type: "oauth", ...stored } })
          },
          profileArn,
        }))
        if (trackUsage) {
          recorder = new UsageRecorder({ fetchAccount: async () => fetchUsage(await tokens.get()), log })
          void recorder.refreshAccount()
        }
        return {
          apiKey: "kiro-oauth", // placeholder: the AI SDK insists on one; our fetch supplies the real bearer token
          baseURL: VIRTUAL_BASE_URL,
          fetch: createKiroFetch({
            tokens,
            log,
            contextWindow: (id) => contextWindowFor(id, provider.models as any),
            onUsage: recorder && ((u) => recorder!.turn(u)),
          }),
        }
      },
    },
  }
  return hooks
}
