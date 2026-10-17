import type { GuardPolicy } from "./policy.js"

/**
 * Guardrails applied before the agent runs a shell command or fetches a URL.
 *
 * This is defence in depth, not a sandbox: it understands ordinary shell (quotes, `&&`/`;`/`|`
 * chains, `$(…)`, `bash -c`, env prefixes, `sudo`) well enough that the agent can't stumble past a
 * rule, but determined obfuscation needs OS- and network-level controls as well.
 */

export type Verdict = { allowed: true } | { allowed: false; reason: string }

export interface GuardContext {
  /** Current branch of the repository a command runs in (`git rev-parse --abbrev-ref HEAD`). */
  currentBranch?: (gitDir?: string) => string | undefined
  /** URL of a named git remote (`git remote get-url <name>`). */
  remoteUrl?: (name: string, gitDir?: string) => string | undefined
}

/* ---------- Shell scanning ---------- */

interface Scan {
  /** Simple commands, each as its list of words. */
  commands: string[][]
  /** Bodies of `$(…)` and backtick substitutions, to be scanned in turn. */
  nested: string[]
}

const SEPARATORS = new Set([";", "&", "|", "\n", "(", ")"])

export function scan(input: string): Scan {
  const commands: string[][] = []
  const nested: string[] = []
  let words: string[] = []
  let word = ""
  let inWord = false
  const endWord = () => {
    if (inWord) words.push(word)
    word = ""
    inWord = false
  }
  const endCommand = () => {
    endWord()
    if (words.length) commands.push(words)
    words = []
  }
  /** Read a `$(…)` body starting after the `(`; returns the index of the closing `)`. */
  const substitution = (from: number): number => {
    let depth = 1
    let i = from
    let quote: string | undefined
    for (; i < input.length; i++) {
      const c = input[i]!
      if (quote) {
        if (c === "\\" && quote === '"') i++
        else if (c === quote) quote = undefined
      } else if (c === "'" || c === '"') quote = c
      else if (c === "\\") i++
      else if (c === "(") depth++
      else if (c === ")" && --depth === 0) break
    }
    nested.push(input.slice(from, i))
    return i
  }

  for (let i = 0; i < input.length; i++) {
    const c = input[i]!
    if (c === "'") {
      const end = input.indexOf("'", i + 1)
      word += input.slice(i + 1, end < 0 ? input.length : end)
      inWord = true
      i = end < 0 ? input.length : end
    } else if (c === '"') {
      inWord = true
      for (i++; i < input.length && input[i] !== '"'; i++) {
        const d = input[i]!
        if (d === "\\" && i + 1 < input.length) word += input[++i]
        else if (d === "$" && input[i + 1] === "(") i = substitution(i + 2)
        else if (d === "`") {
          const end = input.indexOf("`", i + 1)
          nested.push(input.slice(i + 1, end < 0 ? input.length : end))
          i = end < 0 ? input.length : end
        } else word += d
      }
    } else if (c === "\\") {
      if (input[i + 1] === "\n") i++ // line continuation
      else {
        word += input[++i] ?? ""
        inWord = true
      }
    } else if (c === "$" && input[i + 1] === "(") {
      i = substitution(i + 2)
      inWord = true
    } else if (c === "`") {
      const end = input.indexOf("`", i + 1)
      nested.push(input.slice(i + 1, end < 0 ? input.length : end))
      i = end < 0 ? input.length : end
      inWord = true
    } else if (c === "#" && !inWord) {
      while (i < input.length && input[i] !== "\n") i++
      endCommand()
    } else if (SEPARATORS.has(c)) {
      endCommand()
    } else if (/\s/.test(c)) {
      endWord()
    } else {
      word += c
      inWord = true
    }
  }
  endCommand()
  return { commands, nested }
}

const WRAPPERS = new Set(["sudo", "command", "exec", "nohup", "time", "nice", "builtin", "doas"])
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/

