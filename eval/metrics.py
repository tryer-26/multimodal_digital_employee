"""评测指标库（纯函数，无网络 / 无副作用，便于离线单测）。

四类指标：
  - 文本错误率：CER / WER（ASR 识别准确率、TTS 反向可懂度）
  - 检索排序：Recall@k / Precision@k / Hit@k / MRR / nDCG@k
  - 结构化字段：归一化精确匹配 / 字段准确率（VLM 截图诊断）
  - 描述质量：关键要素命中率 / 余弦相似度（VLM 配图描述）

单测见 ``tests/test_eval_metrics.py``（不依赖任何外部服务）。
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Iterable, Sequence

# ----------------------------------------------------------------------
# 文本归一化
# ----------------------------------------------------------------------
# 归一化后仅保留：ASCII 字母数字 + CJK 统一表意文字。
# 注意 NFKC 已把全角字符（ＡＢ１２，）折成半角，故此处只需剔除其余符号/空白。
_KEEP_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

# 中文数字 → 阿拉伯数字。语音转写会把「619」写成「六幺九」或「六百一十九」，
# 不做这步会让 CER 虚高。策略：对连续的中文数字/数词串整体转换，
#   无单位（六幺九）→ 逐字映射拼接；有单位（六百一十九）→ 按十百千万进位求和。
_CJK_DIGITS = {
    "零": 0, "〇": 0, "洞": 0,
    "一": 1, "幺": 1,
    "二": 2, "两": 2,
    "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CJK_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CJK_NUM_RE = re.compile(
    "[" + "".join(_CJK_DIGITS) + "".join(_CJK_UNITS) + "]+"
)

_EN_TOK_RE = re.compile(r"[a-z0-9]+")

# 小数点的两种写法必须等价：「3.2」与「三点二」若不统一，小数场景会因写法差异
# 产生虚假 CER。做法：夹在数字之间的小数点（. / · / 点）直接剔除，与后续剔除标点
# 的口径一致；限定「点」夹在数字之间，避免误伤「重点」「地点」等普通词。
_NUM_CHARS = "".join(_CJK_DIGITS) + "".join(_CJK_UNITS) + "0123456789"
_DECIMAL_DOT_RE = re.compile(f"(?<=[{_NUM_CHARS}])[.·点](?=[{_NUM_CHARS}])")


def _cjk_run_to_arabic(run: str) -> str:
    """把一个连续的中文数字串转成阿拉伯数字串。"""
    if not any(ch in _CJK_UNITS for ch in run):
        return "".join(str(_CJK_DIGITS[ch]) for ch in run)
    total = 0
    section = 0
    number = 0
    for ch in run:
        if ch in _CJK_DIGITS:
            number = _CJK_DIGITS[ch]
        else:
            unit = _CJK_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit  # 「十九」的十位隐含 1
            number = 0
    return str(total + section + number)


def normalize_text(text: object) -> str:
    """评测比对前的文本归一化（去除标点、空白、大小写与数字写法差异）。"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    s = _DECIMAL_DOT_RE.sub("", s)
    s = _CJK_NUM_RE.sub(lambda m: _cjk_run_to_arabic(m.group()), s)
    return _KEEP_RE.sub("", s)


def _tokenize_en(text: object) -> list[str]:
    """英文按词切分（用于 WER）。"""
    if text is None:
        return []
    return _EN_TOK_RE.findall(unicodedata.normalize("NFKC", str(text)).lower())


# ----------------------------------------------------------------------
# 编辑距离与错率
# ----------------------------------------------------------------------
def edit_distance(a: Sequence, b: Sequence) -> int:
    """序列编辑距离（Levenshtein），即 替换 S + 删除 D + 插入 I 之和。

    对 ``str`` 即为字级距离，对 ``list`` 即为词级距离。
    """
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1,          # 删除
                    cur[j - 1] + 1,       # 插入
                    prev[j - 1] + (ca != cb),  # 替换 / 命中
                )
            )
        prev = cur
    return prev[-1]


