// An HTTP(S) proxy that records every destination a client tries to reach, and refuses all of them.
//   node egress-recorder.mjs <port> <log.jsonl>
import { appendFileSync } from "node:fs"
import { createServer } from "node:http"

const [port, log] = [Number(process.argv[2]), process.argv[3]]
const record = (kind, target) => appendFileSync(log, JSON.stringify({ t: Date.now(), kind, target }) + "\n")
const server = createServer((req, res) => {
  let host = req.headers.host
  try {
    host = new URL(req.url).host
  } catch {}
  record("http", host)
  res.writeHead(403).end("blocked by egress recorder")
})
server.on("connect", (req, socket) => {
  socket.on("error", () => {}) // clients often hang up first; that must not stop the recording
  record("connect", req.url)
  socket.end("HTTP/1.1 403 Forbidden\r\n\r\n")
})
server.on("clientError", (_err, socket) => socket.destroy())
server.listen(port, "127.0.0.1", () => console.log(`egress recorder on ${port}`))
