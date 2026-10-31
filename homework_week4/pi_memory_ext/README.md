# pi_memory_ext —— 把作业的 pi_memory 长期记忆「真正接入 pi」

这是一个**原生 pi 扩展**（TypeScript），把本作业 Python 版 `pi_memory` 的四个动作
（写入 / 按项目查找读取 / 更新 / 删除）注册成 pi 的工具，让 pi 在真实会话里能落盘、
读回、更新、删除项目长期记忆。

最关键的一点：扩展底层用 `memory-store.ts`（Python `pi_memory` 的**零依赖 TS 端口**）读写磁盘，
**落盘格式与 Python 版完全一致**，因此 pi 与 Python 脚本共享同一份记忆库
（默认 `<cwd>/memory_store`，与 Python `new_memory()` 的默认 `store_root="./memory_store"` 相同）。

---

## 目录结构

```
pi_memory_ext/
├── memory-store.ts        # 核心：Python pi_memory 的 TS 端口（零依赖，只用 node:fs/path/crypto）
├── extension.ts           # pi 扩展：注册 memory_write/find/update/delete 四个工具
├── memory-store.test.ts   # 核心测试（含读取 Python 真实产物 demo_project.json 的兼容校验）
├── extension.test.ts      # 扩展测试（用 mock pi 验证工具注册与 execute 全链路）
├── package.json           # "pi": {"extensions": ["./extension.ts"]} 清单 + 本地测试依赖
├── tsconfig.json          # 对齐 pi 的 TS 约定（Node16 / strict / erasableSyntaxOnly）
└── .gitignore

# 项目级自动发现入口（pi 在受信任项目里读取 .pi/extensions/*.ts）：
../.pi/extensions/memory.ts   # 一行转发：export { default } from "../../pi_memory_ext/extension.ts"
```

---

## 四个工具（与 Python 四动作一一对应）

| pi 工具名 | 动作 | 必填参数 | 可选参数 | 对应 Python |
|-----------|------|----------|----------|-------------|
| `memory_write`  | 写入 | `project, title, content` | `source, tags` | `ProjectMemory.write` |
| `memory_find`   | 按项目查找并读取 | `project` | `query, tags` | `ProjectMemory.find_and_read` |
| `memory_update` | 更新（content 合并、revision+1） | `project` | `record_id, title, new_title, content, source, tags` | `ProjectMemory.update` |
| `memory_delete` | 删除 | `project` | `record_id, title` | `ProjectMemory.delete` |

工具名、参数名、`required` 列表均与 Python `pi_memory/tools.py` 的 `MEMORY_TOOLS` 对齐；
`execute` 内部统一走 `dispatch(store, toolName, params)`，返回结构与 Python `dispatch` 一致
（`{ok, action, record/records/count/deleted, save_path}`），并映射为 pi 的 `AgentToolResult`
（`content` 给模型的紧凑 JSON 文本、`details` 给 UI/日志、失败时 `isError=true`）。

---

## 加载进 pi（任选其一）

1. **项目级自动发现**（推荐）：从 `homework_week4` 目录启动 pi。pi 在受信任项目里会自动加载
   `.pi/extensions/*.ts`，本仓已放置转发 shim `../.pi/extensions/memory.ts`。
2. **清单发现**：本目录 `package.json` 声明了 `"pi": {"extensions": ["./extension.ts"]}`。
3. **快速加载**：`pi -e ./pi_memory_ext/extension.ts`。

> pi 通过 jiti 加载扩展，TypeScript 无需编译。`typebox` 与 `@earendil-works/pi-*`
> 由 pi 运行时提供并映射到它自己的副本，因此**接入 pi 时无需 `npm install`**；
> 本目录的 `devDependencies` 仅供脱离 pi 的独立测试/类型检查使用。

### 记忆库根目录

- 默认：`<cwd>/memory_store`（与 Python 默认一致 → 天然共享同一份记忆）。
- 覆盖：设置环境变量 `PI_MEMORY_STORE=/abs/path/to/store`。

---

## 本地验证（不依赖 pi）

核心零依赖，可直接用 Node（v24 默认剥离 .ts 类型）运行：

```bash
cd pi_memory_ext
npm install        # 仅安装本地测试/类型检查用的 @types/node、typebox、typescript
npm test           # node --test  -> 18 项测试全绿
npm run typecheck  # tsc --noEmit -> strict + erasableSyntaxOnly 零诊断
```

