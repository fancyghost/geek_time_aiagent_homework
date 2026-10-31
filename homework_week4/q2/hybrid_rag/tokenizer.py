"""BM25 中文分词器。

LlamaIndex 的 BM25Retriever 默认按空白切词，对中文几乎无效（一句话会被当成一个词）。
这里用 jieba 做中文分词，并做归一化：转小写、去标点/空白、过滤停用词与单字噪声，
让关键词检索对"登录/会话/有效期/分钟"等中文词真正可命中。
"""
from __future__ import annotations

import re
from functools import lru_cache

# 轻量中文停用词（够用即可，避免过度裁剪关键词）
_STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "在", "是", "为", "对", "以", "并", "被", "把",
    "本", "该", "其", "中", "上", "下", "个", "会", "要", "有", "无", "请", "你", "我",
    "他", "她", "它", "们", "这", "那", "就", "都", "而", "也", "还", "又", "吗", "呢",
    "啊", "吧", "等", "如", "若", "可", "能", "将", "由", "从", "到", "自", "至",
}

# 匹配中文、字母、数字的词；其余（标点/空白）作为分隔
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+")


@lru_cache(maxsize=1)
def _jieba():
    """惰性导入 jieba，避免未安装时影响其它模块的导入。"""
    import jieba  # type: ignore

    # 关闭 jieba 的默认日志噪声
    try:
        jieba.setLogLevel(60)
    except Exception:
        pass
    # 领域词，保证被当成一个词切分（提升 BM25 命中）
    for w in ("有效期", "会话", "登录", "令牌", "刷新令牌", "多因素认证", "并发会话", "滑动过期"):
        try:
            jieba.add_word(w)
        except Exception:
            pass
    return jieba


def tokenize(text: str) -> list[str]:
    """把一段文本切成用于 BM25 的词元列表（小写、去停用词/标点）。"""
    if not text:
        return []
    jieba_mod = _jieba()
    raw_tokens = jieba_mod.lcut(text)
    out: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip().lower()
        if not tok:
            continue
        # 只保留含中文/字母/数字的词元，丢弃纯标点与空白
        if not _TOKEN_RE.fullmatch(tok):
            # 混合词元（如 "30分钟"）再做一次内部抽取
            pieces = _TOKEN_RE.findall(tok)
            if not pieces:
                continue
            for p in pieces:
                p = p.lower()
                if p and p not in _STOPWORDS:
                    out.append(p)
            continue
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out
