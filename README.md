# Tools and plugins

A collection of standalone tools and editor/agent plugins. Each project lives in its own folder, has its own README, and is installed and tested independently.

## Catalog

| Project | Kind | What it does |
| --- | --- | --- |
| [`tools/ip-redactor`](tools/ip-redactor) | CLI (Python) | Structure-preserving IPv4/IPv6 redaction for configs, logs, Office/Visio files and packet captures. It is keyed and reversible, and subnets stay together. |
| [`tools/topomap`](tools/topomap) | CLI (Python) | Gathers network and AWS topology automatically (AWS APIs, AWS Organizations OUs and accounts, SSH crawl over CDP/LLDP, or saved command output and config backups), stitches on-prem to AWS, and draws it as draw.io, SVG, Mermaid or DOT. |

### In review ([PR #1](https://github.com/CorpLynx/kiro-opencode/pull/1))

These projects are still at the top level on their branch. They move into the layout below once that PR is merged.

| Project | Kind | What it does |
| --- | --- | --- |
| `plugins/opencode-kiro-provider` | OpenCode plugin (TS) | Adds Kiro (AWS CodeWhisperer / Amazon Q Developer) as a model provider, with sign-in and a live usage tracker. Published to npm as `opencode-kiro-provider`. |
| `plugins/opencode-ghe-policy` | OpenCode plugin (TS) | Makes a GitHub Enterprise repository the control plane for OpenCode: central policy plus command guardrails. |
| `builds/opencode-hardened` | Patched build | An OpenCode build that makes no network calls the organization didn't choose. It is a patch queue on a pinned upstream release. |

### Planned

| Project | Kind | Notes |
| --- | --- | --- |
| `plugins/opencode-ipredact` | OpenCode plugin | Keeps real addresses away from the model: messages are redacted before they are sent, and addresses are restored when tools run and in replies. Built on `tools/ip-redactor`. |
| `tools/ipam-infoblox` | CLI | Converts an AWS IPAM export into an Infoblox import, checking ranges against the Infoblox API first. |

## Layout

```
tools/<name>/            standalone tools (CLIs, libraries)
plugins/<host>-<name>/   plugins for a host app, e.g. plugins/opencode-ipredact
builds/<name>/           patched builds of third-party software
.github/workflows/ci.yml one CI job per project
```

Each project folder contains:

- **`README.md`**: what it does, how to install it, how to use it, and its limits.
- **Its own build manifest**: `pyproject.toml` for Python, `package.json` for TypeScript and JavaScript.
- **`tests/`**: tests runnable from inside the folder with a single command, which the README states.

Projects don't import from each other through relative paths. If a plugin needs a tool, it depends on the tool's installed package or CLI. That way every project can be installed on its own.

## Keeping real data out

Fixtures and samples use made-up values only. `scripts/check_real_data.py` runs in CI and fails when a tracked file holds an AWS account ID or Organizations ID that isn't obviously fake (`python3 scripts/check_real_data.py` runs it locally). To turn a real topomap run into something shareable, use `topomap anonymize`.

## Adding a project

1. Create `tools/<name>/`, `plugins/<host>-<name>/` or `builds/<name>/` with the files above.
2. Add a row to the catalog in this README.
3. Add an entry to the `project` matrix in `.github/workflows/ci.yml`.
4. Keep dependencies optional where you can. A Python tool's core should run on the standard library, with optional extras that add heavier features.

## Running tests

```sh
cd tools/ip-redactor
pip install '.[test]'
python3 -m unittest discover -s tests
```

CI runs the same command for every project on each push and pull request.
