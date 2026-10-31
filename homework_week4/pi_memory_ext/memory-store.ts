/**
 * memory-store.ts —— Python `pi_memory` 的零依赖 TypeScript 端口。
 *
 * 目标：让 pi（TypeScript harness）与作业里的 Python `pi_memory` 共享**同一份**磁盘记忆。
 * 为此，本文件严格复刻 Python 侧的落盘格式与四动作语义：
 *   - 存储布局：<store_root>/projects/<slug>.json
 *   - 顶层结构：{ project, project_path, count, records[] }
 *   - 记录字段顺序：id, project, title, content, source, tags, revision,
 *                   created_at, updated_at, created_at_iso, updated_at_iso
 *   - JSON：UTF-8、非 ASCII 原样、2 空格缩进、无尾换行；原子写（临时文件 + fsync + rename）
 *
 * 只依赖 Node 内置模块（node:fs / node:path / node:crypto），因此可脱离 pi 用 `node --test` 独立验证。
 */

import { randomBytes } from "node:crypto";
import {
	closeSync,
	existsSync,
	fsyncSync,
	mkdirSync,
	openSync,
	readFileSync,
	readdirSync,
	renameSync,
	rmSync,
	writeSync,
} from "node:fs";
import * as path from "node:path";

// --------------------------------------------------------------------------- //
// 类型别名
// --------------------------------------------------------------------------- //
export type ContentDict = Record<string, unknown>;
export interface SourceItem {
	path: string;
	note: string;
}
/** 落盘记录（snake_case，与 Python to_dict 完全一致） */
export interface RecordDict {
	id: string;
	project: string;
	title: string;
	content: ContentDict;
	source: SourceItem[];
	tags: string[];
	revision: number;
	created_at: number;
	updated_at: number;
	created_at_iso: string;
	updated_at_iso: string;
}
export interface ProjectMeta {
	project: string;
	project_path: string | null;
	count: number;
}
export interface DispatchResult {
	ok: boolean;
	action?: string;
	record?: RecordDict;
	records?: RecordDict[];
	count?: number;
	deleted?: boolean;
	save_path?: string;
	error?: string;
}

// --------------------------------------------------------------------------- //
// 时间与命名工具（对齐 Python record.py / store.py）
// --------------------------------------------------------------------------- //
/** 统一时间源：Unix 秒（浮点），对应 Python time.time()。 */
export function now(): number {
	return Date.now() / 1000;
}

/** 把 Unix 秒转成本地可读 ISO8601（YYYY-MM-DDTHH:MM:SS），对应 Python _iso()。 */
export function isoLocal(ts: number): string {
	const d = new Date(ts * 1000);
	const pad = (n: number) => String(n).padStart(2, "0");
	return (
		`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
		`T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
	);
}

/** 把项目键规整成安全文件名片段，对应 Python _slug()。 */
export function slug(project: string): string {
	const subbed = String(project)
		.trim()
		.replace(/[^0-9A-Za-z_.\-]+/g, "_");
	const stripped = subbed.replace(/^[._]+/, "").replace(/[._]+$/, "");
	return stripped || "default";
}

/** 模拟 Python str() 的取值字符串化，用于关键词匹配（string/number 完全一致，bool/null 对齐 True/False/None）。 */
function pyStr(v: unknown): string {
	if (typeof v === "string") return v;
	if (typeof v === "number") return String(v);
	if (typeof v === "boolean") return v ? "True" : "False";
	if (v === null || v === undefined) return "None";
	try {
		return JSON.stringify(v) ?? String(v);
	} catch {
		return String(v);
	}
}

