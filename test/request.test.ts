import { describe, expect, it } from "vitest"
import { transformRequest, type OpenAIMessage } from "../src/transform/request.js"

const tool = (name: string, parameters: unknown = { type: "object", properties: { x: { type: "string" } } }) => ({
  type: "function" as const,
  function: { name, description: `${name} tool`, parameters },
})
const run = (messages: OpenAIMessage[], extra: object = {}) =>
  transformRequest({ model: "claude-sonnet-4.5", messages, ...extra }, { conversationId: "c1" })

describe("transformRequest", () => {
  it("sends a single user message as the current message", () => {
    const r = run([{ role: "user", content: "hi" }])
    const cs = r.body.conversationState
    expect(cs.conversationId).toBe("c1")
    expect(cs.history).toBeUndefined()
    expect(cs.currentMessage.userInputMessage).toMatchObject({ content: "hi", modelId: "claude-sonnet-4.5", origin: "AI_EDITOR" })
  })

  it("prepends the system prompt to the first user message", () => {
    const r = run([
      { role: "system", content: "SYS" },
      { role: "user", content: "one" },
      { role: "assistant", content: "two" },
      { role: "user", content: "three" },
    ])
    const cs = r.body.conversationState
    expect((cs.history![0] as any).userInputMessage.content).toBe("SYS\n\none")
    expect((cs.history![1] as any).assistantResponseMessage.content).toBe("two")
    expect(cs.currentMessage.userInputMessage.content).toBe("three")
  })

  it("merges consecutive same-role messages and drops empty assistant turns", () => {
    const r = run([
      { role: "user", content: "a" },
      { role: "user", content: [{ type: "text", text: "b" }] },
      { role: "assistant", content: "" },
      { role: "user", content: "c" },
    ])
    expect(r.body.conversationState.history).toBeUndefined()
    expect(r.body.conversationState.currentMessage.userInputMessage.content).toBe("a\n\nb\n\nc")
  })

  it("adds a Continue turn when the conversation ends on the assistant", () => {
    const r = run([
      { role: "user", content: "a" },
      { role: "assistant", content: "b" },
    ])
    expect(r.body.conversationState.currentMessage.userInputMessage.content).toBe("Continue")
    expect(r.body.conversationState.history).toHaveLength(2)
  })

  it("pairs tool calls with tool results", () => {
    const r = run(
      [
        { role: "user", content: "go" },
        { role: "assistant", content: null, tool_calls: [{ id: "t1", function: { name: "bash", arguments: '{"x":"1"}' } }] },
        { role: "tool", tool_call_id: "t1", content: "done" },
      ],
      { tools: [tool("bash")] },
    )
    const cs = r.body.conversationState
    expect((cs.history![1] as any).assistantResponseMessage.toolUses).toEqual([{ toolUseId: "t1", name: "bash", input: { x: "1" } }])
    const cur = cs.currentMessage.userInputMessage
    expect(cur.userInputMessageContext?.toolResults).toEqual([{ toolUseId: "t1", status: "success", content: [{ text: "done" }] }])
    expect(cur.content).toBe("Tool results provided.")
    expect(cur.userInputMessageContext?.tools).toHaveLength(1)
  })

  it("synthesises results for interrupted tool calls and demotes orphan results to text", () => {
    const r = run([
      { role: "user", content: "go" },
      { role: "assistant", content: "x", tool_calls: [{ id: "t1", function: { name: "bash", arguments: "{}" } }] },
      { role: "user", content: "never mind" },
      { role: "tool", tool_call_id: "ghost", content: "stray output" },
    ])
    const cur = r.body.conversationState.currentMessage.userInputMessage
    expect(r.body.conversationState.history).toHaveLength(2)
    expect(cur.userInputMessageContext?.toolResults).toEqual([
      { toolUseId: "t1", status: "error", content: [{ text: "Tool call was interrupted before producing a result." }] },
    ])
    expect(cur.content).toContain("never mind")
    expect(cur.content).toContain("stray output")
  })

  it("stubs tools that appear in history but not in the tool list", () => {
    const r = run([
      { role: "user", content: "go" },
      { role: "assistant", content: "x", tool_calls: [{ id: "t1", function: { name: "old_tool", arguments: "{}" } }] },
      { role: "tool", tool_call_id: "t1", content: "ok" },
    ])
    const tools = r.body.conversationState.currentMessage.userInputMessage.userInputMessageContext!.tools!
    expect(tools.map((t) => t.toolSpecification.name)).toEqual(["old_tool"])
  })

  it("sanitises tool names and schemas, remembering the original name", () => {
    const r = run([{ role: "user", content: "x" }], {
      tools: [tool("mcp.server/do thing", { $schema: "http://x", type: "object", properties: {}, required: [] }), tool("plain", null)],
    })
    const specs = r.body.conversationState.currentMessage.userInputMessage.userInputMessageContext!.tools!
    expect(specs[0]!.toolSpecification.name).toBe("mcp_server_do_thing")
    expect(r.toolNames.get("mcp_server_do_thing")).toBe("mcp.server/do thing")
    expect(specs[0]!.toolSpecification.inputSchema.json).toEqual({ type: "object", properties: {} })
    expect(specs[1]!.toolSpecification.inputSchema.json).toEqual({ type: "object", properties: {} })
  })

  it("requests thinking mode through the system prompt", () => {
    const r = run([{ role: "system", content: "SYS" }, { role: "user", content: "q" }], { reasoning_effort: "high" })
    expect(r.thinking).toBe(true)
    expect(r.body.conversationState.currentMessage.userInputMessage.content).toBe(
      "<thinking_mode>enabled</thinking_mode><max_thinking_length>24000</max_thinking_length>\n\nSYS\n\nq",
    )
    expect(run([{ role: "user", content: "q" }]).thinking).toBe(false)
  })

  it("forwards inline images and includes the profile ARN", () => {
    const r = transformRequest(
      { model: "m", messages: [{ role: "user", content: [{ type: "text", text: "see" }, { type: "image_url", image_url: { url: "data:image/jpg;base64,QUJD" } }, { type: "image_url", image_url: { url: "https://x/y.png" } }] }] },
      { profileArn: "arn:aws:codewhisperer:us-east-1:1:profile/X" },
    )
    expect(r.body.conversationState.currentMessage.userInputMessage.images).toEqual([{ format: "jpeg", source: { bytes: "QUJD" } }])
    expect(r.body.profileArn).toContain("profile/X")
  })

  it("truncates huge tool output", () => {
    const r = transformRequest(
      { model: "m", messages: [{ role: "user", content: "x" }, { role: "assistant", content: "", tool_calls: [{ id: "t", function: { name: "n", arguments: "{}" } }] }, { role: "tool", tool_call_id: "t", content: "z".repeat(5000) }] },
      { maxToolResultChars: 1000 },
    )
    const text = r.body.conversationState.currentMessage.userInputMessage.userInputMessageContext!.toolResults![0]!.content[0]!.text
    expect(text.length).toBeLessThan(1200)
    expect(text).toContain("truncated")
  })
})
