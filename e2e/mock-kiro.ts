/**
 * Minimal stand-in for Kiro's API, OIDC and desktop-auth endpoints, used by e2e/run.sh.
 * Run with: node --experimental-strip-types e2e/mock-kiro.ts <port> <request-log.jsonl>
 *
 * Behaviour is keyed off the prompt text so each scenario can steer the "model".
 */
import { appendFileSync } from "node:fs"
import { createServer, type ServerResponse } from "node:http"
import { concat, contextUsage, exception, text, toolUse } from "../test/helpers/eventstream.ts"

const port = Number(process.argv[2] ?? 18765)
const log = process.argv[3] ?? "requests.jsonl"
let devicePolls = 0
let overflowServed = false

const json = (res: ServerResponse, status: number, body: unknown) => {
  res.writeHead(status, { "content-type": "application/json" })
  res.end(JSON.stringify(body))
}
const stream = (res: ServerResponse, ...frames: Uint8Array[]) => {
  res.writeHead(200, { "content-type": "application/vnd.amazon.eventstream" })
  res.end(Buffer.from(concat(...frames, contextUsage(2))))
}

createServer((req, res) => {
  let raw = ""
  req.on("data", (d) => (raw += d))
  req.on("end", () => {
    const url = new URL(req.url!, "http://mock")
    const body = raw ? JSON.parse(raw) : {}
    appendFileSync(log, JSON.stringify({ method: req.method, path: url.pathname, auth: req.headers.authorization, body }) + "\n")

    switch (url.pathname) {
      case "/client/register":
        return json(res, 200, { clientId: "cid", clientSecret: "csec" })
      case "/device_authorization":
        return json(res, 200, {
          deviceCode: "dc",
          userCode: "ABCD-EFGH",
          verificationUri: `http://127.0.0.1:${port}/verify`,
          verificationUriComplete: `http://127.0.0.1:${port}/verify?code=ABCD-EFGH`,
          interval: 1,
          expiresIn: 60,
        })
      case "/token":
        if (body.grantType === "refresh_token") return json(res, 200, { accessToken: "idc-refreshed", expiresIn: 3600 })
        if (devicePolls++ === 0) return json(res, 400, { error: "authorization_pending" })
        return json(res, 200, { accessToken: "device-access", refreshToken: "device-refresh", expiresIn: 3600 })
      case "/refreshToken":
        return json(res, 200, { accessToken: "social-refreshed", refreshToken: "social-rotated", expiresIn: 3600 })
      case "/getUsageLimits":
        return json(res, 200, {
          userInfo: { email: "dev@example.com" },
          subscriptionInfo: { subscriptionTitle: "KIRO PRO" },
          usageBreakdownList: [{ displayName: "Credits", currentUsage: 42, usageLimit: 1000 }],
        })
      case "/generateAssistantResponse": {
        const cur = body.conversationState.currentMessage.userInputMessage
        const results = cur.userInputMessageContext?.toolResults
        const agentRequest = Boolean(cur.userInputMessageContext?.tools) // title generation sends no tools
        const content: string = cur.content
        if (results?.length) return stream(res, text("Tool said: "), text(results[0].content[0].text.slice(0, 300)))
        if (/explode/.test(content)) {
          return stream(res, text("partial answer "), exception("InternalServerException", "Mock Kiro blew up mid-stream"))
        }
        if (/overflow/.test(content) && agentRequest && !overflowServed) {
          overflowServed = true
          return json(res, 400, { message: "Input is too long.", reason: "CONTENT_LENGTH_EXCEEDS_THRESHOLD" })
        }
        if (/check my kiro usage/i.test(content) && agentRequest) {
          return stream(res, toolUse("u1", "kiro_usage", ""), toolUse("u1", "kiro_usage", "", true))
        }
        if (/use the tool/i.test(content) && agentRequest) {
          return stream(
            res,
            toolUse("b1", "bash", ""),
            toolUse("b1", "bash", '{"command":"echo hi",'),
            toolUse("b1", "bash", '"description":"Echo hi"}'),
            toolUse("b1", "bash", "", true),
          )
        }
        return stream(res, text("Hello from mock Kiro."))
      }
      default:
        return json(res, 404, { message: "not found" })
    }
  })
}).listen(port, "127.0.0.1", () => console.log(`mock kiro listening on ${port}`))
