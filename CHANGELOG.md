# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-16

First release.

### Added

- Kiro as an opencode provider: Claude, open-weight and `auto` models through Kiro's API, with streaming,
  tool calls, images and thinking variants.
- Sign-in with AWS Builder ID, IAM Identity Center, Google or GitHub (experimental), or by importing an
  existing Kiro IDE / kiro-cli login (Linux, macOS and Windows). Tokens refresh automatically.
- Live model discovery: the account's real model list and limits, cached for the next launch, with a
  built-in fallback.
- Real token counts from Kiro when it reports them, otherwise estimates from its context figure.
- Usage tracker in the opencode TUI (1.19+): context bar, session credits and account credits in the sidebar,
  a compact readout by the prompt, a low-credit warning and a `/kiro` usage dialog.
- `kiro_usage` tool for asking about remaining credits.
- Automatic retries for throttling, server errors and transient stream failures that happen before any output;
  oversized prompts are handed to opencode's compaction without a wasted request.
- Opt-in diagnostics (`KIRO_DEBUG=1`): a redacted log of request and stream shapes for bug reports.

[Unreleased]: https://github.com/CorpLynx/kiro-opencode/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CorpLynx/kiro-opencode/releases/tag/v0.1.0
