# Hardened opencode

A build of [opencode](https://github.com/anomalyco/opencode) for organizations that need the binary itself to make
**no network calls the organization didn't choose**. The CLI, TUI, providers and plugins work as upstream; what
changes is everything opencode would otherwise contact by itself: the public model catalogue, the update check,
LSP and skill downloads, web search services, the hosted web UI, the npm registry, and opencode.ai's account,
GitHub app and model services.

The fork is kept as **a patch queue on a pinned upstream release** rather than a long-lived branch: eight small,
reviewable patches (150 lines added) in `patches/`, applied by `build.sh` to the exact commit in `UPSTREAM`. Moving to
a new release means re-applying eight patches, not merging months of upstream history.

## Build

```sh
./build.sh                                             # catalogue downloaded from models.dev, curated

# offline build host: a reviewed catalogue copy, and the internal npm registry
./build.sh --models reviewed/api.json \
           --approved snapshot/approved-providers.json \
           --npm-registry https://artifactory.agency.gov/api/npm/npm-remote
```

| Option | |
| --- | --- |
| `--models` | The models.dev catalogue (`api.json`) to embed. Default: download it. |
| `--approved` | Providers to keep (`snapshot/approved-providers.json`). The rest aren't in the binary at all. |
| `--npm-registry` | The internal registry plugins and provider SDKs are installed from. Without it, npm runs offline: only packages already in the cache, or `file://` plugins, load. |
| `--models-url` | An internal mirror of the model catalogue to refresh from. Without it, the embedded catalogue is the only one. |

`build.sh` refuses to build if the upstream tag no longer points at the pinned commit, and writes the binaries,
the embedded catalogue and `SHA256SUMS` to `dist/`.

All settings are compiled in (the `OPENCODE_HARDENED` build define). None can be changed at run time.

## What the patches change

| # | Patch | Upstream behaviour | Hardened |
| --- | --- | --- | --- |
| 1 | build-time switch | – | `Hardened` settings compiled in; without the define the source builds stock opencode. |
| 2 | model catalogue | Fetches `models.opencode.ai/api.json` at startup and hourly; `OPENCODE_MODELS_URL` points it anywhere. | Embedded, curated catalogue only, or the build's `--models-url` mirror. The env vars are ignored. |
| 3 | updates | Checks for, downloads and installs new versions; `opencode upgrade`. | No update checks. `opencode upgrade` says updates come from your organization. |
| 4 | downloads & services | Downloads LSP servers from GitHub; fetches skills from URLs; imports Claude Code prompts/skills; Exa and Parallel web search; `share`. | All off and pinned off: `OPENCODE_ENABLE_EXA` and the rest can't turn them back on. |
| 5 | web UI | `opencode serve` proxies the web UI from `app.opencode.ai` when the binary has none embedded. | Serves a local "not available" page instead. |
| 6 | npm | Installs plugins and provider SDKs from the user's npm registry (`.npmrc`). | Only from the build's registry, whatever `.npmrc` says; offline without one. |
| 7 | managed policy | `/etc/opencode/opencode.json` (or the macOS/Windows equivalents) can be redirected with `OPENCODE_TEST_MANAGED_CONFIG_DIR`, loosened with `OPENCODE_PERMISSION`, or its plugins skipped with `--pure`. | None of the three works: managed configuration always applies. |
| 8 | opencode.ai services | `opencode console login` and the organization config it pulls; `opencode github install`; the "Zen" model service. | Commands hidden and refused; no remote org config; the `opencode` provider disabled. |

## Verification

`test/egress.sh` drives a binary through `--version`, `models`, a chat, a tool call, `upgrade`, `console login`,
`github install`, `serve` with a web UI request, and the TUI, in an environment set up to switch network features
back on (`OPENCODE_ENABLE_EXA=1`, `OPENCODE_MODELS_URL=…`, an npm plugin in the project config and so on). It
records every attempt twice over: a proxy logs the host each proxy-aware client asks for (and refuses it), and
`strace` logs every `connect()` syscall, which catches clients that ignore the proxy and DNS lookups.

```
$ test/egress.sh dist/opencode-linux-x64 --expect-none        # hardened
functional: chat=1 tool=1 tui=2 web-ui=HTTP 404
destinations attempted (destination  count  during):
  (none)
PASS: no outbound connection attempts

$ test/egress.sh /usr/local/bin/opencode                      # stock 1.19.3
functional: chat=1 tool=1 tui=2 web-ui=HTTP 404
destinations attempted (destination  count  during):
  models.example.invalid:443 (via proxy)         5  chat, console, models, tool, tui
  opencode.ai:443 (via proxy)                    1  console
  registry.npmjs.org:443 (via proxy)            18  chat, github, models, tool, tui
```

Without the hostile environment, stock opencode contacts `models.opencode.ai` on every start. Other paths (LSP
downloads, web search, the update check on a package-manager install) stay latent until a user, a project config or
an install method triggers them, which is why the patches remove them rather than rely on them being idle.

`test/managed.sh` (root: it writes `/etc/opencode`, and restores it afterwards) checks that managed configuration
beats project config, `OPENCODE_PERMISSION`, `OPENCODE_TEST_MANAGED_CONFIG_DIR`, `--pure` and `OPENCODE_PURE`, and
that Zen is disabled. The hardened build passes all six; stock 1.19.3 passes only the first.

The hardened build has no browser interface (`--skip-embed-web-ui`, so nothing is fetched to build it). If you want
one, build upstream's `packages/app` on a build host with access to its dependencies and drop the flag.

## Deploying it

1. **Managed configuration.** Ship `/etc/opencode/opencode.json` (macOS: `/Library/Application Support/opencode`,
   Windows: `%ProgramData%\opencode`) from device management. That's where to load
   [`opencode-ghe-policy`](../ghe-policy) for central policy, guardrails and provenance, and to set
   `enabled_providers`. In this build users can't redirect, loosen or skip it.
2. **Distribution.** Publish the binaries through your software catalogue, signed with your code-signing key, with
   `SHA256SUMS` and an SBOM (`syft dist/` or the like). Updates are new builds you ship; the binary never updates itself.
3. **Internal registry.** Mirror the plugin and provider SDK packages you approve into the registry passed to `--npm-registry`.

## What else to secure

The patches cover what the binary does by itself. These need decisions or infrastructure beyond the source:

* **Egress.** Run opencode behind a forward proxy that allows only your model endpoints, GHE and the internal
  registry. The patches stop opencode from calling out; the proxy also covers what the agent runs (`curl`, `npm`,
  MCP servers).
* **Credentials at rest.** opencode keeps provider tokens and OAuth refresh tokens in plain JSON
  (`~/.local/share/opencode/auth.json`, mode 600). Prefer providers that use short-lived, SSO-derived credentials, and
  full-disk encryption. A further patch can move `auth.json` into the OS keychain (macOS Keychain, Windows DPAPI,
  libsecret).
* **Transcripts.** Sessions, prompts and tool output are stored unencrypted in `~/.local/share/opencode/opencode.db`
  (SQLite) and `~/.local/share/opencode/storage`. Decide on retention, and on whether that directory may hold CUI. The plugin `event` hook
  can forward sessions to your SIEM for audit.
* **Audit logging.** `opencode-ghe-policy` blocks and records policy violations. For a complete record, add a plugin
  that sends each tool call (`tool.execute.after`) to your log pipeline.
* **Sandboxing.** The agent's shell runs as the user. For untrusted repositories run opencode in a container or VM
  (devcontainer, Codespaces on GHE) instead of on the workstation.
* **FIPS.** The binary uses Bun's BoringSSL, which isn't a FIPS-validated module in this configuration. Where FIPS
  140-3 is required, end TLS at a FIPS-validated proxy, or run on a platform with a validated module, and get an
  assessment.
* **Supply chain.** Pin plugin versions in the managed config, review plugin updates like code, and mirror only those
  versions. `bun install --frozen-lockfile` keeps the build itself reproducible from upstream's lockfile.
* **MCP servers.** Remote MCP servers are network calls the user configures. Use `mcp.allow` in
  `opencode-ghe-policy`, or remove `mcp` from user control in the managed config.

## Updating to a new opencode release

```sh
git clone https://github.com/anomalyco/opencode && cd opencode
git checkout -b hardened vX.Y.Z
git am /path/to/opencode-fork/patches/*.patch        # resolve conflicts, `git am --continue`
bun install && (cd packages/opencode && bun run typecheck)
git format-patch --no-signature -o /path/to/opencode-fork/patches vX.Y.Z..hardened
```

Then set `UPSTREAM_TAG` and `UPSTREAM_COMMIT` in `UPSTREAM`, run both tests against the new build, and check
upstream's changelog for new network features. `test/egress.sh` will catch any that run by default. To keep a real
fork on your GHE instead, push the `hardened` branch there; the patch queue stays the reviewable record of what
differs from upstream.
