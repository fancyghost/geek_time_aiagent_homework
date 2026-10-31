"""把混合检索到的证据交给本地 LLM，生成带来源引用的回答。

约束模型：只依据给定证据作答、逐句标注来源序号 [n]、证据不足时明确说明。
这样第 2/3 步的"回答"可追溯到具体资料分块，形成"引用"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PROMPT_TEMPLATE = """你是企业知识库问答助手。请只依据下面提供的「证据」回答问题，禁止使用证据之外的知识。
每条证据前有序号，回答中凡用到某条证据，请在相应句子末尾用方括号标注其序号（例如 [1] 或 [1][3]）。
如果证据不足以回答问题，请明确回答"资料中未找到相关信息"。

问题：{question}

证据：
{evidence}

要求：用简洁、准确的中文作答，先给出直接结论，再简要说明依据，并在引用处标注来源序号。
回答："""


@dataclass
class Evidence:
    idx: int
    source: str
    text: str

    def to_dict(self) -> dict:
        return {"idx": self.idx, "source": self.source, "text": self.text}


@dataclass
class AnswerResult:
    question: str
    answer: str
    citations: list[int] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": self.citations,
            "evidence": [e.to_dict() for e in self.evidence],
        }


def build_evidence_blocks(nodes_with_score) -> list[Evidence]:
    """把检索到的 NodeWithScore 列表转成带序号的证据块（来源取 metadata.source）。"""
    out: list[Evidence] = []
    for i, nws in enumerate(nodes_with_score, start=1):
        node = getattr(nws, "node", nws)
        meta = node.metadata or {}
        source = meta.get("source") or meta.get("file_name") or "未知来源"
        out.append(Evidence(idx=i, source=str(source), text=node.get_content().strip()))
    return out


def render_prompt(question: str, evidence: list[Evidence]) -> str:
    blocks = []
    for e in evidence:
        blocks.append(f"[{e.idx}] 来源：{e.source}\n{e.text}")
    return PROMPT_TEMPLATE.format(question=question, evidence="\n\n".join(blocks))


def _extract_citations(answer: str, max_idx: int) -> list[int]:
    """从回答里抽取 [n] 形式的引用序号（去重、按出现顺序、限定在证据范围内）。"""
    seen: list[int] = []
    for m in re.finditer(r"\[(\d+)\]", answer):
        n = int(m.group(1))
        if 1 <= n <= max_idx and n not in seen:
            seen.append(n)
    return seen


def generate_answer(llm, question: str, nodes_with_score) -> AnswerResult:
    """用本地 LLM 基于证据生成带来源引用的回答。

    走 chat 接口而非 completions：本地 llama.cpp 加载的多为 instruct/chat 模型，
    /v1/chat/completions 会自动套用对话模板，回答更稳定、更贴合"只依据证据作答"的指令。
    """
    from llama_index.core.llms import ChatMessage, MessageRole

    evidence = build_evidence_blocks(nodes_with_score)
    prompt = render_prompt(question, evidence)
    messages = [ChatMessage(role=MessageRole.USER, content=prompt)]
    resp = llm.chat(messages)
    msg = getattr(resp, "message", None)
    answer = (getattr(msg, "content", None) or getattr(resp, "text", None) or str(resp)).strip()
    citations = _extract_citations(answer, max_idx=len(evidence))
    return AnswerResult(question=question, answer=answer, citations=citations, evidence=evidence)
