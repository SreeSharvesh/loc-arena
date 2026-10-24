/**
 * pi's trace extension on a Meridian runner: a span for each model call and each tool call of one execution,
 * sent to the company's observability store (Phoenix) as the call ends.
 *
 * The agents service sets the execution's MERIDIAN_* variables, and the runner's PHOENIX_COLLECTOR_ENDPOINT says
 * where Phoenix is. An execution's spans share one trace and use OpenTelemetry's generative-AI attributes; with
 * MERIDIAN_CAPTURE_CONTENT=false they keep no prompt, reply, tool argument or tool result.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import { randomBytes } from "node:crypto";

const PROJECT = "meridian-agents";
const spansUrl = `${process.env.PHOENIX_COLLECTOR_ENDPOINT}/v1/projects/${PROJECT}/spans`;
const keepsContent = process.env.MERIDIAN_CAPTURE_CONTENT === "true";
const traceId = randomBytes(16).toString("hex");
const executionAttributes = {
  "meridian.execution.id": process.env.MERIDIAN_EXECUTION_ID,
  "meridian.execution.started_by": process.env.MERIDIAN_EXECUTION_STARTED_BY,
  "meridian.agent.name": process.env.MERIDIAN_AGENT,
  "meridian.agent.version": process.env.MERIDIAN_AGENT_VERSION,
  "meridian.runner": process.env.MERIDIAN_RUNNER,
};

interface EndedCall {
  name: string;
  kind: "LLM" | "TOOL";
  startTime: string;
  error: string | undefined;
  attributes: Record<string, unknown>;
  content: Record<string, unknown>;
}

async function sendSpan(call: EndedCall): Promise<void> {
  const span = {
    name: call.name,
    context: { trace_id: traceId, span_id: randomBytes(8).toString("hex") },
    span_kind: call.kind,
    start_time: call.startTime,
    end_time: new Date().toISOString(),
    status_code: call.error === undefined ? "OK" : "ERROR",
    status_message: call.error ?? "",
    attributes: { ...executionAttributes, ...call.attributes, ...(keepsContent ? call.content : {}) },
  };
  const response = await fetch(spansUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ data: [span] }),
  });
  if (!response.ok) {
    throw new Error(`Phoenix refused a span: ${response.status} ${await response.text()}`);
  }
}

export default function (pi: ExtensionAPI) {
  let latestRequest: { startTime: string; messages: unknown } | undefined;
  const toolCallsById = new Map<string, { startTime: string; args: unknown }>();

  pi.on("before_provider_request", async (event) => {
    const payload = event.payload as { messages?: unknown };
    latestRequest = { startTime: new Date().toISOString(), messages: payload.messages };
  });

  pi.on("message_end", async (event) => {
    if (event.message.role !== "assistant") {
      return;
    }
    const message = event.message as AssistantMessage;
    const request = latestRequest!;
    await sendSpan({
      name: `chat ${message.model}`,
      kind: "LLM",
      startTime: request.startTime,
      error: message.stopReason === "error" ? (message.errorMessage ?? "error") : undefined,
      attributes: {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": message.provider,
        "gen_ai.request.model": message.model,
        "gen_ai.response.model": message.responseModel ?? message.model,
        "gen_ai.response.id": message.responseId,
        "gen_ai.response.finish_reasons": [message.stopReason],
        "gen_ai.usage.input_tokens": message.usage.input,
        "gen_ai.usage.output_tokens": message.usage.output,
      },
      content: {
        "gen_ai.input.messages": JSON.stringify(request.messages),
        "gen_ai.output.messages": JSON.stringify(message.content),
      },
    });
  });

  pi.on("tool_execution_start", async (event) => {
    toolCallsById.set(event.toolCallId, { startTime: new Date().toISOString(), args: event.args });
  });

  pi.on("tool_execution_end", async (event) => {
    const started = toolCallsById.get(event.toolCallId)!;
    toolCallsById.delete(event.toolCallId);
    await sendSpan({
      name: `execute_tool ${event.toolName}`,
      kind: "TOOL",
      startTime: started.startTime,
      error: event.isError ? "the tool failed" : undefined,
      attributes: {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": event.toolName,
        "gen_ai.tool.call.id": event.toolCallId,
      },
      content: {
        "gen_ai.tool.call.arguments": JSON.stringify(started.args),
        "gen_ai.tool.call.result": JSON.stringify(event.result),
      },
    });
  });
}
