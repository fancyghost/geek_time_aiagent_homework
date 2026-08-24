# db_agent 模型网关

统一抽象层的模型网关：通过**适配器模式**封装不同 API 协议，按请求中的 `model` 字段动态路由到对应适配器，屏蔽底层鉴权、请求体结构和返回格式的差异。基于 **FastAPI + Uvicorn** 构建。

## 功能总览

| 能力 | 说明 |
|---|---|
| 统一抽象层 | `POST /v1/invoke`，`model` 字段路由（`llama-local` / `deepseek-v4-flash` / `deepseek-v4-pro`） |
| 流式输出 | `stream=true`，SSE（`text/event-stream`）逐块返回，含首 Token 延迟 |
| 结构化输出 | `output_schema` 约束返回合法 JSON（chat=json_mode，responses=原生 json_schema） |
| 提示词版本管理 | 受控模板库：`template=name@v1/@latest` + `variables` 变量替换 |
| 可观测性 | 每次调用记录 Token 消耗（含 cached/reasoning 分类）与延迟（含 TTFT），SQLite 落盘 |
| 韧性基础 | 统一错误码 + 指数退避重试（0.5s→1s→2s，最多 3 次）+ 按模型独立限流（超限 429） |

## 架构

```
HTTP 层（FastAPI）                src/api/ + src/main.py
  POST /v1/invoke[/stream]       GET /v1/observability/{calls,summary}
  GET  /v1/templates  /v1/models  POST /v1/_debug/fault（故障注入，仅验证用）
        │
网关编排层                        src/gateway.py
  模板解析（版本+变量）→ 限流 → 模型路由 → 重试包装 → 观测落盘
        │
┌───────┼──────────────┬─────────────────┐
模板管理 src/prompts/   韧性 src/resilience/  可观测 src/observability/
 文件仓库（可切 DB）     错误码/重试/限流        SQLite call_records
        │
适配层                          src/adapter/
  LlamaCppChatAdapter（本地 OpenAI 兼容）
  DeepSeekChatAdapter（chat completions + responses 双接口）
  QoderChatAdapter（qoder-agent-sdk）
```

**防提示词注入约定**：HTTP 请求不接受自由文本系统提示词，只能通过 `template` 引用受控模板库（`prompts/` 目录登记）中审核过的模板；`variables` 仅允许填充模板预声明的 `{{var}}` 占位符。

## 目录结构

```
src/
├── main.py                  # FastAPI 入口
├── gateway.py               # 编排：模板→限流→路由→重试→观测
├── errors.py                # 统一错误码 GatewayError
├── config.py                # .env 配置
├── api/                     # schemas.py / routers.py
├── resilience/              # retry.py（指数退避）/ ratelimit.py（令牌桶）
├── prompts/                 # repository.py（存储抽象）/ registry.py（渲染）
├── observability/store.py   # SQLite 观测存储
└── adapter/                 # modelAdapter / dsAdapter / llamaAdapter / qoderAdapter
prompts/                     # 模板文件：<name>/v<N>.md + manifest.json
scripts/verify_all.py        # 六大功能验证脚本
```

## 启动指南

### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

### 2. 配置 .env

```ini
DEEPSEEK_API_KEY=sk-xxxx            # DeepSeek 云端凭据
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLAMA_BASE_URL=http://localhost:8080/v1
LLAMA_MODEL_NAME=Qwen3.8-27B-NVFP4-MTP-HIGHEST
# 限流（默认值偏低便于演示 429，生产调高）
RATE_LIMIT_RPS=2.0
RATE_LIMIT_BURST=2
```

### 3. 启动本地 llama.cpp server（验证 llama 模型时需要）

```powershell
llama-server -m <模型文件.gguf> --port 8080
```

### 4. 启动网关

```powershell
uvicorn src.main:app --port 8000
```

### 5. 运行验证脚本

```powershell
python scripts/verify_all.py
# 默认包含 DeepSeek 云端验证（消耗 token），跳过云端验证：
$env:SKIP_DEEPSEEK="1"; python scripts/verify_all.py
```

## curl 示例

### 1. 统一调用（model 路由）

```bash
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "用一句话介绍北京", "template": "general_chat"}'
```

### 2. 流式输出（SSE）

```bash
curl -N -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "用三句话介绍长城", "stream": true}'
# 逐块输出 data: {"type": "delta", "text": "..."}，
# 收尾 data: {"type": "done", "ttft_ms": ..., "latency_ms": ..., "usage": {...}}
```

### 3. 结构化输出

```bash
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-local",
    "input": "介绍北京",
    "template": "city_info_json",
    "output_schema": {
      "type": "object",
      "properties": {"city": {"type": "string"}, "is_capital": {"type": "boolean"}},
      "required": ["city", "is_capital"]
    }
  }'
# 响应 kind=structured，data 为符合 schema 的 JSON 对象
```

### 4. 模板版本引用 + 变量替换

```bash
# 指定 v1 版本
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "介绍你的输出规范", "template": "db_assistant@v1"}'

# latest（当前 v2）
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "介绍你的输出规范", "template": "db_assistant@latest"}'

# 变量替换（db_query 模板声明了 {{dialect}}）
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "分页查询怎么写？",
       "template": "db_query", "variables": {"dialect": "MySQL"}}'

# 模板清单
curl http://localhost:8000/v1/templates
```

### 5. 可观测数据

```bash
# 最近调用明细（Token 分类、延迟、TTFT、重试次数）
curl "http://localhost:8000/v1/observability/calls?limit=10"
# 按模型聚合（调用数、token 合计、avg/P95 延迟与 TTFT）
curl http://localhost:8000/v1/observability/summary
```

### 6. 重试与限流

```bash
# 注入 2 次瞬态故障，观察下一次调用的 retries=2（退避 0.5s+1.0s）
curl -X POST http://localhost:8000/v1/_debug/fault \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "fail_times": 2}'
curl -X POST http://localhost:8000/v1/invoke \
  -H "Content-Type: application/json" \
  -d '{"model": "llama-local", "input": "你好"}'

# 限流：连发 5 个请求（默认 2 RPS / burst 2），超限请求返回 429 LOCAL_RATE_LIMITED
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/invoke \
    -H "Content-Type: application/json" \
    -d '{"model": "llama-local", "input": "你好", "max_output_tokens": 8}'
done
```

## 统一错误码

| 错误码 | HTTP | 可重试 | 场景 |
|---|---|---|---|
| INVALID_REQUEST / TEMPLATE_VAR_MISSING | 400 | 否 | 参数/模板变量错误 |
| TEMPLATE_NOT_FOUND / MODEL_NOT_FOUND | 404 | 否 | 模板或模型未登记 |
| LOCAL_RATE_LIMITED | 429 | 否 | 网关本地限流 |
| UPSTREAM_TIMEOUT | 408 | 是 | 上游超时 |
| UPSTREAM_RATE_LIMITED | 503 | 是 | 上游限流 |
| UPSTREAM_AUTH / UPSTREAM_ERROR | 502 | 鉴权否/其他是 | 上游错误 |
| INTERNAL_ERROR | 500 | 否 | 网关内部错误 |

错误响应统一为 `{"error": {"code", "message", "detail"}}`。

## 新增模板 / 切换模板存储

- 新增模板：在 `prompts/<name>/v<N>.md` 写入正文，并在 `prompts/manifest.json` 登记 latest 与变量清单。
- 切换 DB 存储：实现 `src/prompts/repository.py` 中的 `TemplateRepository` 抽象接口，替换 `get_repository()` 装配点即可，其余模块无感知。
