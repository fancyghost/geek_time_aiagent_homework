/**
 * memory-store.test.ts —— 用 Node 内置测试运行器验证 TS 核心。
 *
 * 覆盖三类关键性质：
 *   1) 四动作（写/查/改/删）真正落盘、跨实例持久；
 *   2) 磁盘格式与 Python pi_memory **完全一致**（顶层键序、记录键序、2 空格缩进、非 ASCII 原样、无尾换行）；
 *   3) 跨语言兼容：直接读 Python 侧真实产物 ../memory_store/projects/demo_project.json 并还原。
 *
 * 运行：在 pi_memory_ext 目录下 `node --test`（或 `npm test`）。
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import * as path from "node:path";
import { describe, it } from "node:test";

import {
	dispatch,
	isoLocal,
	MemoryNotFoundError,
	MemoryStore,
	ProjectMemory,
	slug,
} from "./memory-store.ts";

// 测试临时根目录：锚定到本文件旁，避免写到工作区之外（沙箱限制）。
const TMP_ROOT = path.join(import.meta.dirname, ".test-tmp");
rmSync(TMP_ROOT, { recursive: true, force: true }); // 运行前先清理上次残留，保证干净起点
mkdirSync(TMP_ROOT, { recursive: true });

function freshStore(): { store: MemoryStore; root: string } {
	const root = path.join(TMP_ROOT, `case_${Date.now()}_${Math.random().toString(36).slice(2)}`);
	mkdirSync(root, { recursive: true });
	return { store: new MemoryStore(root), root };
}

// Python 侧真实落盘产物（由 run_demo.py 生成），用于跨语言兼容校验。
const PY_STORE_ROOT = path.resolve(import.meta.dirname, "..", "memory_store");
const PY_DEMO_FILE = path.join(PY_STORE_ROOT, "projects", "demo_project.json");

// 进程正常退出时同步清理临时目录（比 node:test 顶层 after 更可靠）；
// maxRetries/retryDelay 应对 Windows 退出瞬间的目录句柄占用（EPERM/ENOTEMPTY）。
process.on("exit", () =>
	rmSync(TMP_ROOT, { recursive: true, force: true, maxRetries: 5, retryDelay: 20 }),
);

describe("工具函数与 Python 对齐", () => {
	it("slug 复刻 Python _slug 规整规则", () => {
		assert.equal(slug("demo_project"), "demo_project");
		assert.equal(slug("  spaced name  "), "spaced_name");
		assert.equal(slug("a/b\\c"), "a_b_c");
		assert.equal(slug("...__"), "default"); // 全被 strip 掉 -> default
		assert.equal(slug("keep.dot-dash_1"), "keep.dot-dash_1");
	});

	it("isoLocal 产出本地 YYYY-MM-DDTHH:MM:SS（无时区无毫秒）", () => {
		const s = isoLocal(1793427226.837599);
		assert.match(s, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
	});
});

describe("四动作真正落盘 + 跨实例持久", () => {
	it("write 落盘后，全新 MemoryStore 实例能读回（跨会话持久化）", () => {
		const { store, root } = freshStore();
		const mem = new ProjectMemory("demo_project", store, root);
		const { record, savePath } = mem.write({
			title: "会话测试入口",
			content: { test_path: "tests/test_session.py", case_count: 4 },
			source: [{ path: "tests/test_session.py", note: "测试文件" }],
			tags: ["session", "test"],
		});
		assert.ok(existsSync(savePath), "save_path 应真实存在");
		assert.equal(record.revision, 1);
		assert.equal(record.id.length, 32, "id 应为 32 位十六进制（uuid4().hex 等价）");

		// 全新实例，只依赖磁盘
		const store2 = new MemoryStore(root);
		const hits = new ProjectMemory("demo_project", store2).findAndRead("会话");
		assert.equal(hits.length, 1);
		assert.equal(hits[0].id, record.id);
		assert.equal(hits[0].content.test_path, "tests/test_session.py");
	});

	it("update 合并 content、revision+1、按 title 定位", () => {
		const { store, root } = freshStore();
		const mem = new ProjectMemory("demo_project", store, root);
		mem.write({ title: "会话测试入口", content: { test_path: "tests/test_session.py", case_count: 4 } });

		const { record } = mem.update({
			title: "会话测试入口",
			content: { test_path: "tests/auth/test_session.py", case_count: 5 },
			newTitle: "会话测试入口",
		});
		assert.equal(record.revision, 2);
		assert.equal(record.content.test_path, "tests/auth/test_session.py");
		assert.equal(record.content.case_count, 5);

		// 合并语义：旧键保留
		const mem2 = new ProjectMemory("demo_project", new MemoryStore(root));
		mem2.update({ title: "会话测试入口", content: { doc_path: "SESSION_TEST.md" } });
		const rec = mem2.get({ title: "会话测试入口" });
		assert.equal(rec.revision, 3);
		assert.equal(rec.content.test_path, "tests/auth/test_session.py", "旧键应保留");
		assert.equal(rec.content.doc_path, "SESSION_TEST.md", "新键应写入");
	});

	it("delete 删除后读不到；再删同一条抛 MemoryNotFoundError", () => {
		const { store } = freshStore();
		const mem = new ProjectMemory("demo_project", store);
		const { record } = mem.write({ title: "会话测试入口", content: { a: 1 } });
		assert.equal(mem.delete({ recordId: record.id }), true);
		assert.deepEqual(mem.readAll(), []);
		assert.throws(() => mem.delete({ recordId: record.id }), MemoryNotFoundError);
	});

	it("findAndRead 支持 tags 交集与 query 子串，两者同时给出取「与」", () => {
		const { store } = freshStore();
		const mem = new ProjectMemory("demo_project", store);
		mem.write({ title: "会话测试入口", content: { test_path: "a" }, tags: ["session"] });
		mem.write({ title: "部署命令", content: { cmd: "make deploy" }, tags: ["deploy"] });

		assert.equal(mem.findAndRead().length, 2, "无条件返回全部");
		assert.equal(mem.findAndRead(null, ["session"]).length, 1);
		assert.equal(mem.findAndRead("部署").length, 1);
		assert.equal(mem.findAndRead("部署", ["session"]).length, 0, "query 与 tags 取与 -> 空");
		assert.equal(mem.findAndRead("部署", ["deploy"]).length, 1);
	});

	it("resolve 把记忆里的相对路径解析为绝对路径（基于 project_path）", () => {
		const { store, root } = freshStore();
		const projRoot = path.join(root, "proj");
		mkdirSync(projRoot, { recursive: true });
		const mem = new ProjectMemory("demo_project", store, projRoot);
		mem.write({ title: "x", content: {} });
		assert.equal(mem.resolvePath("tests/a.py"), path.resolve(projRoot, "tests/a.py"));
		const abs = path.resolve(root, "elsewhere/b.py");
		assert.equal(mem.resolvePath(abs), abs, "绝对路径原样返回");
	});
});

describe("磁盘格式与 Python 完全一致", () => {
	it("顶层键序 / 记录键序 / 2 空格缩进 / 非 ASCII 原样 / 无尾换行", () => {
		const { store, root } = freshStore();
		const mem = new ProjectMemory("demo_project", store, root);
		mem.write({
			title: "会话测试入口",
			content: { test_path: "tests/test_session.py" },
			source: [{ path: "tests/test_session.py", note: "测试文件" }],
			tags: ["session"],
		});

		const file = path.join(root, "projects", "demo_project.json");
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
		assert.equal(parsed.count, 1);
		assert.ok(text.includes("\n  \"project\""), "应为 2 空格缩进");
		assert.ok(text.includes("会话测试入口"), "非 ASCII 应原样写入（ensure_ascii=False 等价）");
		assert.ok(!text.includes("\\u"), "不应出现 \\uXXXX 转义");
		assert.ok(!text.endsWith("\n"), "Python json.dump 无尾换行，TS 应一致");
	});
});

describe("跨语言兼容：读取 Python 真实产物", () => {
	it("能还原 Python run_demo 落盘的 demo_project.json", (t) => {
		if (!existsSync(PY_DEMO_FILE)) {
			t.skip(`未找到 Python 产物：${PY_DEMO_FILE}（先跑 run_demo.py 生成）`);
			return;
		}
		const store = new MemoryStore(PY_STORE_ROOT);
		const { records, meta } = store.readProject("demo_project");

		assert.equal(meta.count, records.length);
		assert.ok(records.length >= 1, "至少有一条记忆");

		const rec = records.find((r) => r.title === "会话测试入口") ?? records[0];
		assert.equal(rec.project, "demo_project");
		assert.equal(rec.id.length, 32);
		assert.equal(typeof rec.createdAt, "number");
		assert.match(rec.toDict().created_at_iso, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
		assert.ok(Array.isArray(rec.source));
		assert.ok(Array.isArray(rec.tags));

		// 与 Python 侧记录逐字段核对（demo 的已知状态）
		const raw = JSON.parse(readFileSync(PY_DEMO_FILE, "utf8"));
		const rawRec = raw.records.find((r: { title: string }) => r.title === "会话测试入口") ?? raw.records[0];
		assert.equal(rec.id, rawRec.id);
		assert.equal(rec.revision, rawRec.revision);
		assert.equal(rec.createdAt, rawRec.created_at);
		assert.equal(rec.toDict().created_at_iso, rawRec.created_at_iso);
		assert.deepEqual(rec.tags, rawRec.tags);
		assert.deepEqual(rec.content, rawRec.content);
	});

	it("ProjectMemory.get 可按 id 命中 Python 记录，二次 toDict 键序不变", (t) => {
		if (!existsSync(PY_DEMO_FILE)) {
			t.skip("未找到 Python 产物");
			return;
		}
		const store = new MemoryStore(PY_STORE_ROOT);
		const mem = new ProjectMemory("demo_project", store);
		const all = mem.readAll();
		const got = mem.get({ recordId: all[0].id });
		assert.equal(got.id, all[0].id);
		assert.deepEqual(Object.keys(got.toDict()), [
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
	});
});

describe("dispatch：四动作与错误分支（对齐 tools.py）", () => {
	it("write -> find -> update -> delete 全链路", () => {
		const { store } = freshStore();
		const w = dispatch(store, "memory_write", {
			project: "demo_project",
			title: "会话测试入口",
			content: { test_path: "tests/test_session.py" },
			tags: ["session"],
		});
		assert.equal(w.ok, true);
		assert.equal(w.action, "write");
		assert.ok(w.save_path && existsSync(w.save_path));
		const id = w.record!.id;

		const f = dispatch(store, "memory_find", { project: "demo_project", query: "会话" });
		assert.equal(f.ok, true);
		assert.equal(f.count, 1);
		assert.equal(f.records![0].id, id);

		const u = dispatch(store, "memory_update", {
			project: "demo_project",
			record_id: id,
			content: { test_path: "tests/auth/test_session.py" },
		});
		assert.equal(u.ok, true);
		assert.equal(u.record!.revision, 2);
		assert.equal(u.record!.content.test_path, "tests/auth/test_session.py");

		const d = dispatch(store, "memory_delete", { project: "demo_project", record_id: id });
		assert.equal(d.ok, true);
		assert.equal(d.deleted, true);

		const f2 = dispatch(store, "memory_find", { project: "demo_project" });
		assert.equal(f2.count, 0);
	});

	it("错误分支：缺 project / 缺 title / 记忆不存在 / 未知工具", () => {
		const { store } = freshStore();
		assert.deepEqual(dispatch(store, "memory_write", { title: "x" }), {
			ok: false,
			error: "缺少 project 参数",
		});
		const noTitle = dispatch(store, "memory_write", { project: "demo_project" });
		assert.equal(noTitle.ok, false);
		assert.equal(noTitle.error, "缺少必填参数: 'title'");

		const notFound = dispatch(store, "memory_update", { project: "demo_project", title: "不存在" });
		assert.equal(notFound.ok, false);
		assert.ok(notFound.error!.includes("不存在"));

		const unknown = dispatch(store, "memory_unknown", { project: "demo_project" });
		assert.equal(unknown.ok, false);
		assert.equal(unknown.error, "未知工具: memory_unknown");
	});
});
