# opencode-kiro-provider

An [opencode](https://opencode.ai) plugin that adds **Kiro** (the AWS CodeWhisperer / Amazon Q Developer
backend behind the Kiro IDE and `kiro-cli`) as a model provider. Sign in once and use Kiro's models
(Claude Sonnet / Opus / Haiku, DeepSeek, GLM, MiniMax, Qwen, and `auto`) from opencode, billed against
your Kiro credits.

> **Unofficial.** Not affiliated with or endorsed by AWS or Kiro. It talks to the same private API the
> Kiro IDE uses, which can change without notice. Check that your Kiro plan permits this kind of use.

## Install

Add the plugin to `opencode.json` (project) or `~/.config/opencode/opencode.json` (global):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["opencode-kiro-provider"]
}
```

For a local checkout: `npm install && npm run build`, then use `"plugin": ["file:///path/to/kiro-opencode/dist/index.js"]`.

## Sign in

```sh
opencode auth login      # choose "Kiro"
```

| Method | Use when |
| --- | --- |
| **AWS Builder ID** | Personal / free Kiro account. Opens a browser device-login page. |
| **AWS IAM Identity Center** | Organization login. Asks for your start URL, region and (optionally) a profile ARN. |
| **Import existing login** | You already ran `kiro-cli login` or signed in to the Kiro IDE. Nothing to approve. |

Then pick a model with `/models` (e.g. `kiro/claude-sonnet-4.5`). Credentials live in opencode's normal
`auth.json`; access tokens are refreshed automatically and the rotated refresh token is written back.

### Credits

The plugin adds a `kiro_usage` tool, so you can ask *"how many Kiro credits do I have left?"*. It reports
used and remaining credits (and free-trial credits) for the signed-in account.

### Thinking

For Claude models, opencode's reasoning variants (`ctrl+t`, or `--variant high`) switch on Kiro's thinking
mode. Thinking output is shown as reasoning.

## Configuration

Everything works without configuration. Optional knobs:

```json
{
  "plugin": [["opencode-kiro-provider", { "region": "us-east-1", "profileArn": "arn:aws:codewhisperer:…" }]],
  "provider": {
    "kiro": {
      "models": {
        "claude-sonnet-4.5": { "limit": { "context": 200000, "output": 32000 } },
        "some-new-model-id": { "name": "Some New Model", "tool_call": true, "limit": { "context": 128000, "output": 16000 } }
      }
    }
  }
}
```

* `provider.kiro.models` is merged over the built-in catalogue, so you can add models Kiro releases
  later (the id must be Kiro's own model id) or adjust limits.
* `profileArn` / `KIRO_PROFILE_ARN`: required by some IAM Identity Center setups. The *Import* method picks it
  up from `kiro-cli` automatically.
* `KIRO_API_ENDPOINT` / `KIRO_AUTH_ENDPOINT`: send API calls, or OIDC and token-refresh calls, somewhere
  else (corporate proxy, local mock server).

## How it works

```
opencode ──(@ai-sdk/openai-compatible, OpenAI chat format)──▶ plugin fetch ──▶ Kiro generateAssistantResponse
        ◀──────────── OpenAI SSE chunks ◀── AWS event-stream decoder ◀──────────────┘
```

* `config` hook declares the `kiro` provider and its models (it has no models.dev entry).
* `tool` hook adds `kiro_usage` (calls Kiro's `getUsageLimits`).
* `auth` hook provides the login methods and a `loader` that returns a custom `fetch`. That fetch converts
  OpenAI requests to Kiro's `conversationState` (history pairing, tool schemas, images, system prompt),
  authenticates and refreshes the token, retries 429/5xx with backoff, and converts the binary event stream
  back to OpenAI streaming chunks (text, reasoning, incremental tool calls, usage).
* Kiro reports usage as a context percentage plus credits, not tokens, so reported token counts are estimates.

## Limitations

* Built against the protocol the Kiro IDE uses, but not yet exercised against live AWS. It is covered by
  unit tests, and by end-to-end runs of the real opencode binary (1.19) against a mock Kiro server. Those
  runs cover chat, tool calls, thinking variants, device login, token refresh and persistence, the usage
  tool, and error handling. Please report breakage with the (redacted) error message.
* Remote image URLs can't be forwarded (Kiro only accepts inline image bytes).
* Sampling parameters (temperature, top-p, max tokens) are not supported by Kiro and are ignored.
* Google/GitHub "social" logins can't be started from opencode; sign in with the Kiro IDE or `kiro-cli`
  and use *Import existing login*.

## Development

```sh
npm install
npm run typecheck && npm test && npm run build

# End-to-end: drives a real opencode binary against a local mock of Kiro (no AWS calls).
OPENCODE_BIN=$(command -v opencode) npm run test:e2e
```

The e2e suite (`e2e/run.sh` + `e2e/mock-kiro.ts`) covers model listing, streaming chat, a tool-call round
trip, the `kiro_usage` tool, thinking variants, token refresh and persistence, mid-stream errors, context-overflow
compaction, and the device login through opencode's HTTP API.

Layout:

| Path | Purpose |
| --- | --- |
| `src/index.ts` | Plugin entry: `config`, `auth` and `tool` hooks |
| `src/auth/` | Credential encoding, OIDC device flow + refresh, Kiro IDE / kiro-cli import, token manager |
| `src/transform/` | OpenAI ⇄ Kiro request/response translation and the AWS event-stream decoder |
| `src/fetch.ts` | The custom `fetch`: auth, retries, error mapping |
| `src/models.ts`, `src/usage.ts` | Model catalogue; credit usage lookup |

Prior art studied for the design: [`opencode-kiro`](https://github.com/NachoFLizaur/opencode-kiro) (MIT, wraps
`kiro-cli` over ACP) and `opencode-kiro-auth` (GPL-3.0, direct API). This project is an independent MIT
implementation and does not copy code from the GPL project.

## License

MIT
