import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { renameSync, writeFileSync } from "node:fs";

const [, , extensionPath, encodedConfig] = process.argv;
const config = JSON.parse(Buffer.from(encodedConfig, "base64url").toString("utf8"));

globalThis.__myagentsDelegateCalls = [];

const fakePackage = `
function makeDefinition(name, cwd) {
  return {
    name,
    label: name,
    description: name + " fake definition",
    promptSnippet: name + " fake prompt",
    promptGuidelines: [name + " fake guideline"],
    parameters: { type: "object" },
    prepareArguments(value) { return value; },
    async execute(toolCallId, input) {
      globalThis.__myagentsDelegateCalls.push({ toolCallId, name, input, cwd });
      return { content: [{ type: "text", text: name + " delegated" }] };
    },
  };
}
export const createReadToolDefinition = (cwd) => makeDefinition("read", cwd);
export const createGrepToolDefinition = (cwd) => makeDefinition("grep", cwd);
export const createFindToolDefinition = (cwd) => makeDefinition("find", cwd);
export const createLsToolDefinition = (cwd) => makeDefinition("ls", cwd);
export const createEditToolDefinition = (cwd) => makeDefinition("edit", cwd);
export const createWriteToolDefinition = (cwd) => makeDefinition("write", cwd);
export const createBashToolDefinition = (cwd) => makeDefinition("bash", cwd);
`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@earendil-works/pi-coding-agent") {
      return {
        url: `data:text/javascript;base64,${Buffer.from(fakePackage).toString("base64")}`,
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});

process.chdir(config.workspace);
for (const [key, value] of Object.entries(config.env)) {
  if (value === null) delete process.env[key];
  else process.env[key] = value;
}

let fakeNow = 1_000_000;
Date.now = () => fakeNow;

const eventHandlers = new Map();
const tools = new Map();
const commands = new Map();
const selectCalls = [];
const statusCalls = [];
const permissionChoices = [...(config.permissionChoices ?? [])];
const pendingAttestations = [];
const sessionStarts = [];

const sourceInfoFor = (name) => ({
  path:
    config.badSourceTool === name
      ? config.outsidePath
      : extensionPath,
  source: "extension",
  scope: "temporary",
  origin: "top-level",
});

const pi = {
  on(event, handler) {
    const handlers = eventHandlers.get(event) ?? [];
    handlers.push(handler);
    eventHandlers.set(event, handlers);
  },
  registerTool(tool) {
    tools.set(tool.name, tool);
  },
  registerCommand(name, options) {
    commands.set(name, options);
  },
  getActiveTools() {
    return [...config.activeTools];
  },
  getAllTools() {
    return [...tools.values()].map((tool) => ({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      promptGuidelines: tool.promptGuidelines,
      sourceInfo: sourceInfoFor(tool.name),
    }));
  },
};

const context = {
  cwd: config.contextCwd ?? config.workspace,
  mode: "rpc",
  hasUI: config.hasUI ?? true,
  ui: {
    async select(title, options, opts) {
      selectCalls.push({ title, options, opts });
      if (title.startsWith("MYAGENTS_PI_ATTEST_V1:")) {
        if (config.deferAttestations) {
          return new Promise((resolve) => {
            pendingAttestations.push({ resolve, options });
          });
        }
        return config.attestationChoice;
      }
      if (title.startsWith("MYAGENTS_PI_PERMISSION_V1:")) {
        const choice = permissionChoices.shift();
        if (choice === "$ALLOW") return options[0];
        if (choice === "$REJECT") return options[1];
        return choice;
      }
      throw new Error(`unexpected select title: ${title}`);
    },
    setStatus(key, text) {
      statusCalls.push({ key, text });
    },
    notify() {},
  },
};

const output = {
  importError: null,
  sessionError: null,
  tools: [],
  commands: [],
  selectCalls,
  statusCalls,
  operations: [],
  delegateCalls: globalThis.__myagentsDelegateCalls,
  sessionStarts,
};

async function flushBackgroundTasks() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function invokeSessionStart(reason = "startup") {
  for (const handler of eventHandlers.get("session_start") ?? []) {
    const returned = await Promise.race([
      Promise.resolve(handler({ type: "session_start", reason }, context))
        .then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 25)),
    ]);
    sessionStarts.push({ reason, returned });
  }
  await flushBackgroundTasks();
}

try {
  const extension = await import(`${pathToFileURL(extensionPath).href}?case=${config.caseId}`);
  await extension.default(pi);
} catch (error) {
  output.importError = error instanceof Error ? error.message : String(error);
}

if (!output.importError) {
  output.tools = [...tools.keys()];
  output.commands = [...commands.keys()];
  try {
    await invokeSessionStart("startup");
  } catch (error) {
    output.sessionError = error instanceof Error ? error.message : String(error);
  }

  for (const operation of config.operations ?? []) {
    if (operation.kind === "sessionStart") {
      await invokeSessionStart(operation.reason ?? "reload");
      output.operations.push({ kind: "sessionStart" });
      continue;
    }

    if (operation.kind === "resolveAttestation") {
      const pending = pendingAttestations[operation.index];
      if (!pending) throw new Error(`missing attestation ${operation.index}`);
      const choice =
        operation.choice === "$ACK"
          ? pending.options[0]
          : operation.choice === "$DENY"
            ? pending.options[1]
            : operation.choice;
      pending.resolve(choice);
      await flushBackgroundTasks();
      output.operations.push({ kind: "resolveAttestation", index: operation.index });
      continue;
    }

    if (operation.kind === "advance") {
      fakeNow += operation.ms;
      output.operations.push({ kind: "advance", now: fakeNow });
      continue;
    }

    if (operation.kind === "replaceFile") {
      const replacement = `${operation.path}.replacement`;
      writeFileSync(replacement, operation.content, "utf8");
      renameSync(replacement, operation.path);
      output.operations.push({ kind: "replaceFile", path: operation.path });
      continue;
    }

    if (operation.kind === "call") {
      const event = {
        type: "tool_call",
        toolCallId: operation.toolCallId,
        toolName: operation.toolName,
        input: structuredClone(operation.input),
      };
      try {
        let result;
        for (const handler of eventHandlers.get("tool_call") ?? []) {
          const candidate = await handler(event, context);
          if (candidate !== undefined) result = candidate;
          if (candidate?.block) break;
        }
        output.operations.push({
          kind: "call",
          result: result ?? null,
          input: event.input,
        });
      } catch (error) {
        output.operations.push({
          kind: "call",
          error: error instanceof Error ? error.message : String(error),
          input: event.input,
        });
      }
      continue;
    }

    if (operation.kind === "execute") {
      const tool = tools.get(operation.toolName);
      try {
        const result = await tool.execute(
          operation.toolCallId,
          structuredClone(operation.input),
          undefined,
          undefined,
          context,
        );
        output.operations.push({ kind: "execute", result });
      } catch (error) {
        output.operations.push({
          kind: "execute",
          error: error instanceof Error ? error.message : String(error),
        });
      }
      continue;
    }

    throw new Error(`unknown operation: ${operation.kind}`);
  }
}

process.stdout.write(JSON.stringify(output));