/** 原子写 JSON：先写同目录临时文件并 fsync，再 rename 覆盖目标，对应 Python _atomic_write_json()。 */
export function atomicWriteJson(filePath: string, payload: unknown): void {
	const dir = path.dirname(filePath);
	mkdirSync(dir, { recursive: true });
	const tmp = path.join(
		dir,
		`.tmp_${process.pid}_${Date.now()}_${randomBytes(4).toString("hex")}.json`,
	);
	const text = JSON.stringify(payload, null, 2);
	const fd = openSync(tmp, "w");
	try {
		writeSync(fd, text, 0, "utf8");
		fsyncSync(fd);
		closeSync(fd);
		renameSync(tmp, filePath);
	} catch (err) {
		try {
			if (existsSync(tmp)) rmSync(tmp);
		} catch {
			// 忽略清理失败
		}
		throw err;
	}
}

/** 把来源统一成 [{path, note}] 结构，对应 Python _normalize_source()。 */
export function normalizeSource(source: unknown): SourceItem[] {
	if (!source || !Array.isArray(source)) return [];
	const out: SourceItem[] = [];
	for (const item of source) {
		if (typeof item === "string") {
			out.push({ path: item, note: "" });
		} else if (item && typeof item === "object") {
			const rec = item as Record<string, unknown>;
			out.push({ path: pyStr(rec.path ?? ""), note: pyStr(rec.note ?? "") });
		} else {
			out.push({ path: pyStr(item), note: "" });
		}
	}
	return out;
}

// --------------------------------------------------------------------------- //
// 异常（对应 Python MemoryNotFoundError / KeyError）
// --------------------------------------------------------------------------- //
/** 缺少必填参数（如 memory_write 的 title）。message 形如 `'title'`，对齐 Python KeyError 的展示。 */
export class KeyError extends Error {
	constructor(key: string) {
		super(`'${key}'`);
		this.name = "KeyError";
	}
}

/** 按 id / title 定位记忆但没找到时抛出。 */
export class MemoryNotFoundError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "MemoryNotFoundError";
	}
}

// --------------------------------------------------------------------------- //
// 记忆条目（对应 Python record.py: MemoryRecord）
// --------------------------------------------------------------------------- //
export interface MemoryRecordInit {
	project: string;
	title: string;
	content?: ContentDict;
	source?: SourceItem[];
	tags?: string[];
	id?: string;
	createdAt?: number;
	updatedAt?: number;
	revision?: number;
}

export class MemoryRecord {
	id: string;
	project: string;
	title: string;
	content: ContentDict;
	source: SourceItem[];
	tags: string[];
	createdAt: number;
	updatedAt: number;
	revision: number;

	constructor(init: MemoryRecordInit) {
		this.project = init.project;
		this.title = init.title;
		this.content = init.content ?? {};
		this.source = init.source ?? [];
		this.tags = init.tags ?? [];
		this.id = init.id ?? randomBytes(16).toString("hex"); // uuid4().hex 等价：32 位十六进制
		this.createdAt = init.createdAt ?? now();
		this.updatedAt = init.updatedAt ?? now();
		this.revision = init.revision ?? 1;
	}

	/** 序列化为落盘 dict，键顺序与 Python to_dict() 完全一致。 */
	toDict(): RecordDict {
		return {
			id: this.id,
			project: this.project,
			title: this.title,
			content: this.content,
			source: this.source,
			tags: this.tags,
			revision: this.revision,
			created_at: this.createdAt,
			updated_at: this.updatedAt,
			created_at_iso: isoLocal(this.createdAt),
			updated_at_iso: isoLocal(this.updatedAt),
		};
	}

	/** 从落盘 dict 还原，对应 Python from_dict()。 */
	static fromDict(data: Record<string, unknown>): MemoryRecord {
		return new MemoryRecord({
			id: data.id as string,
			project: data.project as string,
			title: data.title as string,
			content: { ...((data.content as ContentDict) ?? {}) },
			source: [...((data.source as SourceItem[]) ?? [])],
			tags: [...((data.tags as string[]) ?? [])],
			createdAt: (data.created_at as number) || now(),
			updatedAt: (data.updated_at as number) || now(),
			revision: (data.revision as number) || 1,
		});
	}

