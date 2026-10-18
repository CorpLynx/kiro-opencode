# Agent guide

This repo is a collection of independent tools (`tools/<name>/`), host-app plugins (`plugins/<host>-<name>/`) and patched builds (`builds/<name>/`). See `README.md` for the catalog and layout.

## Working rules

- **Stay inside one project folder** unless the task says otherwise. Every project has its own README, build manifest and `tests/`.
- **Run the project's tests** with the command in its README before you finish. For Python projects that means `python3 -m unittest discover -s tests`, run from the project folder.
- **No cross-imports.** Projects never import each other through relative paths; a plugin uses a tool through its installed package or CLI.
- **New projects** also get a row in the root README catalog and an entry in the CI matrix in `.github/workflows/ci.yml`.
- **Python:**
  - Support 3.9 and later.
  - The core stays on the standard library, and heavier dependencies go behind optional extras that are loaded lazily, with a clear message when one is missing.
  - Match the surrounding style: small modules, docstrings that explain why, few comments.
- **Data:** never commit addresses, hostnames, credentials, captures or documents from real networks, including in test fixtures. Use documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`) or RFC 1918 space.
