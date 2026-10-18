// A tiny OpenAI-compatible model on 127.0.0.1 for the fork's tests: answers "Hello from the mock model.",
// or runs `RUN: <command>` through the bash tool and reports the result.
//   node mock-model.mjs <port>
import { createServer } from "node:http"

const port = Number(process.argv[2])
const text = (c) => (typeof c === "string" ? c : Array.isArray(c) ? c.map((p) => (p.type === "text" ? p.text : "")).join("") : "")
function sse(res, deltas, finish) {
  const chunk = (delta, finish_reason = null) =>
    `data: ${JSON.stringify({ id: "c1", object: "chat.completion.chunk", created: 0, model: "m", choices: [{ index: 0, delta, finish_reason }] })}\n\n`
  res.writeHead(200, { "content-type": "text/event-stream" })
  res.write(chunk({ role: "assistant", content: "" }))
  for (const d of deltas) res.write(chunk(d))
  res.write(chunk({}, finish))
  res.end("data: [DONE]\n\n")
}
createServer((req, res) => {
  let raw = ""
  req.on("data", (d) => (raw += d))
  req.on("end", () => {
    if (!req.url.endsWith("/chat/completions")) return res.writeHead(404).end()
    const body = JSON.parse(raw)
    const last = body.messages.at(-1)
    if (last.role === "tool") return sse(res, [{ content: `Tool said: ${text(last.content).slice(0, 300)}` }], "stop")
    const prompt = text(body.messages.filter((m) => m.role === "user").at(-1)?.content).replace(/^"([\s\S]*)"$/, "$1")
    const run = prompt.match(/RUN: (.+)$/m)
    if (run && body.tools?.length) {
      return sse(res, [{ tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "bash", arguments: JSON.stringify({ command: run[1], description: "run" }) } }] }], "tool_calls")
    }
    sse(res, [{ content: "Hello from the mock model." }], "stop")
  })
}).listen(port, "127.0.0.1", () => console.log(`mock model on ${port}`))
