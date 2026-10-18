/**
 * Server entry in opencode's module format (`exports["./server"]`). The package root (".")
 * keeps exporting the plugin function directly, for configs that point at it.
 */
import type { PluginModule } from "@opencode-ai/plugin"
import { KiroPlugin } from "./index.js"

const plugin: PluginModule = { id: "opencode-kiro-provider", server: KiroPlugin }
export default plugin