测试覆盖：
- **四动作 + 跨实例持久**：写入后全新 `MemoryStore` 实例能读回；update 合并 content 且 revision+1；delete 后读不到。
- **磁盘格式与 Python 完全一致**：顶层键序 `project/project_path/count/records`、记录键序
  `id/project/title/content/source/tags/revision/created_at/updated_at/created_at_iso/updated_at_iso`、
  2 空格缩进、非 ASCII 原样（不转 `\uXXXX`）、无尾换行。
- **跨语言兼容**：直接读取 Python `run_demo.py` 落盘的真实产物 `../memory_store/projects/demo_project.json`
  并逐字段还原（id / revision / created_at / created_at_iso / tags / content 全部一致）。
- **扩展层**：mock pi 验证默认工厂注册四个工具、schema `required` 与 Python 对齐、execute 全链路。

### Python ↔ TS 双向回环（已实证）

除上面的「TS 读 Python 产物」外，还做了一次完整双向回环：

```
TS  dispatch(memory_write) 写入 xlang_probe  ──►  Python MemoryStore.read_project 读回并断言全字段一致
                                              └─►  Python ProjectMemory.update 就地更新（revision 1→2、content 合并）
                                              ──►  TS  MemoryStore.readProject 读回，确认 revision=2 且新旧键都在
```

三段全部通过（`PY-READ-TS-OK` / `PY-UPDATE-OK` / `TS-READ-PY-UPDATE-OK`），
证明 pi（TS）与 Python `pi_memory` 可安全地读写同一份磁盘记忆。

---

## 与 Python 版的文件级对应

| Python (`pi_memory/`) | TypeScript (`pi_memory_ext/`) | 说明 |
|-----------------------|-------------------------------|------|
| `record.py` `MemoryRecord` | `memory-store.ts` `MemoryRecord` | `to_dict/from_dict/matches` ↔ `toDict/fromDict/matches` |
| `store.py` `MemoryStore` / `_slug` / `_atomic_write_json` | `MemoryStore` / `slug` / `atomicWriteJson` | 按项目分文件、原子写 |
| `memory.py` `ProjectMemory` / `_normalize_source` | `ProjectMemory` / `normalizeSource` | 四动作高层 API |
| `tools.py` `MEMORY_TOOLS` / `dispatch` | `extension.ts` 工具 schema / `memory-store.ts` `dispatch` | 工具定义与分发 |
| `__init__.py` `new_memory` | `newMemory` | 便捷工厂 |

---

## 设计说明

- **零依赖核心**：`memory-store.ts` 只用 `node:fs/path/crypto`，因此能脱离 pi 用 `node --test` 独立、
  确定性地验证；所有落盘/检索/合并逻辑都在这里，`extension.ts` 只是薄适配层。
- **erasable-only TS**：不使用 enum / namespace / 构造器参数属性，相对导入显式带 `.ts` 扩展名，
  与 pi 的 `tsconfig`（`erasableSyntaxOnly`、`allowImportingTsExtensions`、Node16 解析）一致，
  Node v24 可直接剥离类型运行。
- **最小 `ExtensionAPI` 接口**：`extension.ts` 只用到 `pi.registerTool`，故本地声明一份最小接口，
  使扩展脱离 pi 单仓也能通过类型检查；运行时由 pi 注入真实对象，结构已对照 pi 的
  `ToolDefinition` / `AgentToolResult` 核对，完全兼容。

### 与 Python 的细微差异（不影响磁盘兼容）

- **时间戳精度**：Python `time.time()` 为微秒；TS `Date.now()/1000` 为毫秒。两者都是「Unix 秒浮点数」，
  JSON 数字互相可读，跨语言一致。
- **错误字符串**：Python `MemoryNotFoundError` 继承自 `KeyError`，`str(exc)` 会带一层引号；TS 端给出
  更干净的消息（`缺少必填参数: 'title'` 这类关键分支仍与 Python 完全一致）。错误串是即时返回值、
  不落盘，不影响共享记忆。
- **关键词匹配的极端情况**：`matches` 对 content 的**字符串/数字**取值与 Python `str()` 完全一致；
  仅当对**深层嵌套对象**做子串匹配时，两侧字符串化方式不同（Python 用 repr、TS 用 JSON），
  属罕见边界，常规标题/标签/字符串正文匹配不受影响。
