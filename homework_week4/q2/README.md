# 第二题 · 基于 LlamaIndex 的「向量 + 关键词」混合检索 RAG

用 **真实 Embedding**（本地 HuggingFace `BAAI/bge-large-zh-v1.5`）与 **BM25 关键词检索**（jieba 中文分词）
在同一批节点上建两路索引，再用 **RRF（倒数排名融合）** 混合检索，最后把证据交给 **本地 llama.cpp**
真实模型生成「带来源引用」的回答。三步验证：建库 → 三路查询对比 → 更新资料后重查。

---

## 1. 题目与对应实现

| 步骤 | 要求 | 实现位置 |
| --- | --- | --- |
| ① 建立索引 | 读取登录资料并分块，基于同一批节点建向量索引 + BM25 关键词索引，用真实 Embedding，保存索引，输出节点数量及一条带来源的分块样例 | `run_demo.py::step1_build` |
| ② 查询对比 | 分别用向量 / BM25 / RRF 混合检索查询「登录会话的有效期是多少分钟？」，各输出前 5 条结果及来源；再把混合检索证据交给真实模型生成带来源引用的回答 | `run_demo.py::step2_query` |
| ③ 更新验证 | 在资料副本中把登录会话有效期改为 20 分钟，同步更新两路索引，确保旧分块退出当前检索；重新查询，输出回答及引用的新资料 | `run_demo.py::step3_update` |

---

## 2. 目录结构

```
q2/
├── login_docs/                 # 【只读初始资料】提交进仓库，有效期 30 分钟，演示绝不改它
│   ├── 登录与会话策略.md         #   主资料（含答案：登录会话有效期 30 分钟）
│   ├── 账号安全与风控.md         #   干扰资料（含「锁定 15 分钟」，不含「30 分钟」）
│   └── 常见问题FAQ.md           #   FAQ（含「默认有效期为 30 分钟」）
├── hybrid_rag/                 # 核心包
│   ├── config.py               #   路径 / 模型 / 检索超参 / 有效期常量（支持 .env 覆盖）
│   ├── tokenizer.py            #   jieba 中文分词器（BM25 的基础）
│   ├── materials.py            #   只读资料 → 工作副本；有效期精确改写
│   ├── embedder.py             #   本地 HuggingFace 真实 Embedding（bge-large-zh-v1.5）
│   ├── llm.py                  #   本地 llama.cpp（OpenAI 兼容）探活与构造
│   ├── indexing.py             #   读取分块 / 向量索引 / 保存加载（绕开 nltk）
│   ├── retrieval.py            #   向量 / BM25(jieba+Okapi) / RRF 三路检索
│   └── answering.py            #   把混合检索证据交给 LLM 生成带来源引用的回答
├── tests/
│   └── test_hybrid_rag.py      # pytest：分块/双索引/RRF/更新退出旧块（7 passed, 1 skipped）
├── run_demo.py                 # 三步编排入口（产出 artifacts + 干净 UTF-8 转录）
├── pytest.ini
├── .rag_workspace/             # 【运行生成】login_docs 的可变副本（第 3 步在此改写）
├── index_store/                # 【运行生成】持久化的向量索引 + docstore
└── artifacts/                  # 【运行生成】三步 JSON 产物 + console_transcript.txt
```

> **pristine → workspace 副本模式**（与第一题 `demo_project → .demo_workspace` 同构）：
> `login_docs/` 是只读初始资料；每次运行先整目录拷贝到 `.rag_workspace/login_docs/`，
> 第 3 步只在副本上改写。这样演示可反复运行、结果一致，且随时能对照原始资料。

---

## 3. 环境与依赖

已在仓库 `.venv`（Python 3.12）中安装并验证：

```
torch 2.15.0+cpu          llama-index-core 0.14.25
llama-index-embeddings-huggingface 0.8.0   sentence-transformers 6.2.0   transformers 5.22.1
llama-index-retrievers-bm25 0.8.0          llama-index-llms-openai-like 0.8.1
jieba 0.42.1              python-dotenv
```

安装命令（PowerShell 用 `;` 分隔）：

```powershell
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install llama-index-core llama-index-embeddings-huggingface `
    llama-index-retrievers-bm25 llama-index-llms-openai-like jieba python-dotenv
```

**Embedding 模型**：首次运行会经 `HF_ENDPOINT=https://hf-mirror.com`（`config.py` 已默认设置）
下载 `BAAI/bge-large-zh-v1.5`（约 1.3GB）到本地缓存，之后完全离线、可复现。
可预先下载并自检维度（应为 1024）：

```powershell
python -m hybrid_rag.embedder
```

---

## 4. 快速开始

在 `q2/` 目录下运行（建议先 `chcp 65001` 切 UTF-8）：

```powershell
cd homework_week4/q2
python run_demo.py
```

运行结束后：
- 控制台按「步骤 1 / 2 / 3」分三段打印，即三张截图的内容；
- `artifacts/console_transcript.txt` 是不受终端编码影响的干净 UTF-8 转录；
- `artifacts/step1_build.json`、`step2_query.json`、`step3_update.json` 是结构化产物；
- `index_store/` 是持久化的向量索引。