/** Drop env assignments and wrappers (`FOO=1 sudo -E git push` → `git push`). */
function unwrap(words: string[]): string[] {
  let w = words
  for (;;) {
    while (w.length && ASSIGNMENT.test(w[0]!)) w = w.slice(1)
    const head = w[0]?.replace(/^.*\//, "") // /usr/bin/git → git
    if (head === "env") {
      w = w.slice(1)
      while (w.length && (w[0]!.startsWith("-") || ASSIGNMENT.test(w[0]!))) w = w.slice(1)
      continue
    }
    if (head && WRAPPERS.has(head)) {
      w = w.slice(1)
      while (w.length && w[0]!.startsWith("-")) w = w.slice(1)
      continue
    }
    if (head && w[0] !== head) w = [head, ...w.slice(1)]
    return w
  }
}

/** Every simple command in `command`, including ones inside substitutions and `sh -c` strings. */
export function simpleCommands(command: string, depth = 0): string[][] {
  if (depth > 5) return []
  const { commands, nested } = scan(command)
  const out: string[][] = []
  for (const raw of commands) {
    const words = unwrap(raw)
    if (!words.length) continue
    out.push(words)
    const [head, ...rest] = words
    if ((head === "bash" || head === "sh" || head === "zsh" || head === "dash" || head === "ksh") && rest.includes("-c")) {
      const script = rest[rest.indexOf("-c") + 1]
      if (script) out.push(...simpleCommands(script, depth + 1))
    } else if (head === "eval") {
      out.push(...simpleCommands(rest.join(" "), depth + 1))
    }
  }
  for (const body of nested) out.push(...simpleCommands(body, depth + 1))
  return out
}

/* ---------- git ---------- */

const GIT_GLOBAL_WITH_VALUE = new Set(["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"])

/** `git [global options] <sub> args…` → { sub, args, gitDir }. */
export function parseGit(words: string[]): { sub: string; args: string[]; gitDir?: string } | undefined {
  if (words[0] !== "git") return undefined
  let gitDir: string | undefined
  let i = 1
  for (; i < words.length; i++) {
    const w = words[i]!
    if (w === "-C") gitDir = words[++i]
    else if (GIT_GLOBAL_WITH_VALUE.has(w)) i++
    else if (!w.startsWith("-")) break
  }
  const sub = words[i]
  return sub ? { sub, args: words.slice(i + 1), gitDir } : undefined
}

function globToRegExp(glob: string): RegExp {
  let re = ""
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]!
    if (c === "*" && glob[i + 1] === "*") {
      re += ".*"
      i++
    } else if (c === "*") re += "[^/]*"
    else if (c === "?") re += "[^/]"
    else re += c.replace(/[.+^${}()|[\]\\]/g, "\\$&")
  }
  return new RegExp(`^${re}$`)
}

const PUSH_WITH_VALUE = new Set(["-o", "--push-option", "--receive-pack", "--exec", "--repo"])

