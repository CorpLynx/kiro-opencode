# opencode-ghe-policy

An [opencode](https://opencode.ai) plugin that makes a **GitHub Enterprise repository the control plane**
for every opencode installation in your organization:

* **Central policy.** One JSON file in a GHE repo sets the opencode settings people can't override
  (providers, models, permissions, sharing, MCP servers), plus rules the agent must follow. Changing policy is
  a reviewed pull request, not a new build.
* **Command guardrails.** Before the agent runs a shell command or fetches a URL, the plugin blocks:
  * pushes to protected branches, and force pushes;
  * network access to hosts outside your allowlist;
  * anything matching your own deny rules.

  The agent is told why, and can adjust.
* **Commit provenance.** Every commit the agent makes carries `Assisted-by: opencode (<provider>/<model>)` and
  `Opencode-Session: <id>` trailers.
* **One identity.** Policy is read with the same GHE identity people already use (`gh auth login`, or a token).

Works with GitHub Enterprise Server (`github.agency.gov`), GitHub Enterprise Cloud with data residency
(`agency.ghe.com`) and github.com. Tested against opencode 1.19.

## Install

```jsonc
// opencode.json (global: ~/.config/opencode/opencode.json, or pushed by device management)
{
  "plugin": [
    ["opencode-ghe-policy", {
      "host": "github.agency.gov",
      "repo": "platform/opencode-policy",
      "path": "opencode-policy.json",   // default
      "ref": "main"                     // default; a tag or commit sha pins a version
    }]
  ]
}
```

The policy is read with, in order: `OPENCODE_POLICY_TOKEN`, `GH_ENTERPRISE_TOKEN`, or the GitHub CLI's login for
that host (`gh auth login --hostname github.agency.gov`). The token needs read access to the policy repo.

Every option can also come from the environment (`OPENCODE_POLICY_HOST`, `_REPO`, `_PATH`, `_REF`, `_FILE`,
`_FAIL_MODE`), which suits device management.

| Option | Default | |
| --- | --- | --- |
| `host`, `repo`, `path`, `ref` | – , – , `opencode-policy.json`, `main` | Where the policy lives in GHE. |
| `file` | – | Read the policy from a local file instead (air-gapped or MDM-managed machines). |
| `failMode` | `closed` | No policy available: `closed` locks down (below), `open` runs without one. |
| `maxStaleDays` | `7` | How long a cached copy may be used while GHE is unreachable. |

## The policy file

```json
{
  "version": 1,
  "name": "agency-baseline",
  "opencode": {
    "share": "disabled",
    "autoupdate": false,
    "enabled_providers": ["github-copilot"],
    "provider": { "github-copilot": { "whitelist": ["claude-sonnet-5", "gpt-5.6-terra"] } },
    "permission": { "webfetch": "ask", "bash": { "*": "ask", "git status*": "allow", "npm test*": "allow" } }
  },
  "mcp": { "allow": ["github"] },
  "instructions": [
    "This system may process CUI. Never paste credentials, tokens or personal data into prompts.",
    "All changes go through pull requests."
  ],
  "guard": {
    "protectedBranches": ["main", "release/*"],
    "blockForcePush": true,
    "allowedHosts": ["*.agency.gov", "registry.npmjs.org"],
    "deny": [{ "pattern": "\\bprintenv\\b|\\benv\\s*$", "reason": "dumping the environment is not allowed" }]
  },
  "provenance": { "enabled": true, "trailer": "Assisted-by" }
}
```

* **`opencode`**: any `opencode.json` settings. Each key set here **overrides** the user's and the project's
  value. Objects merge; lists and plain values replace. Keys the policy doesn't set stay under the user's control.
  `plugin` can't be set this way, because plugins load before the policy does.
* **`mcp.allow`**: MCP servers not named here are removed. Servers the policy itself defines under `opencode.mcp`
  are always kept.
* **`instructions`**: added to every session's system prompt.
* **`guard`**:
  * **`protectedBranches`** (globs: `*` within a segment, `**` across): blocks direct pushes there, including a bare
    `git push` while on such a branch, and `--all` / `--mirror`.
  * **`blockForcePush`**: blocks `-f`, `--force`, `--force-with-lease` and `+ref` pushes.
  * **`allowedHosts`**: limits where `curl`, `wget`, `ssh`, `scp`, `rsync`, `nc`, git remotes and the web-fetch tool
    may connect. Loopback is always allowed. `*.example.gov` matches subdomains; list the apex separately if needed.
  * **`deny`**: regular expressions matched against the whole command, each with the reason shown to the agent.
* **`provenance`**: commit trailers (needs git 2.32+). `"session": false` drops the session trailer.

Commands are parsed the way a shell would see them: through `&&` / `;` / `|` chains, `$(…)` and backticks,
`bash -c` / `eval`, env-var prefixes and wrappers like `sudo` and `env`. Rewording a command doesn't get it past a rule.

Ask the agent "what policy applies?" (the `policy_status` tool) to see the policy in force and where it came from.

## When the policy can't be loaded

1. GHE unreachable or the file invalid: the last good copy, cached for up to `maxStaleDays`, is used, with a warning.
   An invalid policy never replaces a good cached one.
2. Nothing usable and `failMode: "closed"` (the default): **lockdown**:
   * no sharing or auto-update;
   * every edit and shell command needs approval, and web fetches are denied;
   * no MCP servers;
   * `main` / `master` protected, and force pushes blocked.

   A warning toast explains why.
3. `failMode: "open"`: opencode runs with the local configuration only.

## What a plugin can't do

The plugin governs opencode from inside, so it can't stop someone running opencode without it, or removing it
from their own config. Pair it with:

* **Distribution:** ship the global `opencode.json` that loads it from device management, so it's present for everyone.
* **The network:** an egress proxy or firewall. The command guard catches the agent's attempts; the network
  enforces them for everything.
* **GitHub itself:** branch protection and rulesets on GHE. The guard keeps the agent from trying; GHE makes it
  impossible.
* **A fork, for the rest:** some opencode behaviour (fetching the public model catalogue at startup, the
  update check) can only be removed in the source.

## Development

```sh
npm install
npm run typecheck && npm test && npm run build
OPENCODE_BIN=$(command -v opencode) npm run test:e2e   # real opencode vs. a mock GHE and a mock model
```

## License

MIT
