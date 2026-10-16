// Fails if the npm package would be missing an entry point, or would ship sources/tests by mistake.
let input = ""
for await (const chunk of process.stdin) input += chunk
const [pack] = JSON.parse(input)
const files = new Set(pack.files.map((f) => f.path))
const required = ["package.json", "README.md", "LICENSE", "CHANGELOG.md", "dist/index.js", "dist/server.js", "dist/tui.js"]
const missing = required.filter((f) => !files.has(f))
const stray = [...files].filter((f) => /^(src|test|e2e|scripts|docs)\//.test(f) || f.endsWith(".tgz"))
if (missing.length || stray.length) {
  if (missing.length) console.error(`missing from package: ${missing.join(", ")}`)
  if (stray.length) console.error(`should not be packaged: ${stray.join(", ")}`)
  process.exit(1)
}
console.log(`package ok: ${pack.name}@${pack.version}, ${files.size} files, ${(pack.unpackedSize / 1024).toFixed(0)} kB unpacked`)
