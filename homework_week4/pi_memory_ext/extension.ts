/**
 * extension.ts —— 把本作业的 pi_memory 长期记忆「真正接入 pi」。
 *
 * 这是一个原生 pi 扩展：默认导出工厂函数 (pi) => void，向 pi 注册四个工具，
 * 与 Python pi_memory 的四个动作一一对应：
 *   memory_write  -> 写入
 *   memory_find   -> 按项目查找并读取
 *   memory_update -> 更新
 *   memory_delete -> 删除
 *
 * 关键点：这些工具底层调用 memory-store.ts（Python pi_memory 的 TS 端口），
 * 落盘格式与 Python 版**完全一致**，因此 pi 与 Python 脚本共享同一份记忆库
 * （默认 <cwd>/memory_store，与 Python new_memory 的默认 store_root 相同）。
 *
 * 加载方式（任选其一）：
 *   - 项目级自动发现：pi 在受信任项目里读取 .pi/extensions/*.ts（本仓已放置 shim）
 *   - 清单发现：本目录 package.json 的 "pi": {"extensions": ["./extension.ts"]}
 *   - 快速加载：`pi -e ./pi_memory_ext/extension.ts`
 *
 * 关于类型：pi 运行时会注入真实的 ExtensionAPI（来自 @earendil-works/pi-coding-agent）。
 * 这里用一份**最小本地接口**描述本扩展用到的子集，使扩展脱离 pi 单仓也能通过类型检查；
 * 结构已与 pi 真实 ToolDefinition / AgentToolResult 对照核对，运行时完全兼容。
 */

import * as path from "node:path";
import { Type } from "typebox";

import { dispatch, MemoryStore, type DispatchResult } from "./memory-store.ts";

// --------------------------------------------------------------------------- //
// 最小 ExtensionAPI 子集（运行时由 pi 注入真实对象）
// --------------------------------------------------------------------------- //
interface ToolTextContent {
	type: "text";
	text: string;
}
interface MemoryToolResult {
	content: ToolTextContent[];
	details: DispatchResult;
	isError?: boolean;
}
interface MemoryToolDefinition {
	name: string;
	label: string;
	description: string;
	promptSnippet?: string;
	promptGuidelines?: string[];
	parameters: unknown;
	execute: (
		toolCallId: string,
		params: Record<string, unknown>,
		signal?: unknown,
		onUpdate?: unknown,
		ctx?: unknown,
	) => Promise<MemoryToolResult>;
}
interface MemoryExtensionAPI {
	registerTool: (tool: MemoryToolDefinition) => void;
}

// --------------------------------------------------------------------------- //
// 参数 schema（typebox）——与 Python tools.py 的 MEMORY_TOOLS 一一对齐
// --------------------------------------------------------------------------- //
const SOURCE_SCHEMA = Type.Array(
	Type.Union([
		Type.String(),
		Type.Object({
			path: Type.String(),
			note: Type.Optional(Type.String()),
		}),
	]),
	{ description: "来源清单，每项 {path, note} 或直接给字符串路径" },
);
const CONTENT_SCHEMA = Type.Record(Type.String(), Type.Any(), {
	description: "结构化正文，如 {test_path, run_command}",
});
const TAGS_SCHEMA = Type.Array(Type.String(), { description: "关键词标签，辅助检索" });

const WRITE_PARAMS = Type.Object({
	project: Type.String({ description: "项目键，如 demo_project" }),
	title: Type.String({ description: "记忆标题，如 会话测试入口" }),
	content: CONTENT_SCHEMA,
	source: Type.Optional(SOURCE_SCHEMA),
	tags: Type.Optional(TAGS_SCHEMA),
});

const FIND_PARAMS = Type.Object({
	project: Type.String({ description: "项目键" }),
	query: Type.Optional(Type.String({ description: "关键词，匹配标题/正文/标签" })),
	tags: Type.Optional(TAGS_SCHEMA),
});

