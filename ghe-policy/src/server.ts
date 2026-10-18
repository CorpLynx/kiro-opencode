/** Server entry in opencode's module format (`exports["./server"]`). */
import type { PluginModule } from "@opencode-ai/plugin"
import { GhePolicyPlugin } from "./index.js"

const plugin: PluginModule = { id: "opencode-ghe-policy", server: GhePolicyPlugin }
export default plugin
