# Tools and plugins

A collection of standalone tools and editor/agent plugins. Each project lives in its own folder, has its own README, and is installed and tested independently.

## Catalog

| Project | Kind | What it does |
| --- | --- | --- |
| [`tools/ip-redactor`](tools/ip-redactor) | CLI (Python) | Structure-preserving IPv4/IPv6 redaction for configs, logs, Office/Visio files and packet captures. It is keyed and reversible, and subnets stay together. |

### Planned

| Project | Kind | Notes |
| --- | --- | --- |
| `plugins/opencode-ipredact` | OpenCode plugin | Keeps real addresses away from the model: messages are redacted before they are sent, and addresses are restored when tools run and in replies. Built on `tools/ip-redactor`. |
| `plugins/opencode-kiro-auth` | OpenCode plugin | Kiro authentication for OpenCode. This was the original purpose of this repo. |
| `tools/ipam-infoblox` | CLI | Converts an AWS IPAM export into an Infoblox import, checking ranges against the Infoblox API first. |

## Layout

```
tools/<name>/            standalone tools (CLIs, libraries)
plugins/<host>-<name>/   plugins for a host app, e.g. plugins/opencode-ipredact
.github/workflows/ci.yml one CI job per project
```

Each project folder contains:

- **`README.md`**: what it does, how to install it, how to use it, and its limits.
- **Its own build manifest**: `pyproject.toml` for Python, `package.json` for TypeScript and JavaScript.
- **`tests/`**: tests runnable from inside the folder with a single command, which the README states.

Projects don't import from each other through relative paths. If a plugin needs a tool, it depends on the tool's installed package or CLI. That way every project can be installed on its own.

## Adding a project

1. Create `tools/<name>/` or `plugins/<host>-<name>/` with the files above.
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