const UPDATE_PARAMS = Type.Object({
	project: Type.String({ description: "项目键" }),
	record_id: Type.Optional(Type.String({ description: "优先按 id 定位" })),
	title: Type.Optional(Type.String({ description: "无 id 时按标题定位" })),
	new_title: Type.Optional(Type.String({ description: "改写标题" })),
	content: Type.Optional(CONTENT_SCHEMA),
	source: Type.Optional(SOURCE_SCHEMA),
	tags: Type.Optional(TAGS_SCHEMA),
});

const DELETE_PARAMS = Type.Object({
	project: Type.String({ description: "项目键" }),
	record_id: Type.Optional(Type.String({ description: "优先按 id 定位" })),
	title: Type.Optional(Type.String({ description: "无 id 时按标题定位" })),
});

// --------------------------------------------------------------------------- //
// 工具构建（导出以便离线用 mock pi 测试）
// --------------------------------------------------------------------------- //
/** 把 dispatch 结果包装成 pi 的 AgentToolResult：content 给模型，details 给 UI/日志。 */
function toToolResult(result: DispatchResult): MemoryToolResult {
	return {
		content: [{ type: "text", text: JSON.stringify(result) }],
		details: result,
		isError: result.ok === false,
	};
}

/** 基于给定 store 构建四个 memory 工具定义。 */
export function buildMemoryTools(store: MemoryStore): MemoryToolDefinition[] {
	const run =
		(toolName: string) =>
		async (_toolCallId: string, params: Record<string, unknown>): Promise<MemoryToolResult> =>
			toToolResult(dispatch(store, toolName, params));

	return [
		{
			name: "memory_write",
			label: "Memory Write",
			description: "写入一条项目长期记忆（真正落盘）。用于把某条项目事实持久化保存。",
			promptSnippet: "memory_write: 持久化一条项目长期记忆到磁盘",
			promptGuidelines: [
				"当用户希望长期记住某个项目事实（如测试入口、部署命令、约定）时，调用 memory_write 落盘。",
				"project 用稳定的项目键；content 用结构化键值；可用 source 记录来源、tags 便于检索。",
			],
			parameters: WRITE_PARAMS,
			execute: run("memory_write"),
		},
		{
			name: "memory_find",
			label: "Memory Find",
			description: "按项目查找并读取长期记忆；query/tags 为空则返回该项目全部记忆。",
			promptSnippet: "memory_find: 按项目检索并读取长期记忆",
			promptGuidelines: [
				"回答涉及既有项目事实前，先用 memory_find 检索本项目记忆，避免臆测。",
			],
			parameters: FIND_PARAMS,
			execute: run("memory_find"),
		},
		{
			name: "memory_update",
			label: "Memory Update",
			description: "更新一条已有记忆（按 record_id 或 title 定位），content 合并、revision 自增并落盘。",
			promptSnippet: "memory_update: 更新既有记忆（revision 自增）",
			promptGuidelines: [
				"当既有事实发生变化（如测试文件迁移）时，用 memory_update 就地更新，而非重复写入。",
			],
			parameters: UPDATE_PARAMS,
			execute: run("memory_update"),
		},
		{
			name: "memory_delete",
			label: "Memory Delete",
			description: "删除一条记忆（按 record_id 或 title 定位）并落盘。",
			promptSnippet: "memory_delete: 删除一条长期记忆",
			parameters: DELETE_PARAMS,
			execute: run("memory_delete"),
		},
	];
}

/** 解析记忆库根目录：优先环境变量 PI_MEMORY_STORE，否则 <cwd>/memory_store（与 Python 默认一致）。 */
export function resolveStoreRoot(env: NodeJS.ProcessEnv = process.env, cwd: string = process.cwd()): string {
	return env.PI_MEMORY_STORE ?? path.resolve(cwd, "memory_store");
}

// --------------------------------------------------------------------------- //
// pi 扩展入口
// --------------------------------------------------------------------------- //
export default function memoryExtension(pi: MemoryExtensionAPI): void {
	const store = new MemoryStore(resolveStoreRoot());
	for (const tool of buildMemoryTools(store)) {
		pi.registerTool(tool);
	}
}
