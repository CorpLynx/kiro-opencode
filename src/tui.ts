/**
 * TUI half of the plugin: a live Kiro usage tracker.
 *
 *  - sidebar:    "Kiro" box with a context bar, this session's credits and the account's credits left
 *  - prompt row: compact "58% · 3.21 cr" next to the model name
 *  - a warning toast when the account's credits run low (once per allowance period)
 *  - a "Kiro usage" command (`/kiro`) that opens the full figures in a dialog
 *
 * Built with OpenTUI's element API rather than JSX, so it needs no JSX build step; the host
 * supplies `@opentui/solid` and `solid-js` at runtime. Data comes from the files the server
 * half writes (see tracker/store.ts).
 */
import type { TuiPlugin, TuiPluginApi, TuiPluginModule } from "@opencode-ai/plugin/tui"
import { createElement, effect, insert, insertNode, setProp } from "@opentui/solid"
import { createEffect, createMemo, createRoot, createSignal, type Accessor } from "solid-js"
import {
  accountLine,
  bar,
  chipText,
  detailText,
  formatCredits,
  formatPct,
  level,
  lowCredit,
  type Level,
} from "./tracker/format.js"
import { readAccount, readSession, usageDir } from "./tracker/store.js"

const POLL_MS = 1500
const BAR_WIDTH = 14
/** Warn when this fraction of the allowance or less is left (plugin option `lowCredits`). */
const DEFAULT_LOW_CREDITS = 0.1
const WARNED_KEY = "opencode-kiro-provider.lowCreditWarned"

type Node = ReturnType<typeof createElement>
type Color = TuiPluginApi["theme"]["current"]["text"]

function text(content: string | Accessor<string>, fg?: Accessor<Color>, ...children: Node[]): Node {
  const el = createElement("text")
  setProp(el, "wrapMode", "none")
  if (fg) effect(() => setProp(el, "fg", fg()))
  insert(el, content)
  for (const child of children) insertNode(el, child)
  return el
}

function span(content: Accessor<string>, fg: Accessor<Color>, tag: "span" | "b" = "span"): Node {
  const el = createElement(tag)
  // Text-node elements only take colour through `style`; a bare `fg` prop is ignored.
  effect(() => setProp(el, "style", { fg: fg() }))
  insert(el, content)
  return el
}

const tui: TuiPlugin = async (api, options) => {
  if (options?.tracker === false) return
  const dir = usageDir()

  // Re-read the usage files on a timer and whenever opencode reports message activity.
  const [tick, setTick] = createSignal(0)
  const bump = () => setTick((n) => n + 1)
  const timer = setInterval(bump, POLL_MS)
  api.lifecycle.onDispose(() => clearInterval(timer))
  api.lifecycle.onDispose(api.event.on("message.updated", bump))
  api.lifecycle.onDispose(api.event.on("session.idle", bump))

  const theme = () => api.theme.current
  const levelColor = (l: Level) => (l === "high" ? theme().error : l === "warn" ? theme().warning : theme().success)

  const session = (sessionID: string) =>
    createMemo(() => (tick(), readSession(dir, sessionID)), undefined, {
      equals: (a, b) => a?.updated === b?.updated,
    })
  const threshold =
    typeof options?.lowCredits === "number" && options.lowCredits >= 0 ? options.lowCredits : DEFAULT_LOW_CREDITS

  // Shared by every view; owned by a root that lives as long as the plugin.
  const account = createRoot((dispose) => {
    api.lifecycle.onDispose(dispose)
    const snapshot = createMemo(() => (tick(), readAccount(dir)), undefined, {
      equals: (a, b) => a?.updated === b?.updated,
    })

    // Low-credit warning: once per allowance period and level (low, then empty), across restarts.
    createEffect(() => {
      const low = threshold > 0 ? lowCredit(snapshot(), threshold) : undefined
      if (!low || !api.kv.ready || api.kv.get<string>(WARNED_KEY) === low.key) return
      api.kv.set(WARNED_KEY, low.key)
      const resets = low.resetsAt ? ` They reset on ${low.resetsAt}.` : ""
      api.ui.toast({
        variant: low.empty ? "error" : "warning",
        title: low.empty ? "Kiro credits used up" : "Kiro credits running low",
        message: `${formatCredits(low.left)} of ${formatCredits(low.limit)} credits left on your Kiro account.${resets}`,
        duration: 10_000,
      })
    })
    return snapshot
  })

  // "Kiro usage" in the command palette and as /kiro.
  const showUsage = () => {
    const route = api.route.current
    const sessionID = route.name === "session" ? (route.params as { sessionID?: string } | undefined)?.sessionID : undefined
    bump()
    api.ui.dialog.replace(() =>
      api.ui.DialogAlert({
        title: "Kiro usage",
        message: detailText(sessionID ? readSession(dir, sessionID) : undefined, readAccount(dir)),
        onConfirm: () => api.ui.dialog.clear(),
      }),
    )
  }
  const command = {
    title: "Kiro usage",
    value: "kiro.usage",
    description: "Context, credits and account quota for Kiro sessions",
    category: "Kiro",
    slash: { name: "kiro" },
    onSelect: showUsage,
  }
  if (api.command?.register) api.lifecycle.onDispose(api.command.register(() => [command]))

  function sidebar(sessionID: string) {
    const usage = session(sessionID)
    const pct = () => usage()?.contextPct ?? 0
    const muted = () => theme().textMuted
    const cells = createMemo(() => bar(pct(), BAR_WIDTH))

    const root = createElement("box")
    setProp(root, "flexDirection", "column")
    insertNode(root, text("", () => theme().text, span(() => "Kiro", () => theme().text, "b")))
    insertNode(
      root,
      text(
        "",
        muted,
        span(() => cells().filled, () => levelColor(level(pct()))),
        span(() => cells().empty, () => theme().borderSubtle),
        span(() => ` ${usage()?.contextPct === undefined ? "–" : formatPct(pct())} context`, muted),
      ),
    )
    insertNode(
      root,
      text(() => {
        const u = usage()
        return u ? `${formatCredits(u.credits)} credits · ${u.turns} ${u.turns === 1 ? "request" : "requests"}` : ""
      }, muted),
    )
    insertNode(
      root,
      text(() => {
        const line = accountLine(account())
        return line ? `${line} on account` : ""
      }, muted),
    )
    // Only Kiro sessions have a usage file; everything else renders nothing.
    return () => (usage() ? root : null)
  }

  function chip(sessionID: string) {
    const usage = session(sessionID)
    const el = text(() => chipText(usage()), () => {
      const pct = usage()?.contextPct
      return pct === undefined ? theme().textMuted : level(pct) === "ok" ? theme().textMuted : levelColor(level(pct))
    })
    setProp(el, "flexShrink", 0)
    return () => (usage() ? el : null)
  }

  api.slots.register({
    order: 50,
    slots: {
      sidebar_content: (_ctx, props) => sidebar(props.session_id) as any,
      session_prompt_right: (_ctx, props) => chip(props.session_id) as any,
    },
  })
}

const plugin: TuiPluginModule = { id: "opencode-kiro-provider", tui }
export default plugin
