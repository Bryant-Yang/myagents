import { createHash, randomBytes } from "node:crypto";
import {
	existsSync,
	lstatSync,
	readFileSync,
	realpathSync,
	statSync,
} from "node:fs";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import {
	createBashToolDefinition,
	createEditToolDefinition,
	createFindToolDefinition,
	createGrepToolDefinition,
	createLsToolDefinition,
	createReadToolDefinition,
	createWriteToolDefinition,
	type ExtensionAPI,
	type ExtensionContext,
	type ToolDefinition,
} from "@earendil-works/pi-coding-agent";

export const POLICY_VERSION = "myagents.pi.policy/v1";
export const POLICY_COMMAND = "myagents-policy-v1";
export const ATTESTATION_PREFIX = "MYAGENTS_PI_ATTEST_V1:";
export const PERMISSION_PREFIX = "MYAGENTS_PI_PERMISSION_V1:";
export const READY_STATUS_KEY = "myagents.pi.policy";
export const PERMIT_TTL_MS = 60_000;
export const PERMISSION_PREVIEW_MAX_BYTES = 4 * 1024;

export const WRAPPER_TOOL_NAMES = [
	"myagents_read",
	"myagents_grep",
	"myagents_find",
	"myagents_ls",
	"myagents_edit",
	"myagents_write",
	"myagents_bash",
] as const;

type WrapperToolName = (typeof WRAPPER_TOOL_NAMES)[number];
type Profile = "default" | "read_only" | "workspace_write";
type JsonRecord = Record<string, unknown>;

interface PathSnapshot {
	path: string;
	exists: boolean;
	anchorPath: string;
	anchorDev: number;
	anchorIno: number;
	anchorMode: number;
	anchorNlink: number;
	anchorSize: number;
	anchorMtimeMs: number;
}

interface CanonicalizedInput {
	input: JsonRecord;
	digest: string;
	path?: string;
	insideWorkspace?: boolean;
	snapshot?: PathSnapshot;
}

interface Permit {
	generation: number;
	toolCallId: string;
	toolName: WrapperToolName;
	digest: string;
	expiresAt: number;
	snapshot?: PathSnapshot;
}

interface PolicyEnvironment {
	nonce: string;
	expectedHash: string;
	expectedPath: string;
	profile?: Profile;
	expectedWorkspace: string;
	issues: string[];
}

const READ_TOOL_NAMES = WRAPPER_TOOL_NAMES.slice(0, 4);
const MUTATION_TOOL_NAMES = new Set<WrapperToolName>([
	"myagents_edit",
	"myagents_write",
]);
const SEARCH_TOOL_NAMES = new Set<WrapperToolName>(READ_TOOL_NAMES);
const WRAPPER_TOOL_NAME_SET = new Set<string>(WRAPPER_TOOL_NAMES);
const TOOL_INPUT_KEYS: Record<WrapperToolName, ReadonlySet<string>> = {
	myagents_read: new Set(["path", "offset", "limit"]),
	myagents_grep: new Set(["pattern", "path", "glob", "ignoreCase", "literal", "context", "limit"]),
	myagents_find: new Set(["pattern", "path", "limit"]),
	myagents_ls: new Set(["path", "limit"]),
	myagents_edit: new Set(["path", "edits"]),
	myagents_write: new Set(["path", "content"]),
	myagents_bash: new Set(["command", "timeout"]),
};

const SOURCE_PATH = realpathSync.native(fileURLToPath(import.meta.url));
const SOURCE_HASH = createHash("sha256")
	.update(readFileSync(SOURCE_PATH))
	.digest("hex");

