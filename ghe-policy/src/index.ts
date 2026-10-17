import { join } from "node:path"
import type { Hooks, Plugin, PluginInput, PluginOptions, ToolDefinition } from "@opencode-ai/plugin"
import { applyPolicy, describe, git, sourceOptions } from "./apply.js"
import { checkCommand, checkUrl, type GuardContext } from "./guard.js"
import { addTrailers, supportsTrailers, trailersFor } from "./provenance.js"
import { loadPolicy, stateDir } from "./source.js"

// Only the plugin is exported here: opencode calls every exported function of a plugin module.

const SERVICE = "ghe-policy"

export const GhePolicyPlugin: Plugin = async (input: PluginInput, options?: PluginOptions) => {
  const { client, directory } = input
  const log = (level: "info" | "warn" | "error", message: string, extra?: Record<string, unknown>) =>
    client.app.log({ body: { service: SERVICE, level, message, extra } }).catch(() => {})

  const dir = stateDir()
  const loaded = await loadPolicy(sourceOptions(options))
  const policy = loaded.policy
  const label = policy.name ?? "organization policy"
  log(loaded.warning ? "warn" : "info", `policy ${loaded.origin}: ${loaded.detail}`, { sha: loaded.sha, warning: loaded.warning })
  if (loaded.warning) {
    client.tui?.showToast?.({ body: { title: "Organization policy", message: loaded.warning, variant: "warning" } })?.catch?.(() => {})
  }

  const trailers = supportsTrailers(git(["--version"], directory))
  if (policy.provenance?.enabled && !trailers) log("warn", "git is older than 2.32, so commit provenance trailers are skipped")

  const guardCtx: GuardContext = {
    currentBranch: (gitDir) => git(["rev-parse", "--abbrev-ref", "HEAD"], gitDir ? join(directory, gitDir) : directory),
    remoteUrl: (name, gitDir) => git(["remote", "get-url", name], gitDir ? join(directory, gitDir) : directory),
  }
  /** sessionID → "provider/model", for provenance trailers. */
  const models = new Map<string, string>()

  const statusTool: ToolDefinition = {
    description: "Show the organization policy in force for this opencode installation: where it came from and what it enforces.",
    args: {},
    async execute() {
      return { title: "Organization policy", output: describe(loaded) }
    },
  }

  const hooks: Hooks = {
    tool: { policy_status: statusTool },

    async config(config) {
      applyPolicy(config as Record<string, any>, policy, dir)
    },

    async "chat.params"(input) {
      const provider = input.provider?.info?.id ?? (input.model as { providerID?: string }).providerID
      if (input.model?.id) models.set(input.sessionID, provider ? `${provider}/${input.model.id}` : input.model.id)
    },

    async "tool.execute.before"(input, output) {
      const args = output.args as Record<string, unknown>
      if (input.tool === "bash" && typeof args?.command === "string") {
        const verdict = checkCommand(args.command, policy.guard, guardCtx)
        if (!verdict.allowed) {
          log("warn", "blocked command", { reason: verdict.reason, sessionID: input.sessionID })
          throw new Error(`Blocked by ${label}: ${verdict.reason}`)
        }
        if (trailers) {
          args.command = addTrailers(
            args.command,
            trailersFor(policy.provenance, { model: models.get(input.sessionID), sessionID: input.sessionID }),
          )
        }
      } else if (input.tool === "webfetch" && typeof args?.url === "string") {
        const verdict = checkUrl(args.url, policy.guard)
        if (!verdict.allowed) {
          log("warn", "blocked fetch", { reason: verdict.reason, sessionID: input.sessionID })
          throw new Error(`Blocked by ${label}: ${verdict.reason}`)
        }
      }
    },
  }
  return hooks
}
