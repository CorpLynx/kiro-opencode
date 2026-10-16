import { appendFileSync, mkdirSync, renameSync, statSync } from "node:fs"
import { join } from "node:path"

/**
 * Opt-in diagnostics (`KIRO_DEBUG=1`, or `"debug": true` in the plugin options).
 *
 * Writes one JSON object per line to `<state dir>/debug.log`, rotated at 5 MB. It records the
 * *shape* of the traffic: sizes, counts, status codes, request ids, which stream event types
 * Kiro sent (unknown ones with their field names) and error messages. It never records
 * prompts, responses, tool output or credentials, so the file is safe to attach to a bug report.
 */
export type Debug = ((event: string, data?: Record<string, unknown>) => void) & {
  readonly enabled: boolean
  readonly file?: string
}

const MAX_BYTES = 5 * 1024 * 1024

export const noDebug: Debug = Object.assign(() => {}, { enabled: false as const })

export function debugEnabled(option: unknown, env: NodeJS.ProcessEnv = process.env): boolean {
  return option === true || /^(1|true|yes|on)$/i.test(env.KIRO_DEBUG ?? "")
}

export function createDebug(opts: {
  enabled: boolean
  dir: string
  /** Also forward each entry here (e.g. opencode's log). */
  mirror?: (event: string, data: Record<string, unknown>) => void
}): Debug {
  if (!opts.enabled) return noDebug
  const file = join(opts.dir, "debug.log")
  const write = (event: string, data: Record<string, unknown> = {}) => {
    const line = JSON.stringify({ t: new Date().toISOString(), event, ...data }) + "\n"
    try {
      mkdirSync(opts.dir, { recursive: true })
      try {
        if (statSync(file).size > MAX_BYTES) renameSync(file, `${file}.1`)
      } catch {
        /* no file yet */
      }
      appendFileSync(file, line)
    } catch {
      /* diagnostics must never break a request */
    }
    opts.mirror?.(event, data)
  }
  return Object.assign(write, { enabled: true as const, file })
}

/** Error text from Kiro, trimmed and with anything token-shaped masked. */
export function redact(text: string, max = 500): string {
  return text
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/g, "Bearer [redacted]")
    .replace(/\b(aoa|aor|eyJ)[A-Za-z0-9._~+/=-]{20,}/g, "[redacted]")
    .slice(0, max)
}
