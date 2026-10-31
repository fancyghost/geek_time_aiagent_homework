"""把四个记忆动作暴露成 pi（Agent）可调用的工具。

作业要求"可以由工具或课程插件提供，不强制使用特定工具名称"。这里给出标准的
OpenAI function-calling 工具定义 + 一个 dispatch 分发器：
  * MEMORY_TOOLS : 工具 schema 列表，可直接塞进 client.chat.completions.create(tools=...)
  * dispatch     : 收到 (tool_name, arguments) 后真正执行，返回可回灌给模型的结果

四个工具与四个动作一一对应：
  memory_write   -> 写入
  memory_find    -> 按项目查找并读取
  memory_update  -> 更新
  memory_delete  -> 删除

dispatch 全程只依赖磁盘上的 MemoryStore，不联网、不调用真实 LLM，
因此既能在真实 Agent Loop 里用，也能在离线 Demo / pytest 里确定性地驱动。
"""
from __future__ import annotations

from typing import Any

from .memory import MemoryNotFoundError, ProjectMemory
from .store import MemoryStore

# --------------------------------------------------------------------------- #
# 工具 schema（OpenAI function-calling 格式）
# --------------------------------------------------------------------------- #
MEMORY_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": "写入一条项目长期记忆（真正落盘）。用于把某条事实持久化保存。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "项目键，如 demo_project"},
                    "title": {"type": "string", "description": "记忆标题，如 会话测试入口"},
                    "content": {"type": "object", "description": "结构化正文，如 {test_path, run_command}"},
                    "source": {
                        "type": "array",
                        "description": "来源清单，每项 {path, note}",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "note": {"type": "string"},
                            },
                        },
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["project", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_find",
            "description": "按项目查找并读取长期记忆；query/tags 为空则返回该项目全部记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string", "description": "关键词，匹配标题/正文/标签"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_update",
            "description": "更新一条已有记忆（按 record_id 或 title 定位），revision 自增并落盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "record_id": {"type": "string", "description": "优先按 id 定位"},
                    "title": {"type": "string", "description": "无 id 时按标题定位"},
                    "new_title": {"type": "string"},
                    "content": {"type": "object", "description": "合并进旧 content"},
                    "source": {"type": "array", "items": {"type": "object"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "删除一条记忆（按 record_id 或 title 定位）并落盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "record_id": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["project"],
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# 分发执行
# --------------------------------------------------------------------------- #
def dispatch(store: MemoryStore, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行一次工具调用，返回 {"ok": bool, ...} 结构（可直接回灌给模型/打印）。"""
    args = dict(arguments or {})
    project = args.pop("project", None)
    if not project:
        return {"ok": False, "error": "缺少 project 参数"}

    mem = ProjectMemory(project, store)
    try:
        if tool_name == "memory_write":
            record, save_path = mem.write(
                title=args["title"],
                content=args.get("content"),
                source=args.get("source"),
                tags=args.get("tags"),
            )
            return {
                "ok": True,
                "action": "write",
                "record": record.to_dict(),
                "save_path": str(save_path),
            }

        if tool_name == "memory_find":
            records = mem.find_and_read(query=args.get("query"), tags=args.get("tags"))
            return {
                "ok": True,
                "action": "find",
                "count": len(records),
                "records": [r.to_dict() for r in records],
            }

        if tool_name == "memory_update":
            record, save_path = mem.update(
                record_id=args.get("record_id"),
                title=args.get("title"),
                new_title=args.get("new_title"),
                content=args.get("content"),
                source=args.get("source"),
                tags=args.get("tags"),
            )
            return {
                "ok": True,
                "action": "update",
                "record": record.to_dict(),
                "save_path": str(save_path),
            }

        if tool_name == "memory_delete":
            deleted = mem.delete(record_id=args.get("record_id"), title=args.get("title"))
            return {"ok": True, "action": "delete", "deleted": deleted}

        return {"ok": False, "error": f"未知工具: {tool_name}"}
    except MemoryNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except KeyError as exc:
        return {"ok": False, "error": f"缺少必填参数: {exc}"}
