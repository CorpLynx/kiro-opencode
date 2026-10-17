/**
 * One local server playing two parts for e2e/run.sh:
 *  - GitHub Enterprise's Contents API, serving the policy file (requires `Bearer test-token`);
 *  - a tiny OpenAI-compatible model. If the prompt says `RUN: <command>` it calls the bash tool with
 *    that command; `STATUS` calls policy_status; after a tool result it replies "Tool said: …".
 * Run with: node --experimental-strip-types e2e/mock.ts <port> <policy.json> <request-log.jsonl>
 */
import { appendFileSync, readFileSync } from "node:fs"
import { createServer, type ServerResponse } from "node:http"

const [port, policyFile, log] = [Number(process.argv[2]), process.argv[3]!, process.argv[4]!]
let gheUp = true

const json = (res: ServerResponse, status: number, body: unknown) =>
  res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body))

function sse(res: ServerResponse, deltas: Record<string, unknown>[], finish: string) {
  res.writeHead(200, { "content-type": "text/event-stream" })
  const chunk = (delta: Record<string, unknown>, finish_reason: string | null = null) =>
    `data: ${JSON.stringify({ id: "c1", object: "chat.completion.chunk", created: 0, model: "m", choices: [{ index: 0, delta, finish_reason }] })}\n\n`
  res.write(chunk({ role: "assistant", content: "" }))
  for (const d of deltas) res.write(chunk(d))
  res.write(chunk({}, finish))
  res.end(`data: ${JSON.stringify({ id: "c1", object: "chat.completion.chunk", created: 0, model: "m", choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } })}\n\ndata: [DONE]\n\n`)
}

const text = (c: unknown): string =>
  typeof c === "string" ? c : Array.isArray(c) ? c.map((p: any) => (p.type === "text" ? p.text : "")).join("") : ""

createServer((req, res) => {
  let raw = ""
  req.on("data", (d) => (raw += d))
  req.on("end", () => {
    const url = new URL(req.url!, "http://mock")
    appendFileSync(log, JSON.stringify({ method: req.method, path: url.pathname, query: url.search, auth: req.headers.authorization, body: raw ? JSON.parse(raw) : undefined }) + "\n")

    if (url.pathname === "/__mock/ghe") {
      gheUp = url.searchParams.get("up") === "1"
      return json(res, 200, { gheUp })
    }
    if (url.pathname.startsWith("/api/v3/repos/")) {
      if (!gheUp) return json(res, 503, { message: "Service unavailable" })
      if (req.headers.authorization !== "Bearer test-token") return json(res, 401, { message: "Bad credentials" })
      if (url.pathname !== "/api/v3/repos/platform/opencode-policy/contents/opencode-policy.json" || url.searchParams.get("ref") !== "main") {
        return json(res, 404, { message: "Not Found" })
      }
      const content = readFileSync(policyFile)
      return json(res, 200, { type: "file", encoding: "base64", sha: "0123456789abcdef0123", content: content.toString("base64") })
    }
    if (url.pathname === "/v1/chat/completions") {
      const body = JSON.parse(raw)
      const msgs: any[] = body.messages
      const last = msgs[msgs.length - 1]
      if (last.role === "tool") return sse(res, [{ content: `Tool said: ${text(last.content).slice(0, 400)}` }], "stop")
      // `opencode run` quotes multi-word messages: "RUN: echo hi" arrives with the quotes.
      const prompt = text(msgs.filter((m) => m.role === "user").at(-1)?.content).replace(/^"([\s\S]*)"$/, "$1")
      const call = (name: string, args: unknown) =>
        sse(res, [{ tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name, arguments: JSON.stringify(args) } }] }], "tool_calls")
      const run = prompt.match(/RUN: (.+)$/m)
      if (run && body.tools) return call("bash", { command: run[1], description: "Run the requested command" })
      if (/STATUS/.test(prompt) && body.tools) return call("policy_status", {})
      return sse(res, [{ content: "Hello from the mock model." }], "stop")
    }
    json(res, 404, { message: "not found" })
  })
}).listen(port, "127.0.0.1", () => console.log(`mock listening on ${port}`))
