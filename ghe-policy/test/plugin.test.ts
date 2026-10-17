import { mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { applyPolicy, sourceOptions } from "../src/apply.js"
import { GhePolicyPlugin } from "../src/index.js"
import server from "../src/server.js"

const tmp = () => mkdtempSync(join(tmpdir(), "ghe-plugin-"))
const client = () => ({ app: { log: vi.fn(async () => ({})) }, tui: { showToast: vi.fn(async () => ({})) } })
const policy = {
  version: 1,
  name: "agency-baseline",
  opencode: { share: "disabled", autoupdate: false, permission: { webfetch: "ask" } },
  mcp: { allow: ["github"] },
  instructions: ["Never paste credentials into prompts."],
  guard: { protectedBranches: ["main"], blockForcePush: true, allowedHosts: ["*.agency.gov"] },
  provenance: { enabled: true },
}

beforeEach(() => vi.stubEnv("XDG_STATE_HOME", tmp()))
afterEach(() => vi.unstubAllEnvs())

async function plugin(p: unknown = policy, extra: Record<string, unknown> = {}) {
  const file = join(tmp(), "policy.json")
  writeFileSync(file, JSON.stringify(p))
  const c = client()
  const hooks = await GhePolicyPlugin({ client: c, directory: tmp() } as any, { file, ...extra })
  return { hooks, c }
}

describe("sourceOptions", () => {
  it("reads plugin options, then OPENCODE_POLICY_* variables, failing closed by default", () => {
    expect(sourceOptions({ host: "h", repo: "o/r" }, {})).toMatchObject({ host: "h", repo: "o/r", failMode: "closed" })
    expect(sourceOptions({}, { OPENCODE_POLICY_HOST: "eh", OPENCODE_POLICY_REPO: "x/y", OPENCODE_POLICY_FAIL_MODE: "open" })).toMatchObject({
      host: "eh",
      repo: "x/y",
      failMode: "open",
    })
  })
})

describe("applyPolicy", () => {
  it("enforces settings, drops unapproved MCP servers and adds the instructions", () => {
    const dir = tmp()
    const config: Record<string, any> = {
      share: "auto",
      model: "kiro/claude-sonnet-5",
      mcp: { github: { type: "local", command: ["gh-mcp"] }, rogue: { type: "remote", url: "https://evil.example" } },
      instructions: ["AGENTS.md"],
    }
    applyPolicy(config, policy as any, dir)
    expect(config.share).toBe("disabled")
    expect(config.autoupdate).toBe(false)
    expect(config.model).toBe("kiro/claude-sonnet-5")
    expect(Object.keys(config.mcp)).toEqual(["github"])
    expect(config.instructions[0]).toBe("AGENTS.md")
    expect(readFileSync(config.instructions[1], "utf8")).toContain("Never paste credentials into prompts.")
  })

  it("keeps MCP servers the policy itself defines", () => {
    const config: Record<string, any> = {}
    applyPolicy(config, { version: 1, opencode: { mcp: { org: { type: "remote", url: "https://mcp.agency.gov" } } }, mcp: { allow: [] } }, tmp())
    expect(Object.keys(config.mcp)).toEqual(["org"])
  })
})

describe("GhePolicyPlugin", () => {
  it("applies the policy through the config hook", async () => {
    const { hooks } = await plugin()
    const config: any = { share: "auto" }
    await hooks.config!(config)
    expect(config.share).toBe("disabled")
  })

  it("blocks commands the guard rejects, with the policy's name", async () => {
    const { hooks, c } = await plugin()
    const run = (command: string) => hooks["tool.execute.before"]!({ tool: "bash", sessionID: "ses_1", callID: "c" }, { args: { command } })
    await expect(run("git push origin main")).rejects.toThrow("Blocked by agency-baseline: pushing to protected branch 'main'")
    await expect(run("curl https://example.com")).rejects.toThrow(/'example.com' is not allowed/)
    await expect(run("ls")).resolves.toBeUndefined()
    expect(c.app.log).toHaveBeenCalledWith({ body: expect.objectContaining({ level: "warn", message: "blocked command" }) })
  })

  it("blocks web fetches to unapproved hosts", async () => {
    const { hooks } = await plugin()
    const fetchUrl = (url: string) => hooks["tool.execute.before"]!({ tool: "webfetch", sessionID: "s", callID: "c" }, { args: { url } })
    await expect(fetchUrl("https://evil.example/x")).rejects.toThrow(/'evil.example'/)
    await expect(fetchUrl("https://docs.agency.gov/x")).resolves.toBeUndefined()
  })

  it("stamps commits with the session's model", async () => {
    const { hooks } = await plugin()
    await hooks["chat.params"]!(
      { sessionID: "ses_9", agent: "build", model: { id: "claude-sonnet-5" }, provider: { info: { id: "github-copilot" } } } as any,
      { temperature: 0, topP: 1, topK: 0, maxOutputTokens: undefined, options: {} },
    )
    const output = { args: { command: "git commit -m 'add feature'" } }
    await hooks["tool.execute.before"]!({ tool: "bash", sessionID: "ses_9", callID: "c" }, output)
    expect(output.args.command).toBe(
      "git commit --trailer 'Assisted-by: opencode (github-copilot/claude-sonnet-5)' --trailer 'Opencode-Session: ses_9' -m 'add feature'",
    )
  })

  it("warns and locks down when the policy can't be loaded", async () => {
    const c = client()
    const hooks = await GhePolicyPlugin({ client: c, directory: tmp() } as any, { file: "/nonexistent/policy.json" })
    expect(c.tui.showToast).toHaveBeenCalledWith({ body: expect.objectContaining({ variant: "warning", message: expect.stringMatching(/locked down/) }) })
    const config: any = { share: "auto", mcp: { anything: { type: "local", command: ["x"] } } }
    await hooks.config!(config)
    expect(config).toMatchObject({ share: "disabled", mcp: {}, permission: { bash: "ask", webfetch: "deny" } })
  })

  it("describes the policy in force", async () => {
    const { hooks } = await plugin()
    const out = (await hooks.tool!.policy_status!.execute({}, {} as any)) as { output: string }
    expect(out.output).toContain("Policy: agency-baseline")
    expect(out.output).toContain("Protected branches: main")
    expect(out.output).toContain("Network allowed to: *.agency.gov")
  })

  it("ships a server entry in opencode's module format", () => {
    expect(server).toMatchObject({ id: "opencode-ghe-policy", server: GhePolicyPlugin })
  })
})