	/** 是否命中检索条件，对应 Python matches()：query 与 tags 同时给出时取「与」。 */
	matches(query?: string | null, tags?: string[] | null): boolean {
		let ok = true;
		if (tags && tags.length > 0) {
			const wanted = new Set(tags.map((t) => t.toLowerCase()));
			const owned = new Set(this.tags.map((t) => t.toLowerCase()));
			let hit = false;
			for (const w of wanted) {
				if (owned.has(w)) {
					hit = true;
					break;
				}
			}
			ok = ok && hit;
		}
		if (query) {
			const q = query.toLowerCase();
			const haystack: string[] = [this.title.toLowerCase(), this.tags.join(" ").toLowerCase()];
			for (const [k, v] of Object.entries(this.content)) {
				haystack.push(String(k).toLowerCase());
				haystack.push(pyStr(v).toLowerCase());
			}
			ok = ok && haystack.some((h) => h.includes(q));
		}
		return ok;
	}
}

// --------------------------------------------------------------------------- //
// 低层存储（对应 Python store.py: MemoryStore）
// --------------------------------------------------------------------------- //
export class MemoryStore {
	storeRoot: string;
	projectsDir: string;

	constructor(storeRoot: string) {
		this.storeRoot = path.resolve(storeRoot);
		this.projectsDir = path.join(this.storeRoot, "projects");
		mkdirSync(this.projectsDir, { recursive: true });
	}

	projectFile(project: string): string {
		return path.join(this.projectsDir, `${slug(project)}.json`);
	}

	/** 列出已建库的所有项目键。 */
	listProjects(): string[] {
		const out: string[] = [];
		if (!existsSync(this.projectsDir)) return out;
		const files = readdirSync(this.projectsDir)
			.filter((f) => f.endsWith(".json"))
			.sort();
		for (const f of files) {
			const data = this.readProjectFile(path.join(this.projectsDir, f));
			if (data) {
				out.push((data.project as string) || f.replace(/\.json$/, ""));
			}
		}
		return out;
	}

	readProjectFile(filePath: string): Record<string, unknown> | null {
		if (!existsSync(filePath)) return null;
		return JSON.parse(readFileSync(filePath, "utf8")) as Record<string, unknown>;
	}

	/** 读取某项目全部记忆，返回 { records, meta }，对应 Python read_project()。 */
	readProject(project: string): { records: MemoryRecord[]; meta: ProjectMeta } {
		const data = this.readProjectFile(this.projectFile(project));
		if (!data) {
			return { records: [], meta: { project, project_path: null, count: 0 } };
		}
		const rawRecords = (data.records as Record<string, unknown>[]) ?? [];
		const records = rawRecords.map((r) => MemoryRecord.fromDict(r));
		const meta: ProjectMeta = {
			project: (data.project as string) ?? project,
			project_path: (data.project_path as string | null) ?? null,
			count: records.length,
		};
		return { records, meta };
	}

	projectPath(project: string): string | null {
		return this.readProject(project).meta.project_path;
	}

	/** 把记忆里的路径解析成绝对路径：绝对原样，相对拼项目根（无项目根则拼 store_root）。 */
	resolve(project: string, relOrAbs: string): string {
		if (path.isAbsolute(relOrAbs)) return relOrAbs;
		const root = this.projectPath(project);
		const base = root ?? this.storeRoot;
		return path.resolve(base, relOrAbs);
	}

	/** 整体覆盖某项目记忆文件（原子），对应 Python _write_project()。 */
	private writeProject(
		project: string,
		records: MemoryRecord[],
		projectPath: string | null,
	): string {
		const payload = {
			project,
			project_path: projectPath ?? null,
			count: records.length,
			records: records.map((r) => r.toDict()),
		};
		const filePath = this.projectFile(project);
		atomicWriteJson(filePath, payload);
		return filePath;
	}

	/** 新增一条记忆并落盘，返回保存路径。 */
	saveRecord(record: MemoryRecord, projectPath?: string | null): string {
		const { records, meta } = this.readProject(record.project);
		const newRoot = projectPath ?? meta.project_path;
		records.push(record);
		return this.writeProject(record.project, records, newRoot);
	}