function sha256(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

function jsonStringPreview(value: string, maxBytes: number): string {
	let bytes = 0;
	let result = "";
	for (const character of value) {
		const width = Buffer.byteLength(JSON.stringify(character), "utf8") - 2;
		if (bytes + width > maxBytes) break;
		result += character;
		bytes += width;
	}
	return result;
}

function stableJson(value: unknown): string {
	if (value === null || typeof value !== "object") {
		const encoded = JSON.stringify(value);
		if (encoded === undefined) throw new Error("unsupported undefined argument");
		return encoded;
	}
	if (Array.isArray(value)) {
		return `[${value.map((item) => stableJson(item ?? null)).join(",")}]`;
	}
	const object = value as JsonRecord;
	const keys = Object.keys(object)
		.filter((key) => object[key] !== undefined)
		.sort();
	return `{${keys
		.map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
		.join(",")}}`;
}

function encodePayload(payload: unknown): string {
	return Buffer.from(stableJson(payload), "utf8").toString("base64url");
}

function stableJsonByteLength(value: unknown): number {
	return Buffer.byteLength(stableJson(value), "utf8");
}

function displaySchemaError(toolName: WrapperToolName): Error {
	return new Error(`${toolName} input is outside the supported display schema`);
}

function assertStringField(
	toolName: WrapperToolName,
	input: JsonRecord,
	key: string,
	required = true,
): void {
	const value = input[key];
	if ((value === undefined && !required) || typeof value === "string") return;
	throw displaySchemaError(toolName);
}

function assertNumberField(toolName: WrapperToolName, input: JsonRecord, key: string): void {
	const value = input[key];
	if (value === undefined || (typeof value === "number" && Number.isFinite(value))) return;
	throw displaySchemaError(toolName);
}

function assertBooleanField(toolName: WrapperToolName, input: JsonRecord, key: string): void {
	const value = input[key];
	if (value === undefined || typeof value === "boolean") return;
	throw displaySchemaError(toolName);
}

function assertDisplaySchema(toolName: WrapperToolName, input: JsonRecord): void {
	const allowed = TOOL_INPUT_KEYS[toolName];
	for (const key of Object.keys(input)) {
		if (!allowed.has(key)) {
			throw displaySchemaError(toolName);
		}
	}

	assertStringField(toolName, input, "path", toolName !== "myagents_bash");
	if (toolName === "myagents_read") {
		assertNumberField(toolName, input, "offset");
		assertNumberField(toolName, input, "limit");
	} else if (toolName === "myagents_grep") {
		assertStringField(toolName, input, "pattern");
		assertStringField(toolName, input, "glob", false);
		assertBooleanField(toolName, input, "ignoreCase");
		assertBooleanField(toolName, input, "literal");
		assertNumberField(toolName, input, "context");
		assertNumberField(toolName, input, "limit");
	} else if (toolName === "myagents_find") {
		assertStringField(toolName, input, "pattern");
		assertNumberField(toolName, input, "limit");
	} else if (toolName === "myagents_ls") {
		assertNumberField(toolName, input, "limit");
	} else if (toolName === "myagents_write") {
		assertStringField(toolName, input, "content");
	} else if (toolName === "myagents_edit") {
		if (!Array.isArray(input.edits) || input.edits.length === 0) {
			throw displaySchemaError(toolName);
		}
		for (const value of input.edits) {
			if (!value || typeof value !== "object" || Array.isArray(value)) {
				throw displaySchemaError(toolName);
			}
			const edit = value as JsonRecord;
			if (
				Object.keys(edit).length !== 2 ||
				typeof edit.oldText !== "string" ||
				typeof edit.newText !== "string"
			) {
				throw displaySchemaError(toolName);
			}
		}
	} else {
		assertStringField(toolName, input, "command");
		assertNumberField(toolName, input, "timeout");
	}
}

function enforcePermissionDisplayBudget(input: JsonRecord): JsonRecord {
	if (stableJsonByteLength(input) > PERMISSION_PREVIEW_MAX_BYTES) {
		throw new Error(`permission input exceeds ${PERMISSION_PREVIEW_MAX_BYTES} UTF-8 bytes`);
	}
	return input;
}

function permissionDisplayInput(toolName: WrapperToolName, canonical: CanonicalizedInput): JsonRecord {
	assertDisplaySchema(toolName, canonical.input);
	const serialized = stableJson(canonical.input);
	const fullBytes = Buffer.byteLength(serialized, "utf8");
	if (fullBytes <= PERMISSION_PREVIEW_MAX_BYTES) {
		return enforcePermissionDisplayBudget(canonical.input);
	}

	const previewMetadata = {
		truncated: true,
		fullBytes,
		argsHash: canonical.digest,
	};
	let preview: JsonRecord;
	if (toolName === "myagents_write") {
		preview = {
			path: canonical.input.path,
			content: "",
			_myagentsPreview: previewMetadata,
		};
		enforcePermissionDisplayBudget(preview);
		const remaining = PERMISSION_PREVIEW_MAX_BYTES - stableJsonByteLength(preview);
		preview.content = jsonStringPreview(canonical.input.content as string, remaining);
	} else if (toolName === "myagents_edit") {
		const edits = canonical.input.edits as JsonRecord[];
		preview = {
			path: canonical.input.path,
			edits: edits.slice(0, 4).map(() => ({ oldText: "", newText: "" })),
			_myagentsPreview: previewMetadata,
		};
		enforcePermissionDisplayBudget(preview);
		const displayEdits = preview.edits as JsonRecord[];
		const fieldCount = displayEdits.length * 2;
		let fieldIndex = 0;
		for (let index = 0; index < displayEdits.length; index += 1) {
			for (const key of ["oldText", "newText"] as const) {
				const remaining = PERMISSION_PREVIEW_MAX_BYTES - stableJsonByteLength(preview);
				const remainingFields = fieldCount - fieldIndex;
				displayEdits[index][key] = jsonStringPreview(
					edits[index][key] as string,
					Math.floor(remaining / remainingFields),
				);
				fieldIndex += 1;
			}
		}
	} else if (toolName === "myagents_bash") {
		throw new Error("bash command is too large to display safely");
	} else {
		throw new Error(`${toolName} input is too large and cannot be truncated safely`);
	}
	return enforcePermissionDisplayBudget(preview);
}

function isProfile(value: string | undefined): value is Profile {
	return value === "default" || value === "read_only" || value === "workspace_write";
}

function canonicalExistingPath(value: string): string | undefined {
	try {
		return realpathSync.native(value);
	} catch {
		return undefined;
	}
}

function loadPolicyEnvironment(): PolicyEnvironment {
	const nonce = process.env.MYAGENTS_PI_POLICY_NONCE ?? "";
	const expectedHash = process.env.MYAGENTS_PI_POLICY_HASH ?? "";
	const rawExpectedPath = process.env.MYAGENTS_PI_POLICY_PATH ?? "";
	const rawProfile = process.env.MYAGENTS_PI_PROFILE;
	const rawWorkspace = process.env.MYAGENTS_PI_WORKSPACE ?? "";
	const issues: string[] = [];

	if (!/^[A-Za-z0-9_-]{16,256}$/.test(nonce)) {
		issues.push("invalid process nonce");
	}
	if (!/^[a-f0-9]{64}$/.test(expectedHash) || expectedHash !== SOURCE_HASH) {
		issues.push("policy hash mismatch");
	}
	const expectedPath = canonicalExistingPath(rawExpectedPath) ?? "";
	if (expectedPath !== SOURCE_PATH) {
		issues.push("policy path mismatch");
	}
	if (!isProfile(rawProfile)) {
		issues.push("invalid execution profile");
	}
	const expectedWorkspace = canonicalExistingPath(rawWorkspace) ?? "";
	if (!expectedWorkspace) {
		issues.push("invalid workspace");
	}

	return {
		nonce,
		expectedHash,
		expectedPath,
		profile: isProfile(rawProfile) ? rawProfile : undefined,
		expectedWorkspace,
		issues,
	};
}

function expectedActiveTools(profile: Profile | undefined): readonly string[] {
	return profile === "read_only" ? READ_TOOL_NAMES : WRAPPER_TOOL_NAMES;
}

function sameOrderedStrings(actual: readonly string[], expected: readonly string[]): boolean {
	return actual.length === expected.length && actual.every((item, index) => item === expected[index]);
}

function isInside(workspace: string, candidate: string): boolean {
	const rel = relative(workspace, candidate);
	return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`));
}

function ambiguousPathReason(rawPath: string): string | undefined {
	if (rawPath.includes("\0")) return "path contains NUL";
	if (rawPath.startsWith("~")) return "path uses ambiguous home expansion";
	if (/^file:\/\//i.test(rawPath)) return "path uses ambiguous file URL syntax";
	if (rawPath.startsWith("@")) return "path uses ambiguous attachment syntax";
	return undefined;
}

function nearestExistingPath(absolutePath: string): { anchor: string; tail: string[] } {
	let cursor = absolutePath;
	const tail: string[] = [];
	for (;;) {
		try {
			lstatSync(cursor);
			return { anchor: cursor, tail };
		} catch (error) {
			const code = (error as NodeJS.ErrnoException).code;
			if (code !== "ENOENT" && code !== "ENOTDIR") throw error;
			const parent = dirname(cursor);
			if (parent === cursor) throw new Error(`path has no existing anchor: ${absolutePath}`);
			tail.unshift(basename(cursor));
			cursor = parent;
		}
	}
}

function snapshotPath(canonicalPath: string): PathSnapshot {
	const exists = existsSync(canonicalPath);
	const anchor = exists ? canonicalPath : nearestExistingPath(canonicalPath).anchor;
	const stats = statSync(anchor);
	return {
		path: canonicalPath,
		exists,
		anchorPath: realpathSync.native(anchor),
		anchorDev: stats.dev,
		anchorIno: stats.ino,
		anchorMode: stats.mode,
		anchorNlink: stats.nlink,
		anchorSize: stats.size,
		anchorMtimeMs: stats.mtimeMs,
	};
}

function sameSnapshot(left: PathSnapshot | undefined, right: PathSnapshot | undefined): boolean {
	if (left === undefined || right === undefined) return left === right;
	return (
		left.path === right.path &&
		left.exists === right.exists &&
		left.anchorPath === right.anchorPath &&
		left.anchorDev === right.anchorDev &&
		left.anchorIno === right.anchorIno &&
		left.anchorMode === right.anchorMode &&
		left.anchorNlink === right.anchorNlink &&
		left.anchorSize === right.anchorSize &&
		left.anchorMtimeMs === right.anchorMtimeMs
	);
}

function canonicalizePath(rawPath: unknown, workspace: string): { path: string; inside: boolean; snapshot: PathSnapshot } {
	if (typeof rawPath !== "string" || rawPath.length === 0) {
		throw new Error("path must be a non-empty string");
	}
	const ambiguous = ambiguousPathReason(rawPath);
	if (ambiguous) throw new Error(`ambiguous path rejected: ${ambiguous}`);

	const absolute = resolve(workspace, rawPath);
	const { anchor, tail } = nearestExistingPath(absolute);
	let canonicalAnchor: string;
	try {
		canonicalAnchor = realpathSync.native(anchor);
	} catch {
		throw new Error(`ambiguous path rejected: broken symlink at ${anchor}`);
	}
	const path = resolve(canonicalAnchor, ...tail);
	return { path, inside: isInside(workspace, path), snapshot: snapshotPath(path) };
}

function hasGitSegment(workspace: string, path: string): boolean {
	if (!isInside(workspace, path)) return false;
	const rel = relative(workspace, path);
	return rel.split(sep).includes(".git");
}

function rejectUnsafeMutation(workspace: string, canonical: { path: string; inside: boolean; snapshot: PathSnapshot }): void {
	if (!canonical.inside) {
		throw new Error("mutation target is outside the workspace");
	}
	if (hasGitSegment(workspace, canonical.path)) {
		throw new Error("mutation of .git metadata is blocked");
	}
	if (canonical.snapshot.exists) {
		const stats = statSync(canonical.path);
		if (stats.isFile() && stats.nlink > 1) {
			throw new Error("mutation of a hard link is blocked");
		}
	}
}

function pathField(toolName: WrapperToolName): "path" | undefined {
	return toolName === "myagents_bash" ? undefined : "path";
}

function canonicalizeInput(
	toolName: WrapperToolName,
	rawInput: unknown,
	workspace: string,
): CanonicalizedInput {
	if (!rawInput || typeof rawInput !== "object" || Array.isArray(rawInput)) {
		throw new Error("tool input must be an object");
	}
	const input = { ...(rawInput as JsonRecord) };
	const field = pathField(toolName);
	let canonicalPath: ReturnType<typeof canonicalizePath> | undefined;

	if (field) {
		const optional = toolName === "myagents_grep" || toolName === "myagents_find" || toolName === "myagents_ls";
		canonicalPath = canonicalizePath(input[field] ?? (optional ? "." : undefined), workspace);
		if (MUTATION_TOOL_NAMES.has(toolName)) {
			rejectUnsafeMutation(workspace, canonicalPath);
		}
		input[field] = canonicalPath.path;
	} else {
		if (typeof input.command !== "string" || input.command.length === 0) {
			throw new Error("bash command must be a non-empty string");
		}
		if (input.command.includes("\0")) throw new Error("bash command contains NUL");
	}

	return {
		input,
		digest: sha256(stableJson(input)),
		path: canonicalPath?.path,
		insideWorkspace: canonicalPath?.inside,
		snapshot: canonicalPath?.snapshot,
	};
}

function copyInput(target: JsonRecord, source: JsonRecord): void {
	for (const key of Object.keys(target)) delete target[key];
	Object.assign(target, source);
}

function sanitizeSourceInfo(sourceInfo: JsonRecord): JsonRecord {
	const result: JsonRecord = {
		path: sourceInfo.path,
		source: sourceInfo.source,
		scope: sourceInfo.scope,
		origin: sourceInfo.origin,
	};
	if (sourceInfo.baseDir !== undefined) result.baseDir = sourceInfo.baseDir;
	return result;
}

function sourceInfoMatches(sourceInfo: JsonRecord): boolean {
	return typeof sourceInfo.path === "string" && canonicalExistingPath(sourceInfo.path) === SOURCE_PATH;
}

function isWrapperToolName(value: string): value is WrapperToolName {
	return WRAPPER_TOOL_NAME_SET.has(value);
}

export default function myagentsPermissionBridge(pi: ExtensionAPI): void {
	const environment = loadPolicyEnvironment();
	const workspace = environment.expectedWorkspace || realpathSync.native(process.cwd());
	const permits = new Map<string, Permit>();
	let ready = false;
	let generation = 0;

	const underlying = {
		myagents_read: createReadToolDefinition(workspace),
		myagents_grep: createGrepToolDefinition(workspace),
		myagents_find: createFindToolDefinition(workspace),
		myagents_ls: createLsToolDefinition(workspace),
		myagents_edit: createEditToolDefinition(workspace),
		myagents_write: createWriteToolDefinition(workspace),
		myagents_bash: createBashToolDefinition(workspace),
	} satisfies Record<WrapperToolName, ToolDefinition>;

	for (const toolName of WRAPPER_TOOL_NAMES) {
		const delegate = underlying[toolName];
		pi.registerTool({
			...delegate,
			name: toolName,
			label: toolName,
			async execute(toolCallId, params, signal, onUpdate, ctx) {
				if (!ready) throw new Error("Pi policy bridge is not ready");
				const permit = permits.get(toolCallId);
				permits.delete(toolCallId);
				if (!permit) throw new Error("missing one-time tool permit");
				if (permit.expiresAt < Date.now()) throw new Error("one-time tool permit expired");
				if (permit.generation !== generation) throw new Error("tool permit generation mismatch");
				if (permit.toolCallId !== toolCallId) throw new Error("tool permit call id mismatch");
				if (permit.toolName !== toolName) throw new Error("tool permit name mismatch");

				const canonical = canonicalizeInput(toolName, params, workspace);
				if (canonical.digest !== permit.digest) throw new Error("tool permit digest mismatch");
				if (!sameSnapshot(canonical.snapshot, permit.snapshot)) {
					throw new Error("tool path changed after permission was granted");
				}
				return delegate.execute(
					toolCallId,
					canonical.input as never,
					signal,
					onUpdate,
					ctx,
				);
			},
		});
	}

	pi.registerCommand(POLICY_COMMAND, {
		description: "Report the active myagents Pi permission policy bridge",
		handler: async (_args, ctx) => {
			ctx.ui.notify(
				ready
					? `${POLICY_VERSION} ready ${environment.expectedHash}`
					: `${POLICY_VERSION} unavailable`,
				ready ? "info" : "error",
			);
		},
	});

	pi.on("session_start", (_event, ctx) => {
		const currentGeneration = ++generation;
		ready = false;
		permits.clear();
		const activeTools = pi.getActiveTools();
		const expectedTools = expectedActiveTools(environment.profile);
		const allTools = new Map(pi.getAllTools().map((tool) => [tool.name, tool]));
		const tools = activeTools.map((name) => {
			const sourceInfo = allTools.get(name)?.sourceInfo as unknown as JsonRecord | undefined;
			return {
				name,
				sourceInfo: sourceInfo ? sanitizeSourceInfo(sourceInfo) : {},
			};
		});
		const contextWorkspace = canonicalExistingPath(ctx.cwd) ?? "";
		const processWorkspace = canonicalExistingPath(process.cwd()) ?? "";
		const issues = [...environment.issues];
		if (ctx.mode !== "rpc" || !ctx.hasUI) issues.push("RPC UI is unavailable");
		if (contextWorkspace !== workspace || processWorkspace !== workspace) {
			issues.push("working directory mismatch");
		}
		if (!sameOrderedStrings(activeTools, expectedTools)) {
			issues.push("active tool set mismatch");
		}
		for (const tool of tools) {
			if (!isWrapperToolName(tool.name) || !sourceInfoMatches(tool.sourceInfo)) {
				issues.push(`untrusted tool source: ${tool.name}`);
			}
		}

		const payload = {
			version: POLICY_VERSION,
			nonce: environment.nonce,
			policyHash: SOURCE_HASH,
			profile: environment.profile ?? process.env.MYAGENTS_PI_PROFILE ?? "",
			workspace: contextWorkspace || processWorkspace,
			activeTools,
			tools,
		};
		if (!ctx.hasUI) return;

		void (async () => {
			let choice: string | undefined;
			try {
				choice = await ctx.ui.select(
					`${ATTESTATION_PREFIX}${encodePayload(payload)}`,
					[`ack:${environment.nonce}`, `deny:${environment.nonce}`],
					{ timeout: 15_000 },
				);
			} catch {
				return;
			}
			if (currentGeneration !== generation) return;
			if (issues.length > 0 || choice !== `ack:${environment.nonce}`) return;

			ready = true;
			ctx.ui.setStatus(
				READY_STATUS_KEY,
				`ready:${environment.nonce}:${environment.expectedHash}`,
			);
		})();
	});

	pi.on("tool_call", async (event, ctx) => {
		const callGeneration = generation;
		for (const [id, permit] of permits) {
			if (permit.expiresAt < Date.now()) permits.delete(id);
		}
		permits.delete(event.toolCallId);

		if (!ready) {
			return { block: true, reason: "Pi policy bridge is not ready", terminate: true };
		}
		if (!isWrapperToolName(event.toolName)) {
			return { block: true, reason: "unmanaged tool blocked by Pi policy", terminate: true };
		}
		if (environment.profile === "read_only" && !SEARCH_TOOL_NAMES.has(event.toolName)) {
			return { block: true, reason: "tool is blocked in read_only profile", terminate: true };
		}

		let canonical: CanonicalizedInput;
		try {
			canonical = canonicalizeInput(event.toolName, event.input, workspace);
			copyInput(event.input as JsonRecord, canonical.input);
		} catch (error) {
			return {
				block: true,
				reason: error instanceof Error ? error.message : String(error),
				terminate: true,
			};
		}

		const grantPermit = (): void => {
			permits.set(event.toolCallId, {
				generation: callGeneration,
				toolCallId: event.toolCallId,
				toolName: event.toolName,
				digest: canonical.digest,
				expiresAt: Date.now() + PERMIT_TTL_MS,
				snapshot: canonical.snapshot,
			});
		};

		if (SEARCH_TOOL_NAMES.has(event.toolName) && canonical.insideWorkspace) {
			grantPermit();
			return undefined;
		}
		if (!ctx.hasUI) {
			return { block: true, reason: "permission UI is unavailable", terminate: true };
		}

		const callNonce = randomBytes(18).toString("base64url");
		let displayInput: JsonRecord;
		try {
			displayInput = permissionDisplayInput(event.toolName, canonical);
		} catch (error) {
			return {
				block: true,
				reason: error instanceof Error ? error.message : String(error),
				terminate: true,
			};
		}
		const permissionPayload = {
			version: POLICY_VERSION,
			processNonce: environment.nonce,
			callNonce,
			toolCallId: event.toolCallId,
			toolName: event.toolName,
			argsHash: canonical.digest,
			input: displayInput,
		};
		let choice: string | undefined;
		try {
			choice = await ctx.ui.select(
				`${PERMISSION_PREFIX}${encodePayload(permissionPayload)}`,
				[`allow_once:${callNonce}`, `reject_once:${callNonce}`],
				{ timeout: 120_000 },
			);
		} catch {
			return { block: true, reason: "permission request failed", terminate: true };
		}
		if (choice !== `allow_once:${callNonce}`) {
			return { block: true, reason: "permission was not granted", terminate: true };
		}
		if (callGeneration !== generation || !ready) {
			return { block: true, reason: "permission belongs to an old policy generation", terminate: true };
		}

		grantPermit();
		return undefined;
	});
}
