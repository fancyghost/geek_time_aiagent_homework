# homework_week4 · 第一题：为 pi 增加「项目长期记忆」

给 Agent **pi** 增加一层**项目长期记忆**：把"某个项目里值得长期记住的事实"（如会话测试入口、
运行命令、来源）真正写入磁盘，并支持四个动作——**写入 / 按项目查找并读取 / 更新 / 删除**。
记忆跨会话生效：关闭会话、开一个全新会话（独立进程）仍能从磁盘读回上一次写入的内容。

> 纯标准库实现，**不依赖向量数据库**；离线、确定、可重复运行。
> 四个动作以 **Agent 工具**（OpenAI function-calling schema + dispatch）的形式提供给 pi，
> 工具名不强制，这里命名为 `memory_write / memory_find / memory_update / memory_delete`。

---

## 1. 目录结构

```
homework_week4/
├── README.md                     # 本文件：设计 + 运行 + 三张截图指引
├── pytest.ini                    # 只收集 tests/，排除演示场景与产物目录
├── pi_memory/                    # ★「项目长期记忆」核心可运行代码
│   ├── __init__.py               #   对外导出 + new_memory 便捷工厂
│   ├── record.py                 #   MemoryRecord 记忆条目模型（含来源/标签/修订号）
│   ├── store.py                  #   MemoryStore：按项目分文件、原子落盘、跨会话持久化
│   ├── memory.py                 #   ProjectMemory：write / find_and_read / update / delete
│   └── tools.py                  #   把四个动作暴露成 pi 可调用的工具（schema + dispatch）
├── run_demo.py                   # ★ 一键编排三步验证，打印清晰输出并落盘 artifacts/
├── demo_project/                 # 只读的「当前项目」初始场景（演示永不破坏它）
│   ├── SESSION_TEST.md           #   测试说明
│   └── tests/test_session.py     #   会话测试文件（真实可跑 pytest，4 用例）
├── tests/
│   └── test_pi_memory.py         # 记忆系统 pytest（9 用例：四动作 + 跨会话 + 项目变化）
├── memory_store/                 # 【运行生成】记忆库（建库结果落这里）
│   └── projects/demo_project.json
├── artifacts/                    # 【运行生成】三步产物 + 干净 UTF-8 转录
│   ├── step1_write.json
│   ├── step2_query.json
│   ├── step3_update.json
│   └── console_transcript.txt
└── .demo_workspace/              # 【运行生成】demo_project 的可变副本（迁移在这里发生）
```

**为什么区分 `demo_project/` 与 `.demo_workspace/`？**
`demo_project/` 是仓库里提交的**只读初始场景**，演示绝不会改它；每次运行 `run_demo.py` 会先把它
整目录拷贝到 `.demo_workspace/demo_project/` 再在副本上做"迁移"。这样演示**可反复运行、结果一致**，
且随时能查看初始状态。

---

## 2. 记忆系统设计

### 2.1 记忆条目 `MemoryRecord`

| 字段 | 含义 |
|---|---|
| `id` | 记录唯一标识（uuid4），一经创建不变，用于精确定位更新/删除 |
| `project` | 所属项目键（如 `demo_project`），**"按项目查找"的依据** |
| `title` | 人类可读标题（如 `会话测试入口`） |
| `content` | 结构化正文，如 `{test_path, run_command, case_count, summary}` |
| `source` | **来源清单** `[{path, note}]`，回答"这条记忆从哪来" |
| `tags` | 关键词标签，辅助检索 |
| `created_at / updated_at` | 创建/更新时间（附带 ISO 可读串） |
| `revision` | 修订号，每次 `update` +1，用以证明"旧记录被真正改过" |

### 2.2 存储 `MemoryStore`（真正落盘）

- **按项目分文件**：`memory_store/projects/<project>.json`，天然支持"按项目查找并读取"。
- **原子写**：临时文件 `+ flush + fsync + os.replace`，磁盘上任何时刻都是完整 JSON，绝无半截文件。
- **跨会话持久化**：记忆只依赖磁盘文件；新建 `MemoryStore` 实例（甚至新进程）即可读回上个会话的写入。
- **路径解析**：记忆里存**项目相对路径**（如 `tests/test_session.py`）；`store.resolve(project, rel)`
  用项目根把它还原成绝对路径，供"核对当前文件是否存在"。
