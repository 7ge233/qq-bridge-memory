# -*- coding: utf-8 -*-
"""文本处理：CQ 码清洗、敏感脱敏、jieba 分词、规则打标辅助（4.1 / 4.1.3 / 4.1.4 / 4.2）。

设计原则：
  * 纯函数优先，方便单测；
  * 脱敏正则与 qq-bridge 的 SENSITIVE_RE 语义一致（路径 / token / Authorization），
    并补手机号、身份证、银行卡；
  * 记忆内容入库前必须过 clean_text + redact，任何地方都不得回写原文到日志。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections import Counter

import jieba
import jieba.posseg as pseg

logger = logging.getLogger("qq_memory.text")

# ---------------------------------------------------------------------------
# 清洗与脱敏
# ---------------------------------------------------------------------------

_RE_CQ = re.compile(r"\[CQ:[^\]]*\]")
_RE_PLACEHOLDER = re.compile(r"\[(?:图片|表情|语音|视频|文件|链接|转发消息[^\]]*)\]")
_RE_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_RE_SPACE = re.compile(r"[ \t\u3000]+")
_RE_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
_RE_INJECTION = re.compile(r"^(?:忽略|无视|你现在是|从现在开始)")

# 顺序有讲究：先长路径/token，再号码类，避免被短模式抢先切碎。
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows 绝对路径
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+"),
    # UNC 路径
    re.compile(r"\\\\[^\\\s\"'<>|]+\\[^\s\"'<>|]+"),
    # Unix 绝对路径（与 qq-bridge 覆盖范围一致）
    re.compile(r"(?<![A-Za-z0-9])(?:/home/|/Users/|/etc/|/var/|/root/)[^\s\"'<>|]*"),
    # Authorization: Bearer xxx
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # token / 密码 / 密钥 / api key 等键值对
    re.compile(
        r"(?i)(?:token|密码|密钥|口令|password|passwd|secret|api[_-]?key|authorization|"
        r"access[_-]?key|credential)\s*(?:[:=：]|是|为)\s*[\"']?[^\s，。；、\"']{3,}"
    ),
    # 身份证（18 位，放银行卡前面，避免被 16~19 位数字规则截断）
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # 手机号
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 银行卡（16~19 位，允许空格/连字符分组）
    re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)"),
)


def clean_text(text: str | None) -> str:
    """剥离 CQ 码 / 媒体占位符，压缩空白。"""
    if not text:
        return ""
    text = _RE_CQ.sub("", text)
    text = _RE_PLACEHOLDER.sub("", text)
    return _RE_SPACE.sub(" ", text).strip()


def redact(text: str | None) -> tuple[str, int]:
    """脱敏，返回 (脱敏后文本, 命中次数)。"""
    if not text:
        return "", 0
    hits = 0
    for pattern in SENSITIVE_PATTERNS:
        text, n = pattern.subn("[已脱敏]", text)
        hits += n
    return text, hits


def is_suspicious(text: str | None) -> bool:
    """指令式话术打标（4.1.4，只打标不拒绝存储）。"""
    return bool(_RE_INJECTION.match(clean_text(text)))


def fingerprint(text: str | None) -> str:
    """日志用指纹：长度 + sha256 前 8 位，绝不记录原文。"""
    raw = (text or "").encode("utf-8", "ignore")
    return f"len={len(text or '')},sha8={hashlib.sha256(raw).hexdigest()[:8]}"


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


# ---------------------------------------------------------------------------
# jieba
# ---------------------------------------------------------------------------

_JIEBA_LOCK = threading.Lock()
_JIEBA_READY = False


def _ensure_jieba() -> None:
    global _JIEBA_READY
    if _JIEBA_READY:
        return
    with _JIEBA_LOCK:
        if _JIEBA_READY:
            return
        jieba.setLogLevel(logging.WARNING)
        custom = os.environ.get("QQ_MEMORY_JIEBA_DICT", "").strip()
        if custom:
            if os.path.exists(custom):
                jieba.set_dictionary(custom)
            else:
                logger.warning("QQ_MEMORY_JIEBA_DICT 指向的词典不存在，回退全量词典：%s", custom)
        jieba.initialize()
        try:  # L2/L3 黑话词表；失败不影响主流程
            import slang

            added = slang.apply(jieba)
            logger.info("黑话词表已加载：%d 条", added)
        except Exception as exc:  # noqa: BLE001
            logger.warning("黑话词表加载失败，按基础词典分词：%s", exc)
        _JIEBA_READY = True


STOPWORDS = frozenset(
    {
        "的", "了", "吗", "呢", "啊", "呀", "吧", "啦", "嘛", "呗", "咯", "哦", "嗯", "唉",
        "哈", "嘿", "喂", "噢", "喔", "哇", "嘞", "哎", "诶",
        "我", "你", "您", "他", "她", "它", "我们", "你们", "他们", "她们", "咱们", "自己",
        "这", "那", "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里",
        "就", "都", "也", "还", "又", "再", "才", "只", "很", "太", "更", "最", "非常",
        "特别", "比较", "有点", "真的", "其实", "应该", "可能", "觉得",
        "是", "在", "有", "和", "与", "及", "或", "而", "但", "但是", "如果", "因为",
        "所以", "虽然", "然后", "而且", "不过",
        "一个", "一下", "一些", "什么", "怎么", "为什么", "哪", "哪里", "谁", "几", "多少",
        "是否", "可以", "能", "会", "要", "想", "去", "来", "说", "看", "做", "给", "让",
        "被", "把", "对", "从", "到", "不", "没", "没有", "不是", "别", "甭",
        "请问", "帮我", "麻烦", "谢谢", "好的", "好", "行", "嗯嗯", "哦哦", "哈哈",
        "哈哈哈", "呵呵", "今天", "明天", "昨天", "现在", "刚才",
        "the", "a", "an", "is", "are", "was", "were", "and", "or", "of", "to", "in", "on",
        "for", "with", "i", "you", "he", "she", "it", "we", "they", "at", "by", "this", "that",
    }
)


def tokenize(text: str | None, *, max_tokens: int = 24) -> list[str]:
    """jieba 分词 + 去停用词/单字虚词/标点；数字保留 3 位以上。"""
    _ensure_jieba()
    return tokenize_with(jieba.lcut, text, max_tokens=max_tokens)


def tokenize_with(cut, text: str | None, *, max_tokens: int = 24) -> list[str]:
    """用给定的切词函数分词，过滤器与 tokenize() 完全一致。

    迁移时用它喂一个独立的 jieba.Tokenizer（加载目标档位词典），
    保证 tokens_next 与正式链路的清洗规则一致。
    """
    text = clean_text(text)
    if not text:
        return []
    out: list[str] = []
    for word in cut(text):
        word = word.strip()
        if not word or word in STOPWORDS:
            continue
        if _RE_PUNCT_ONLY.match(word):
            continue
        if word.isdigit():
            if len(word) < 3:
                continue
        elif len(word) < 2:
            continue
        out.append(word)
        if len(out) >= max_tokens:
            break
    return out


def tokens_text(tokens: list[str]) -> str:
    return " ".join(tokens)


# 代词不参与"虚词"剔除：8.1 验收要求「我胃疼」不被门控跳过，
# 若把"我"也当停用词就只剩 1 个有效词，会被"有效词 <2"误杀。
_PRONOUNS = frozenset(
    {"我", "你", "您", "他", "她", "它", "我们", "你们", "他们", "她们", "咱们", "自己"}
)


def tokenize_gate(text: str | None, *, max_tokens: int = 12) -> list[str]:
    """门控专用分词：去虚词/标点/停用词，但保留代词与单字实词。

    为什么不用 tokenize()：jieba 会把「我胃疼」切成 我/胃/疼，单字实词若被全部丢掉，
    就只剩 1 个词，会被「有效词 <2」误杀——而 8.1 验收明确要求它不被跳过。
    这里改用词性判断：只丢助词/介词/连词/副词/语气词/叹词/量词/标点。
    """
    text = clean_text(text)
    if not text:
        return []
    _ensure_jieba()
    out: list[str] = []
    for word, flag in pseg.cut(text):
        word = word.strip()
        if not word:
            continue
        if word in STOPWORDS and word not in _PRONOUNS:
            continue
        if flag.startswith(("u", "p", "c", "d", "y", "e", "o", "q", "w", "x")):
            continue
        if len(word) < 2 and flag not in ("n", "v", "a", "r", "ns", "nr", "nz"):
            continue
        out.append(word)
        if len(out) >= max_tokens:
            break
    return out


def extract_tags(text: str | None, limit: int = 5) -> list[str]:
    """规则打标：jieba 词性 n/vn、长度 ≥ 2、最多 limit 个（4.1.3）。"""
    text = clean_text(text)
    if not text:
        return []
    _ensure_jieba()
    counter: Counter[str] = Counter()
    for word, flag in pseg.cut(text):
        word = word.strip()
        if len(word) < 2 or word in STOPWORDS:
            continue
        if flag.startswith("n") or flag == "vn":
            counter[word] += 1
    return [word for word, _ in counter.most_common(limit)]


# ---------------------------------------------------------------------------
# 规则词表与判定
# ---------------------------------------------------------------------------

COMMITMENT_WORDS = (
    "记住", "约定", "答应", "说好", "别忘", "生日", "一定要", "保证", "约好",
    "见面", "提醒我", "讲好", "承诺",
)
PREFERENCE_WORDS = (
    "我喜欢", "我讨厌", "想要", "爱吃", "不爱", "最爱", "偏好", "不喜欢", "最讨厌",
    "喜欢", "讨厌", "想要一", "爱吃",
)
KNOWLEDGE_WORDS = ("科普", "教程", "常识", "原理", "知识", "区别", "如何选", "干货")
EMOTION_WORDS = (
    "开心", "高兴", "难过", "伤心", "生气", "委屈", "烦", "累", "焦虑", "害怕", "兴奋",
    "失望", "孤独", "幸福", "崩溃", "郁闷", "无语", "心疼", "难受", "爽", "难受", "emo",
)
TIME_HINTS = (
    "昨天", "今天", "前天", "明天", "后天", "上周", "本周", "这周", "下周", "上个月",
    "这个月", "下个月", "去年", "今年", "明年", "上次", "之前", "刚才", "刚刚", "最近",
    "前几天", "周末", "早上", "中午", "晚上", "半夜", "凌晨",
)
_GREETING_WORDS = frozenset(
    {
        "在吗", "在么", "在不在", "在", "嗯", "嗯嗯", "哦", "哦哦", "啊", "哈", "哈哈",
        "哈哈哈", "呵呵", "嘿", "喂", "你好", "您好", "hi", "hello", "早", "早安", "晚安",
        "1", "？", "?", "。。", "...", "、",
    }
)
_FIRST_PERSON = ("我", "咱", "俺", "本人")
_RE_QUESTION = re.compile(r"[?？]")
_RE_QUESTION_WORDS = (
    "吗", "呢", "什么", "怎么", "为什么", "哪", "谁", "几", "多少", "是否",
    "能不能", "可不可以", "要不要", "有没有", "是不是", "行不行", "好不好",
)
_RE_PLACE = re.compile(
    r"(?:在|去|到)[^，。！？]{0,8}(?:市|区|县|路|街|店|馆|园|场|站|机场|医院|学校|公司|家|楼|号)"
)
_RE_NUMBER = re.compile(r"\d")
_RE_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def contains_any(text: str, words: tuple[str, ...] | list[str]) -> bool:
    return any(word in text for word in words)


def has_commitment(text: str | None) -> bool:
    return contains_any(clean_text(text), COMMITMENT_WORDS)


def has_preference(text: str | None) -> bool:
    return contains_any(clean_text(text), PREFERENCE_WORDS)


def has_knowledge_marker(text: str | None) -> bool:
    return contains_any(clean_text(text), KNOWLEDGE_WORDS)


def has_emotion(text: str | None) -> bool:
    return contains_any(clean_text(text), EMOTION_WORDS)


def has_first_person(text: str | None) -> bool:
    clean = clean_text(text)
    return any(clean.startswith(p) or p in clean for p in _FIRST_PERSON)


def is_question(text: str | None) -> bool:
    clean = clean_text(text)
    if _RE_QUESTION.search(clean):
        return True
    return contains_any(clean, _RE_QUESTION_WORDS)


def has_concrete_info(text: str | None) -> bool:
    """含数字 / 时间 / 地点——群聊"选记"的一个正条件（4.1）。"""
    clean = clean_text(text)
    return bool(
        _RE_NUMBER.search(clean)
        or contains_any(clean, TIME_HINTS)
        or _RE_PLACE.search(clean)
    )


def has_time_hint(text: str | None) -> bool:
    return contains_any(clean_text(text), TIME_HINTS)


def is_greeting(text: str | None) -> bool:
    """寒暄黑名单（4.2 门控第 4 条）：在吗/嗯/哦/哈哈/？/。。"""
    cleaned = clean_text(text)
    core = re.sub(r"[\s\W_]+", "", cleaned, flags=re.UNICODE).lower()
    if not core:
        return True
    if core in _GREETING_WORDS:
        return True
    if len(core) <= 2 and all(ch in "嗯哦啊哈呵嘿唉诶呀吧啦" for ch in core):
        return True
    return False


def is_noise(text: str | None) -> bool:
    """纯表情 / 纯 CQ / 纯链接 → 不记录。"""
    cleaned = clean_text(text)
    if not cleaned:
        return True
    without_url = _RE_URL.sub("", cleaned).strip()
    if not without_url:
        return True
    return bool(_RE_PUNCT_ONLY.match(without_url))


def is_command(text: str | None) -> bool:
    cleaned = clean_text(text)
    return cleaned.startswith("/") or cleaned.startswith("／")


def detect_category(text: str | None, *, fact_key: str | None = None) -> str:
    """event | fact | preference | knowledge（4.1.3）。"""
    if fact_key:
        return "fact"
    if has_preference(text):
        return "preference"
    if has_knowledge_marker(text):
        return "knowledge"
    return "event"


def detect_importance(text: str | None) -> float:
    """约定/承诺/长期偏好=1.0；情绪词=0.8；其余 0.5（4.1.3）。"""
    if has_commitment(text):
        return 1.0
    if has_emotion(text):
        return 0.8
    return 0.5


def detect_visibility(scope_type: str, text: str | None) -> str:
    """私聊 private；群聊 group；纯知识且无第一人称 → public（4.1.3）。"""
    if scope_type == "private":
        return "private"
    if has_knowledge_marker(text) and not has_first_person(text):
        return "public"
    return "group"


def detect_emotion(text: str | None) -> float:
    return 0.8 if has_emotion(text) else 0.0


def is_bearer_token(text: str | None) -> bool:
    return bool(_RE_BEARER.search(clean_text(text)))
