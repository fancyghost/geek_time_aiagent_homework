/**
 * extension.test.ts —— 用 mock pi 验证扩展层：
 *   1) 默认工厂向 pi 注册四个 memory 工具（名称与 Python MEMORY_TOOLS 一致）；
 *   2) 每个工具 execute 走 dispatch，产出 pi AgentToolResult（content/details/isError）；
 *   3) 参数 schema 的 required 与 Python 对齐；
 *   4) 经工具写入的落盘文件与 Python 格式一致（可被 MemoryStore 直接读回）；
 *   5) resolveStoreRoot 的环境变量优先级。
 *
 * 运行：在 pi_memory_ext 目录下 `node --test`（或 `npm test`）。
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import * as path from "node:path";
import { describe, it } from "node:test";

import memoryExtension, { buildMemoryTools, resolveStoreRoot } from "./extension.ts";
import { MemoryStore } from "./memory-store.ts";

const TMP_ROOT = path.join(import.meta.dirname, ".test-tmp-ext");
rmSync(TMP_ROOT, { recursive: true, force: true }); // 运行前先清理上次残留，保证干净起点
mkdirSync(TMP_ROOT, { recursive: true });

function freshRoot(): string {
	const root = path.join(TMP_ROOT, `case_${Date.now()}_${Math.random().toString(36).slice(2)}`);
	mkdirSync(root, { recursive: true });
	return root;
}

interface MockTool {
	name: string;
	parameters: { required?: string[] };
	execute: (
		toolCallId: string,
		params: Record<string, unknown>,
	) => Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown>; isError?: boolean }>;
}

function makeMockPi(): { registerTool: (t: unknown) => void; tools: MockTool[] } {
	const tools: MockTool[] = [];
	return { registerTool: (t: unknown) => tools.push(t as MockTool), tools };
}

// 进程正常退出时同步清理临时目录（比 node:test 顶层 after 更可靠）；
// maxRetries/retryDelay 应对 Windows 退出瞬间的目录句柄占用（EPERM/ENOTEMPTY）。
process.on("exit", () =>
	rmSync(TMP_ROOT, { recursive: true, force: true, maxRetries: 5, retryDelay: 20 }),
);

describe("resolveStoreRoot 环境变量优先级", () => {
	it("有 PI_MEMORY_STORE 用它，否则 <cwd>/memory_store", () => {
		assert.equal(resolveStoreRoot({ PI_MEMORY_STORE: "/tmp/x" } as NodeJS.ProcessEnv, "/cwd"), "/tmp/x");
		assert.equal(resolveStoreRoot({} as NodeJS.ProcessEnv, "/cwd"), path.resolve("/cwd", "memory_store"));
	});
});

describe("默认工厂注册四个工具", () => {
	it("memory_write/find/update/delete 全部注册，名称与 Python 一致", () => {
		const root = freshRoot();
		const prev = process.env.PI_MEMORY_STORE;
		process.env.PI_MEMORY_STORE = root;
		try {
			const pi = makeMockPi();
			memoryExtension(pi);
			assert.deepEqual(
				pi.tools.map((t) => t.name).sort(),
				["memory_delete", "memory_find", "memory_update", "memory_write"],
			);
		} finally {
			if (prev === undefined) delete process.env.PI_MEMORY_STORE;
			else process.env.PI_MEMORY_STORE = prev;
		}
	});

	it("schema required 与 Python MEMORY_TOOLS 对齐", () => {
		const store = new MemoryStore(freshRoot());
		const tools = buildMemoryTools(store) as unknown as MockTool[];
		const byName = Object.fromEntries(tools.map((t) => [t.name, t]));
		assert.deepEqual(byName.memory_write.parameters.required, ["project", "title", "content"]);
		assert.deepEqual(byName.memory_find.parameters.required, ["project"]);
		assert.deepEqual(byName.memory_update.parameters.required, ["project"]);
		assert.deepEqual(byName.memory_delete.parameters.required, ["project"]);
	});
});

describe("工具 execute 全链路（经 dispatch）", () => {
	it("write -> find -> update -> delete，且写出的文件与 Python 格式一致", async () => {
		const root = freshRoot();
		const store = new MemoryStore(root);
		const tools = buildMemoryTools(store) as unknown as MockTool[];
		const byName = Object.fromEntries(tools.map((t) => [t.name, t]));

		// write
		const w = await byName.memory_write.execute("call_1", {
			project: "demo_project",
			title: "会话测试入口",
			content: { test_path: "tests/test_session.py", case_count: 4 },
			source: [{ path: "tests/test_session.py", note: "测试文件" }],
			tags: ["session", "test"],
		});
		assert.equal(w.isError, false);
		const wDetails = w.details as { ok: boolean; action: string; record: { id: string }; save_path: string };
		assert.equal(wDetails.ok, true);
		assert.equal(wDetails.action, "write");
		// content 是给模型的紧凑 JSON 文本
		assert.deepEqual(JSON.parse(w.content[0].text), w.details);

		// 落盘文件与 Python 格式一致：键序 + 非 ASCII 原样 + 无尾换行
		const file = path.join(root, "projects", "demo_project.json");
		assert.ok(existsSync(file));
		const text = readFileSync(file, "utf8");
		const parsed = JSON.parse(text);
		assert.deepEqual(Object.keys(parsed), ["project", "project_path", "count", "records"]);
		assert.deepEqual(Object.keys(parsed.records[0]), [
			"id",
			"project",
			"title",
			"content",
			"source",
			"tags",
			"revision",
			"created_at",
			"updated_at",
			"created_at_iso",
			"updated_at_iso",
		]);
		assert.ok(text.includes("会话测试入口"));
		assert.ok(!text.endsWith("\n"));

		const id = wDetails.record.id;

		// find
		const f = await byName.memory_find.execute("call_2", { project: "demo_project", query: "会话" });
		const fDetails = f.details as { ok: boolean; count: number; records: { id: string }[] };
		assert.equal(fDetails.ok, true);
		assert.equal(fDetails.count, 1);
		assert.equal(fDetails.records[0].id, id);

		// update
		const u = await byName.memory_update.execute("call_3", {
			project: "demo_project",
			record_id: id,
			content: { test_path: "tests/auth/test_session.py", case_count: 5 },
		});
		const uDetails = u.details as { ok: boolean; record: { revision: number; content: Record<string, unknown> } };
		assert.equal(uDetails.ok, true);
		assert.equal(uDetails.record.revision, 2);
		assert.equal(uDetails.record.content.test_path, "tests/auth/test_session.py");
		assert.equal(uDetails.record.content.case_count, 5);

		// delete
		const d = await byName.memory_delete.execute("call_4", { project: "demo_project", record_id: id });
		const dDetails = d.details as { ok: boolean; deleted: boolean };
		assert.equal(dDetails.ok, true);
		assert.equal(dDetails.deleted, true);

		const f2 = await byName.memory_find.execute("call_5", { project: "demo_project" });
		assert.equal((f2.details as { count: number }).count, 0);
	});

	it("错误分支：缺 project -> isError=true；未知记忆 -> error", async () => {
		const store = new MemoryStore(freshRoot());
		const tools = buildMemoryTools(store) as unknown as MockTool[];
		const byName = Object.fromEntries(tools.map((t) => [t.name, t]));

		const noProj = await byName.memory_write.execute("c", { title: "x" });
		assert.equal(noProj.isError, true);
		assert.equal((noProj.details as { error: string }).error, "缺少 project 参数");

		const notFound = await byName.memory_update.execute("c", { project: "demo_project", title: "不存在" });
		assert.equal(notFound.isError, true);
		assert.ok((notFound.details as { error: string }).error.includes("不存在"));
	});

	it("pi 与 Python 共享同一份磁盘记忆：工具写入可被 MemoryStore 读回", async () => {
		const root = freshRoot();
		const store = new MemoryStore(root);
		const tools = buildMemoryTools(store) as unknown as MockTool[];
		const byName = Object.fromEntries(tools.map((t) => [t.name, t]));

		await byName.memory_write.execute("c", {
			project: "shared_project",
			title: "共享记忆",
			content: { k: "v" },
			tags: ["shared"],
		});

		// 换一个全新的 MemoryStore 实例（模拟 Python 侧另起进程）读回
		const reread = new MemoryStore(root).readProject("shared_project");
		assert.equal(reread.meta.count, 1);
		assert.equal(reread.records[0].title, "共享记忆");
		assert.equal(reread.records[0].content.k, "v");
	});
});