function analysePush(args: string[], gitDir: string | undefined, policy: GuardPolicy, ctx: GuardContext): Verdict {
  let force = false
  let all = false
  const positional: string[] = []
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!
    if (a === "-f" || a === "--force" || a.startsWith("--force-with-lease") || a === "--force-if-includes") force = true
    else if (a === "--all" || a === "--mirror" || a === "--branches") all = true
    else if (/^-[a-zA-Z]*f[a-zA-Z]*$/.test(a)) force = true // bundled short flags, e.g. -uf
    else if (PUSH_WITH_VALUE.has(a)) i++
    else if (!a.startsWith("-")) positional.push(a)
  }
  const [, ...refspecs] = positional
  if (refspecs.some((r) => r.startsWith("+"))) force = true

  if (force && policy.blockForcePush) {
    return { allowed: false, reason: "force-pushing is not allowed; push a new commit or open a pull request instead" }
  }
  const protectedGlobs = (policy.protectedBranches ?? []).map(globToRegExp)
  if (!protectedGlobs.length) return { allowed: true }
  const isProtected = (branch: string) => protectedGlobs.some((re) => re.test(branch))
  const pattern = policy.protectedBranches!.join(", ")

  if (all) return { allowed: false, reason: `pushing all branches is not allowed (protected: ${pattern})` }

  const targets: string[] = []
  for (const spec of refspecs.length ? refspecs : ["HEAD"]) {
    const s = spec.replace(/^\+/, "")
    const dst = s.includes(":") ? s.slice(s.indexOf(":") + 1) : s
    targets.push(dst === "HEAD" || dst === "" ? "HEAD" : dst.replace(/^refs\/heads\//, ""))
  }
  for (const t of targets) {
    const branch = t === "HEAD" ? ctx.currentBranch?.(gitDir) : t
    if (branch === undefined) {
      return { allowed: false, reason: "couldn't tell which branch this push targets; name the branch explicitly (e.g. `git push origin my-branch`)" }
    }
    if (isProtected(branch)) {
      return { allowed: false, reason: `pushing to protected branch '${branch}' is not allowed; push a feature branch and open a pull request` }
    }
  }
  return { allowed: true }
}

/* ---------- network egress ---------- */

const NETWORK_COMMANDS = new Set([
  "curl", "wget", "http", "https", "xh", "nc", "ncat", "netcat", "telnet", "ftp", "sftp", "scp", "ssh", "rsync",
])
const GIT_NETWORK = new Set(["clone", "fetch", "pull", "push", "ls-remote", "submodule", "remote"])
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"])

export function hostAllowed(host: string, allowed: string[]): boolean {
  const h = host.toLowerCase().replace(/\.$/, "")
  if (LOCAL_HOSTS.has(h)) return true
  return allowed.some((pattern) => {
    const p = pattern.toLowerCase()
    return p.startsWith("*.") ? h.endsWith(p.slice(1)) : h === p
  })
}

/** Hosts a word refers to: URLs, scp-style `user@host:path`, or (for some tools) bare hosts. */
export function hostsIn(word: string, opts: { bare?: boolean; scp?: boolean } = {}): string[] {
  const url = word.match(/^[a-z][a-z0-9+.-]*:\/\/(?:[^@/]*@)?(\[[^\]]+\]|[^/:?#]+)/i)
  if (url) return [url[1]!]
  if (opts.scp) {
    const scp = word.match(/^(?:[^@/:\s]+@)?([A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z0-9-]+):(?!\/\/)/)
    if (scp && !/^[A-Za-z]:\\/.test(word)) return [scp[1]!]
  }
  if (opts.bare) {
    const bare = word.match(/^(?:[^@/\s]+@)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?::\d+)?(?:\/|$)/)
    if (bare) return [bare[1]!]
  }
  return []
}

function egressHosts(words: string[], gitDir: string | undefined, ctx: GuardContext): string[] {
  const [head, ...rest] = words
  if (head === "git") {
    const g = parseGit(words)
    if (!g || !GIT_NETWORK.has(g.sub)) return []
    const hosts = g.args.flatMap((a) => hostsIn(a, { scp: true }))
    // `git push origin …`: check where the named remote actually points.
    if ((g.sub === "push" || g.sub === "fetch" || g.sub === "pull") && !hosts.length) {
      const remote = g.args.find((a) => !a.startsWith("-")) ?? "origin"
      const url = ctx.remoteUrl?.(remote, g.gitDir ?? gitDir)
      if (url) hosts.push(...hostsIn(url, { scp: true }))
    }
    return hosts
  }
  if (!head || !NETWORK_COMMANDS.has(head)) return []
  const scp = head === "scp" || head === "rsync" || head === "sftp"
  const bare = head === "curl" || head === "wget" || head === "http" || head === "https" || head === "xh"
  const hosts = rest.filter((a) => !a.startsWith("-")).flatMap((a) => hostsIn(a, { scp, bare }))
  if ((head === "ssh" || head === "telnet" || head === "nc" || head === "ncat" || head === "netcat" || head === "ftp") && !hosts.length) {
    const target = rest.find((a) => !a.startsWith("-"))
    if (target) hosts.push(target.replace(/^[^@]*@/, ""))
  }
  return hosts
}

/* ---------- public checks ---------- */

/** Should the agent be allowed to run this shell command? */
export function checkCommand(command: string, policy: GuardPolicy | undefined, ctx: GuardContext = {}): Verdict {
  if (!policy) return { allowed: true }
  for (const rule of policy.deny ?? []) {
    if (new RegExp(rule.pattern).test(command)) return { allowed: false, reason: rule.reason }
  }
  for (const words of simpleCommands(command)) {
    const git = parseGit(words)
    if (git?.sub === "push") {
      const verdict = analysePush(git.args, git.gitDir, policy, ctx)
      if (!verdict.allowed) return verdict
    }
    if (policy.allowedHosts) {
      for (const host of egressHosts(words, git?.gitDir, ctx)) {
        if (!hostAllowed(host, policy.allowedHosts)) {
          return { allowed: false, reason: `network access to '${host}' is not allowed (approved hosts: ${policy.allowedHosts.join(", ")})` }
        }
      }
    }
  }
  return { allowed: true }
}

/** Should the agent be allowed to fetch this URL? */
export function checkUrl(url: string, policy: GuardPolicy | undefined): Verdict {
  if (!policy?.allowedHosts) return { allowed: true }
  const [host] = hostsIn(url)
  if (!host) return { allowed: false, reason: `'${url}' is not a URL the policy can check` }
  return hostAllowed(host, policy.allowedHosts)
    ? { allowed: true }
    : { allowed: false, reason: `fetching from '${host}' is not allowed (approved hosts: ${policy.allowedHosts.join(", ")})` }
}