	/** 用同 id 的新版本替换旧记录（用于 update），否则追加。 */
	replaceRecord(record: MemoryRecord, projectPath?: string | null): string {
		const { records, meta } = this.readProject(record.project);
		const newRoot = projectPath ?? meta.project_path;
		let replaced = false;
		for (let i = 0; i < records.length; i++) {
			if (records[i].id === record.id) {
				records[i] = record;
				replaced = true;
				break;
			}
		}
		if (!replaced) records.push(record);
		return this.writeProject(record.project, records, newRoot);
	}

	/** 按 id 删除一条记忆并落盘，返回是否真的删掉。 */
	deleteRecord(project: string, recordId: string): boolean {
		const { records, meta } = this.readProject(project);
		const kept = records.filter((r) => r.id !== recordId);
		if (kept.length === records.length) return false;
		this.writeProject(project, kept, meta.project_path);
		return true;
	}

	/** 登记/更新项目根绝对路径（不改动已有记录）。 */
	setProjectPath(project: string, projectPath: string): void {
		const { records } = this.readProject(project);
		this.writeProject(project, records, path.resolve(projectPath));
	}

	/** 清空某项目记忆文件。 */
	clearProject(project: string): void {
		const filePath = this.projectFile(project);
		if (existsSync(filePath)) rmSync(filePath);
	}
}

// --------------------------------------------------------------------------- //
// 高层记忆 API（对应 Python memory.py: ProjectMemory）
// --------------------------------------------------------------------------- //
export interface WriteOptions {
	title: string;
	content?: ContentDict | null;
	source?: unknown;
	tags?: string[] | null;
}
export interface UpdateOptions {
	recordId?: string | null;
	title?: string | null;
	newTitle?: string | null;
	content?: ContentDict | null;
	source?: unknown;
	tags?: string[] | null;
}
export interface LocateOptions {
	recordId?: string | null;
	title?: string | null;
}

export class ProjectMemory {
	project: string;
	store: MemoryStore;

	constructor(project: string, store: MemoryStore, projectPath?: string | null) {
		this.project = project;
		this.store = store;
		if (projectPath != null) {
			store.setProjectPath(project, path.resolve(projectPath));
		}
	}

	/** 动作 1：写入一条新记忆并落盘，返回 { record, savePath }。 */
	write(opts: WriteOptions): { record: MemoryRecord; savePath: string } {
		const record = new MemoryRecord({
			project: this.project,
			title: opts.title,
			content: { ...(opts.content ?? {}) },
			source: normalizeSource(opts.source),
			tags: [...(opts.tags ?? [])],
		});
		const savePath = this.store.saveRecord(record);
		return { record, savePath };
	}

	/** 动作 2：读取本项目全部记忆。 */
	readAll(): MemoryRecord[] {
		return this.store.readProject(this.project).records;
	}

	/** 动作 2：按关键词/标签查找并读取（命中多条全部返回）。 */
	findAndRead(query?: string | null, tags?: string[] | null): MemoryRecord[] {
		return this.readAll().filter((r) => r.matches(query, tags));
	}

	/** 精确定位单条：优先 id，其次 title。找不到抛 MemoryNotFoundError。 */
	get(opts: LocateOptions): MemoryRecord {
		const records = this.readAll();
		if (opts.recordId != null) {
			for (const r of records) {
				if (r.id === opts.recordId) return r;
			}
			throw new MemoryNotFoundError(`项目 ${this.project} 中不存在 id=${opts.recordId} 的记忆`);
		}
		if (opts.title != null) {
			for (const r of records) {
				if (r.title === opts.title) return r;
			}
			throw new MemoryNotFoundError(`项目 ${this.project} 中不存在 title='${opts.title}' 的记忆`);
		}
		throw new Error("get() 需要 record_id 或 title 至少其一");
	}

