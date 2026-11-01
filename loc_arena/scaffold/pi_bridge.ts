/**
 * pi's side of the arena's pi harness: pi runs one agent's loop, and the episode plays each of its turns.
 *
 * pi's own read, write, edit and bash keep pi's behaviour but act through operations the episode serves on the
 * agent's checkout and in its sandbox. Every other tool the agent is offered (the services', finish) is the
 * episode's own, and pi only forwards its call. A call waits until the episode plays it in the agent's turn,
 * and its result reaches pi when the agent's next turn starts. The episode answers on loopback, at
 * LOC_ARENA_PI_BRIDGE, to the agent's key alone.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  createBashToolDefinition,
  createEditToolDefinition,
  createReadToolDefinition,
  createWriteToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";

interface ArenaTool {
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

interface Answer {
  status: string;
  [field: string]: unknown;
}

interface ToolResult {
  content: Array<{ type: string; text?: string }>;
}

const bridge = process.env.LOC_ARENA_PI_BRIDGE;
const key = process.env.LOC_ARENA_PI_KEY;
const arenaTools: ArenaTool[] = JSON.parse(readFileSync(String(process.env.LOC_ARENA_PI_TOOLS), "utf8"));
let operations = 0;

// The episode holds each ask until it has an answer or a while has passed, then is asked again.
async function ask(path: string, body: unknown): Promise<Answer> {
  for (;;) {
    const reply = await fetch(`${bridge}${path}`, {
      method: "POST",
      headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!reply.ok) throw new Error(`the episode refused ${path}: ${reply.status} ${await reply.text()}`);
    const answer = (await reply.json()) as Answer;
    if (answer.status !== "pending") return answer;
  }
}

async function operate(op: string, fields: Record<string, unknown>): Promise<Answer> {
  const answer = await ask("/ops", { id: operations++, op, ...fields });
  if (answer.status === "error") throw new Error(String(answer.error));
  return answer;
}

const files = {
  readFile: async (path: string) => Buffer.from(String((await operate("read", { path })).base64), "base64"),
  access: async (path: string) => {
    await operate("access", { path });
  },
  writeFile: async (path: string, content: string) => {
    await operate("write", { path, content });
  },
  mkdir: async (path: string) => {
    await operate("mkdir", { path });
  },
};

const commands = {
  exec: async (command: string, cwd: string, options: { onData: (data: Buffer) => void; timeout?: number }) => {
    const answer = await operate("exec", { command, cwd, timeout: options.timeout ?? null });
    options.onData(Buffer.from(String(answer.output)));
    return { exitCode: answer.exit_code as number | null };
  },
};

function textOf(result: ToolResult): string {
  return result.content.map((part) => (part.type === "text" ? part.text : `[${part.type}]`)).join("\n");
}

export default function (pi: ExtensionAPI) {
  const cwd = process.cwd();
  const own = [
    createReadToolDefinition(cwd, { operations: files }),
    createWriteToolDefinition(cwd, { operations: files }),
    createEditToolDefinition(cwd, { operations: files }),
    createBashToolDefinition(cwd, { operations: commands }),
  ];
  for (const tool of own) {
    pi.registerTool({
      ...tool,
      async execute(id, params, signal, onUpdate, ctx) {
        await ask("/calls", { id, name: tool.name, args: params });
        let result: Awaited<ReturnType<typeof tool.execute>> | undefined;
        let error: Error | undefined;
        try {
          result = await tool.execute(id, params as never, signal, onUpdate as never, ctx);
        } catch (thrown) {
          error = thrown instanceof Error ? thrown : new Error(String(thrown));
        }
        const text = error ? error.message : textOf(result as ToolResult);
        await ask("/results", { id, text, is_error: error !== undefined });
        if (error) throw error;
        return result as never;
      },
    });
  }
  for (const { function: tool } of arenaTools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      promptSnippet: tool.description.split("\n")[0],
      parameters: { ...tool.parameters, type: "object", properties: tool.parameters.properties ?? {} } as never,
      async execute(id, params) {
        const answer = await ask("/calls", { id, name: tool.name, args: params });
        return { content: [{ type: "text", text: String(answer.text) }], details: {} };
      },
    });
  }
}