> **关于「生成回答」**：步骤 2/3 的「带来源回答」由**本地 llama.cpp**（`http://localhost:8080/v1`）生成。
> 若服务未启动，演示不会报错——检索对比照常展示，回答处打印占位提示，并说明需先启动 llama-server。
> 要得到真实回答，请先启动本地服务（见第 6 节），再重跑 `python run_demo.py`。

---

## 5. 三张截图对应位置

| 截图 | 内容 | 终端段落 / 产物 |
| --- | --- | --- |
| **截图 1 · 建库结果** | 节点数量（10）、每来源分块数、一条带来源的分块样例、索引保存路径与文件清单、Embedding 维度=1024 | 步骤 1 整段；或 `artifacts/step1_build.json` |
| **截图 2 · 三路查询对比与回答** | 向量 / BM25 / RRF 三路各 Top5 及来源，加上混合检索证据交给真实模型生成的带来源引用回答 | 步骤 2 整段；或 `artifacts/step2_query.json` |
| **截图 3 · 更新后的回答** | 有效期 30→20 的改写命中（2 文件 4 处）、旧分块退出（残留=0）、更新后 RRF Top5、更新后带新资料引用的回答、更新前 vs 更新后对比 | 步骤 3 整段；或 `artifacts/step3_update.json` |

截图操作：Windows 按 `Win + Shift + S` 框选终端对应段落即可；若终端中文显示异常，
可直接截 `artifacts/` 下对应的 JSON 产物或 `console_transcript.txt`。

---

## 6. 启动本地 llama.cpp（生成真实回答所需）

本项目回答生成只用**本地 llama.cpp**（OpenAI 兼容接口，无需 API Key）。启动一个 OpenAI 兼容服务并监听 `8080`：

```powershell
# 示例：用 llama-server 加载本地模型并开启 OpenAI 兼容 API
llama-server -m <你的模型.gguf> --host 127.0.0.1 --port 8080 -c 8192
```

- 端点 / 模型名可用环境变量覆盖（见 `.env` 或 `config.py`）：
  `LLAMA_BASE_URL`（默认 `http://localhost:8080/v1`）、`LLAMA_MODEL_NAME`。
- 探活：`python -m hybrid_rag.llm`（打印 `/v1/models` 摘要）。
- 服务就绪后重跑 `python run_demo.py`，步骤 2/3 即会输出真实的带来源回答。

---

## 7. 设计要点（为什么这么做）

- **真实 Embedding**：`HuggingFaceEmbedding(bge-large-zh-v1.5)`，1024 维中文向量；
  按 bge 官方建议对「查询」加指令前缀 `为这个句子生成表示以用于检索相关文章：`，对「文档」不加。
- **中文 BM25**：LlamaIndex 官方 `BM25Retriever` 底层是 `bm25s`，其分词面向英文
  （按 `\b\w+\b` 切词），对中文会把整句当成一个词、几乎无法命中。故自实现
  `JiebaBM25Retriever(BaseRetriever)`：**jieba 分词 + 经典 Okapi BM25 公式**，返回真实
  `NodeWithScore`，可无缝接入 `QueryFusionRetriever`。
- **RRF 融合**：`QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)`，
  `num_queries=1` 表示只用原始问题、不做 LLM 查询改写，纯做两路结果的倒数排名融合；
  构造期显式传入离线 `MockLLM` 占位，避免其解析默认 `Settings.llm` 时误连 OpenAI。
- **绕开 nltk**：`SentenceSplitter` 默认用 nltk punkt 分句，而 `nltk_data` 常托管在 GitHub
  （本机不可达）。故传 `chunking_tokenizer_fn=chinese_sentence_splitter`（按中文句末标点/换行切分）。
- **带来源引用**：每个节点保留 `metadata.source`（来源文件名），但用
  `excluded_embed_metadata_keys / excluded_llm_metadata_keys` 把来源类元数据排除在
  「被向量化 / 被 LLM 拼接」的正文之外，既不污染语义向量，又能在回答里逐句标注 `[n]` 来源。
- **更新退出旧块**：第 3 步在副本里把「30 分钟」精确改写为「20 分钟」，随后**重建两路索引**；
  通过「重建后全库仍含旧短语的节点数 = 0」证明旧分块已退出当前检索。

---

## 8. 测试

```powershell
cd homework_week4/q2
python -m pytest -q          # 7 passed, 1 skipped
```

覆盖：中文分词命中关键词、分块带来源与块序号、BM25 命中「有效期」块、
双索引 + RRF 融合、向量索引持久化往返、更新后旧短语退出/新短语进入、只读资料不被污染。

> 向量部分用离线 `MockEmbedding`，测试**不触发 1.3GB 真实模型下载**、秒级完成；
> 真实 Embedding 维度自检（`test_real_embedder_dim`）默认跳过，
> 需要时设 `RAG_TEST_REAL_EMBED=1` 再跑（会加载真实模型断言维度=1024）。