- **人类可读**：UTF-8 + `ensure_ascii=False` + `indent=2`，落盘文件可直接查看/截图。

### 2.3 四个动作（`ProjectMemory` 与工具一一对应）

| 动作 | ProjectMemory API | 对应工具 | 说明 |
|---|---|---|---|
| 写入 | `write(title, content, source, tags)` | `memory_write` | 新增一条记忆并落盘，返回 `(记录, 保存路径)` |
| 按项目查找并读取 | `find_and_read(query, tags)` / `read_all()` | `memory_find` | 项目内按关键词/标签检索；空条件返回全部 |
| 更新 | `update(record_id\|title, content=..., ...)` | `memory_update` | 定位旧记录，合并内容、`revision+1`、落盘 |
| 删除 | `delete(record_id\|title)` | `memory_delete` | 定位并移除，落盘，返回是否删成功 |

工具通过 `dispatch(store, tool_name, arguments)` 执行，返回 `{"ok": bool, ...}`，可直接回灌给模型。
因此既能接进真实 Agent Loop（把 `MEMORY_TOOLS` 传给 `chat.completions.create(tools=...)`），
也能在离线 Demo / pytest 里确定性驱动。

---

## 3. 如何运行

> 环境：Python 3.10+ 与 `pytest`（本机验证于 Python 3.12.8 / pytest 9.1.1）。
> Windows PowerShell 请用 `;` 分隔命令；**先执行 `chcp 65001` 让控制台以 UTF-8 显示中文**。
> （`run_demo.py` 内部也会尽力切到 UTF-8，并把整场输出同步写一份干净转录到
> `artifacts/console_transcript.txt`，即使终端编码异常也能查看。）

### 3.1 跑记忆系统测试（9 用例）

```powershell
cd "homework_week4"
python -m pytest tests/test_pi_memory.py -v
```

覆盖：写入落盘、按项目查找并读取、更新（revision 递增 + 内容合并）、删除、
跨会话（全新实例）持久化、项目变化后更新旧记录并核对存在性、工具 dispatch 一致性与错误分支。

### 3.2 跑三步验证演示

```powershell
cd "homework_week4"
chcp 65001
python run_demo.py
```

演示会先复位（重建 `.demo_workspace/`、清空 `memory_store/` 与 `artifacts/`），
再依次执行三步，并把每步结果写入 `artifacts/`。

---

## 4. 三步验证 → 演示输出对照

| 作业验证步骤 | 演示做了什么 | 输出位置 |
|---|---|---|
| **① 把记录真正写入持久化存储**：读当前项目的会话测试文件与测试说明，把测试路径与运行命令存入长期记忆（附来源），返回实际保存路径 | pi 读取 `demo_project/tests/test_session.py` 与 `SESSION_TEST.md`，调用 `memory_write` 写入 `{test_path, run_command, source...}`，打印**实际保存路径**与落盘 JSON | 控制台"步骤 1"；`artifacts/step1_write.json`；`memory_store/projects/demo_project.json` |
| **② 关闭会话→全新会话，验证读取并核对**：从长期记忆查找会话测试入口，再核对当前文件是否存在 | **另起独立子进程**（真·全新会话，只靠磁盘恢复），调用 `memory_find` 查到记忆，用 `resolve` 把相对路径还原为绝对路径并 `exists()` 核对，给出"记忆 vs 磁盘"对比与回答 | 控制台"步骤 2"；`artifacts/step2_query.json` |
| **③ 制造项目变化→更新旧记录**：把会话测试迁移到 `tests/auth/test_session.py`、同步改测试说明、跑新命令、按实际结果更新原记忆 | 在副本里迁移测试文件（新增角色用例，4→5）、改写 `SESSION_TEST.md`、**真跑** `python -m pytest tests/auth/test_session.py -q`（5 passed）、调用 `memory_update` 更新同一条记忆（revision 1→2），核对旧路径已不存在/新路径存在 | 控制台"步骤 3"；`artifacts/step3_update.json` |

