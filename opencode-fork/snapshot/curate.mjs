// Build the model catalogue a hardened opencode embeds: the full models.dev catalogue, cut down to the
// providers the organization has approved. Unapproved providers then don't exist in the binary at all.
//   node curate.mjs <models.dev api.json> <approved-providers.json> <out.json>
import { readFileSync, writeFileSync } from "node:fs"

const [source, approvedFile, out] = process.argv.slice(2)
if (!source || !approvedFile || !out) {
  console.error("usage: node curate.mjs <api.json> <approved-providers.json> <out.json>")
  process.exit(2)
}
const all = JSON.parse(readFileSync(source, "utf8"))
const { providers } = JSON.parse(readFileSync(approvedFile, "utf8"))
if (!Array.isArray(providers) || !providers.length) throw new Error(`${approvedFile} must list "providers"`)

const missing = providers.filter((id) => !all[id])
if (missing.length) throw new Error(`not in the catalogue: ${missing.join(", ")}`)

const curated = Object.fromEntries(providers.map((id) => [id, all[id]]))
writeFileSync(out, JSON.stringify(curated))
const models = Object.values(curated).reduce((n, p) => n + Object.keys(p.models ?? {}).length, 0)
console.log(`kept ${providers.length} of ${Object.keys(all).length} providers (${models} models): ${providers.join(", ")}`)