	/** 动作 3：更新一条已有记忆并落盘（content 合并、revision+1、updated_at 刷新）。 */
	update(opts: UpdateOptions): { record: MemoryRecord; savePath: string } {
		const record = this.get({ recordId: opts.recordId, title: opts.title });
		if (opts.newTitle != null) record.title = opts.newTitle;
		if (opts.content != null) record.content = { ...record.content, ...opts.content };
		if (opts.source != null) record.source = normalizeSource(opts.source);
		if (opts.tags != null) record.tags = [...opts.tags];
		record.revision += 1;
		record.updatedAt = now();
		const savePath = this.store.replaceRecord(record);
		return { record, savePath };
	}

	/** 动作 4：删除一条记忆并落盘，返回是否真的删掉。 */
	delete(opts: LocateOptions): boolean {
		const record = this.get(opts);
		return this.store.deleteRecord(this.project, record.id);
	}

	/** 便捷：把记忆里的相对路径解析为绝对路径。 */
	resolvePath(relOrAbs: string): string {
		return this.store.resolve(this.project, relOrAbs);
	}
}

/** 便捷工厂，对应 Python new_memory()。默认 store_root="./memory_store"。 */
export function newMemory(
	project: string,
	storeRoot: string = "./memory_store",
	projectPath?: string | null,
): ProjectMemory {
	const store = new MemoryStore(storeRoot);
	return new ProjectMemory(project, store, projectPath);
}

// --------------------------------------------------------------------------- //
// 工具分发（对应 Python tools.py: dispatch）
// --------------------------------------------------------------------------- //
export const MEMORY_TOOL_NAMES = [
	"memory_write",
	"memory_find",
	"memory_update",
	"memory_delete",
] as const;

/**
 * 执行一次工具调用，返回 { ok, action, ... } 结构（可直接回灌给模型 / 打印）。
 * 与 Python dispatch 的输出字段完全一致：write/update 带 record + save_path，
 * find 带 count + records，delete 带 deleted。
 */
export function dispatch(
	store: MemoryStore,
	toolName: string,
	arguments_: Record<string, unknown>,
): DispatchResult {
	const args: Record<string, unknown> = { ...(arguments_ || {}) };
	const project = args.project as string | undefined;
	if (!project) {
		return { ok: false, error: "缺少 project 参数" };
	}

	const mem = new ProjectMemory(project, store);
	try {
		if (toolName === "memory_write") {
			if (args.title === undefined) throw new KeyError("title");
			const { record, savePath } = mem.write({
				title: args.title as string,
				content: args.content as ContentDict | undefined,
				source: args.source,
				tags: args.tags as string[] | undefined,
			});
			return { ok: true, action: "write", record: record.toDict(), save_path: savePath };
		}

		if (toolName === "memory_find") {
			const records = mem.findAndRead(
				args.query as string | undefined,
				args.tags as string[] | undefined,
			);
			return {
				ok: true,
				action: "find",
				count: records.length,
				records: records.map((r) => r.toDict()),
			};
		}

		if (toolName === "memory_update") {
			const { record, savePath } = mem.update({
				recordId: args.record_id as string | undefined,
				title: args.title as string | undefined,
				newTitle: args.new_title as string | undefined,
				content: args.content as ContentDict | undefined,
				source: args.source,
				tags: args.tags as string[] | undefined,
			});
			return { ok: true, action: "update", record: record.toDict(), save_path: savePath };
		}

		if (toolName === "memory_delete") {
			const deleted = mem.delete({
				recordId: args.record_id as string | undefined,
				title: args.title as string | undefined,
			});
			return { ok: true, action: "delete", deleted };
		}

		return { ok: false, error: `未知工具: ${toolName}` };
	} catch (err) {
		if (err instanceof MemoryNotFoundError) {
			return { ok: false, error: err.message };
		}
		if (err instanceof KeyError) {
			return { ok: false, error: `缺少必填参数: ${err.message}` };
		}
		throw err;
	}
}