---

## 5. 三张截图怎么截

三张截图分别对应演示的三个步骤。推荐直接截**控制台输出**（已切 UTF-8，中文正常）；
若终端字体/编码不理想，也可截 `artifacts/` 下对应的 JSON 文件（用 VS Code / 记事本以 UTF-8 打开）。

**截图 1 · 建库结果**
运行 `python run_demo.py` 后，截"步骤 1"这一段，要能看到：
- `[pi] 调用工具 memory_write(...)` 的调用轨迹；
- `save_path = ...\memory_store\projects\demo_project.json` 与 `exists = True`；
- 随后打印的落盘 JSON（含 `title / content.test_path / content.run_command / source`）。
（等价证据：`artifacts/step1_write.json` 或直接打开 `memory_store/projects/demo_project.json`。）

**截图 2 · 查询对比与回答**
截"步骤 2"这一段，要能看到：
- `启动全新会话（独立子进程…）` 与 `memory_find 命中 1 条记忆`；
- `---- 查询对比：记忆声称 vs 磁盘实际 ----` 那一行 `记忆: tests/test_session.py -> 磁盘存在: True => 一致: True`；
- `---- 全新会话的回答 ----` 下方给出的测试入口/运行命令/来源与"文件存在、记忆与实际一致"。
（等价证据：`artifacts/step2_query.json`。）

**截图 3 · 更新后的回答**
截"步骤 3"这一段，要能看到：
- `[项目变化] 迁移 tests/test_session.py -> tests/auth/test_session.py`、`旧路径存在=False 新路径存在=True`；
- `[pi] 测试结果: returncode=0 passed=True passed_count=5`；
- `---- 更新前 vs 更新后 ----` 的 `revision 1→2`、`test_path/run_command/case_count` 前后对比；
- `---- 更新后的回答 ----`。
（等价证据：`artifacts/step3_update.json`。）

> 小贴士：三步在一次 `python run_demo.py` 里连续输出，可整屏截一张长图后裁成三段；
> 也可分三次截。整场干净转录见 `artifacts/console_transcript.txt`。

---

## 6. 验收标准对照

| 要求 | 实现位置 | 佐证 |
|---|---|---|
| 记忆支持四个动作：写入/按项目查找并读取/更新/删除 | `pi_memory/memory.py`、`pi_memory/tools.py` | `tests/test_pi_memory.py`（9 用例全绿） |
| 可由工具或插件提供，不强制工具名 | `MEMORY_TOOLS` + `dispatch`（4 个工具） | 演示中 pi 全程通过工具调用记忆 |
| 不要求接入向量数据库 | 纯 JSON 文件存储（`store.py`） | 无任何向量库依赖 |
| 记录真正写入持久化存储、返回实际保存路径 | `store._atomic_write_json`；`write` 返回 `save_path` | 步骤 1 打印 save_path + 落盘 JSON |
| 关闭会话→全新会话可读回并核对 | 磁盘持久化 + `resolve/exists` 核对 | 步骤 2 **独立子进程**读回并核对 |
| 项目变化后按实际结果更新旧记录 | `update`（revision 递增）+ 真跑 pytest | 步骤 3 revision 1→2 + 5 passed |

---

## 7. 备注

- `demo_project/tests/test_session.py` 自带极简 `SessionStore` 实现，**自包含**，可独立跑通，不依赖项目其它模块。
- 演示对 pytest 的调用使用 `python -B -m pytest ... -o addopts= -p no:cacheprovider`：
  `-B` 不写字节码（规避"同秒覆写源码后子进程加载陈旧 .pyc"的陷阱）、`-o addopts=` 清空上层
  `pytest.ini` 可能注入的额外开关（避免 `-q` 叠加成 `-qq` 吞掉"N passed"汇总行）、`no:cacheprovider` 保持副本干净。
- `memory_store/`、`artifacts/`、`.demo_workspace/` 均为运行生成物，可随时删除，下次运行自动重建。