def char_error_rate(reference: str, hypothesis: str, normalize: bool = True) -> float:
    """字错率 CER = (S+D+I)/N，N 为参考文本长度。

    可能 >1（插入过多时）；参考为空且假设非空时记 1.0。
    """
    ref = normalize_text(reference) if normalize else str(reference or "")
    hyp = normalize_text(hypothesis) if normalize else str(hypothesis or "")
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """词错率 WER（按英文词切分），面向英文语料。"""
    ref = _tokenize_en(reference)
    hyp = _tokenize_en(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def corpus_cer(pairs: Iterable[tuple[str, str]]) -> float:
    """语料级 CER = Σ 编辑距离 / Σ 参考长度（比句级平均更稳健，长句权重更高）。"""
    total_dist = 0
    total_len = 0
    for reference, hypothesis in pairs:
        ref = normalize_text(reference)
        hyp = normalize_text(hypothesis)
        total_dist += edit_distance(ref, hyp)
        total_len += len(ref)
    if total_len == 0:
        return 0.0
    return total_dist / total_len


def sentence_accuracy(pairs: Iterable[tuple[str, str]]) -> float:
    """句准率：归一化后完全一致（CER=0）的句子占比。"""
    total = 0
    hit = 0
    for reference, hypothesis in pairs:
        total += 1
        if normalize_text(reference) == normalize_text(hypothesis):
            hit += 1
    return hit / total if total else 0.0


# ----------------------------------------------------------------------
# 检索排序指标（相关性按「命中集合」二值判定）
# ----------------------------------------------------------------------
def recall_at_k(ranked: Sequence, relevant: Iterable, k: int) -> float:
    """Recall@k = 命中的相关项数 / 相关项总数。"""
    rel = set(relevant)
    if not rel:
        return 0.0
    return len(rel & set(ranked[:k])) / len(rel)


def precision_at_k(ranked: Sequence, relevant: Iterable, k: int) -> float:
    """Precision@k = 前 k 条中相关项数 / k（ranked 不足 k 条时仍以 k 为分母）。"""
    if k <= 0:
        return 0.0
    return len(set(relevant) & set(ranked[:k])) / k


def hit_at_k(ranked: Sequence, relevant: Iterable, k: int) -> float:
    """Hit@k：前 k 条是否至少命中一个相关项（1/0）。"""
    return 1.0 if set(relevant) & set(ranked[:k]) else 0.0


def reciprocal_rank(ranked: Sequence, relevant: Iterable) -> float:
    """RR = 1 / 首个相关项的排名（从 1 开始），未命中记 0。"""
    rel = set(relevant)
    for idx, item in enumerate(ranked, 1):
        if item in rel:
            return 1.0 / idx
    return 0.0


def mrr(ranked_lists: Iterable[Sequence], relevant_lists: Iterable[Iterable]) -> float:
    """MRR：多条查询 RR 的均值。"""
    scores = [
        reciprocal_rank(ranked, relevant)
        for ranked, relevant in zip(ranked_lists, relevant_lists)
    ]
    return mean(scores)


def ndcg_at_k(ranked: Sequence, relevant: Iterable, k: int) -> float:
    """nDCG@k（二值相关性）。"""
    rel = set(relevant)
    dcg = sum(
        1.0 / math.log2(i + 2) for i, item in enumerate(ranked[:k]) if item in rel
    )
    ideal_hits = min(len(rel), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


# ----------------------------------------------------------------------
# 结构化字段（VLM 截图诊断）
# ----------------------------------------------------------------------
def exact_match(prediction: object, gold: object, normalize: bool = True) -> bool:
    """归一化后精确匹配（用于错误码 / 服务 / 类型等枚举字段）。"""
    if normalize:
        return normalize_text(prediction) == normalize_text(gold)
    return str(prediction or "").strip() == str(gold or "").strip()


def field_accuracy(predictions: Iterable, golds: Iterable, normalize: bool = True) -> float:
    """字段级准确率：逐条比对后取均值（命中=1，未命中=0）。"""
    hits = [
        1.0 if exact_match(p, g, normalize) else 0.0
        for p, g in zip(predictions, golds)
    ]
    return mean(hits)


def substring_match(prediction: object, gold: object, normalize: bool = True) -> bool:
    """宽松匹配：gold 归一化后作为 prediction 的子串出现即命中。

    用于 ``service`` 这类模型可能带修饰词（"VPN" vs "VPN 客户端"）的字段。
    """
    p = normalize_text(prediction) if normalize else str(prediction or "").strip()
    g = normalize_text(gold) if normalize else str(gold or "").strip()
    return bool(g) and g in p


# ----------------------------------------------------------------------
# 描述质量（VLM 配图描述）
# ----------------------------------------------------------------------
def key_point_hit_rate(text: object, key_points: Sequence[str]) -> float:
    """关键要素命中率：归一化后 key_point 作为子串出现即算命中。"""
    if not key_points:
        return 0.0
    norm = normalize_text(text)
    hit = sum(1 for kp in key_points if normalize_text(kp) and normalize_text(kp) in norm)
    return hit / len(key_points)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（零向量返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ----------------------------------------------------------------------
# 统计小工具
# ----------------------------------------------------------------------
def mean(values: Iterable[float]) -> float:
    seq = list(values)
    return sum(seq) / len(seq) if seq else 0.0


def pearson(xs: Iterable[float], ys: Iterable[float]) -> float:
    """皮尔逊相关系数（样本不足或零方差返回 0）。

    用于 TTS 的「时长 vs 文本长度」一致性：正常合成应接近 1（越长越久），
    显著偏低说明存在漏读 / 截断。
    """
    a = [float(x) for x in xs]
    b = [float(y) for y in ys]
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ma, mb = mean(a), mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def percentile(values: Iterable[float], p: float) -> float:
    """线性插值百分位（p 取 0~1）。空集返回 0。"""
    seq = sorted(float(v) for v in values)
    if not seq:
        return 0.0
    if len(seq) == 1:
        return seq[0]
    pos = max(0.0, min(1.0, p)) * (len(seq) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return seq[lo]
    return seq[lo] + (seq[hi] - seq[lo]) * (pos - lo)
