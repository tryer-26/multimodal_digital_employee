"""检索评测套件：多模态检索排序质量（Recall / MRR / nDCG）。

对 ``datasets/retrieval_cases.jsonl`` 的每个 query 分别跑
**多模态模式**与 ``text_only`` 纯文本模式（见设计文档 7.6 核心演示点），
按来源文件去重后计算排序指标，并给出两者的增益差。

前置条件：演示知识库需已索引（知识库管理页「加载演示知识库」）。
若金标来源不在已索引文档中，本套件跳过并提示，不误报。

指标与判定（相关性按「命中集合」二值判定，排名越靠前越好，均为越大越好）：
  - ``mm.recall@3``（主指标）：多模态模式下前 3 命中金标来源的比例，阈值
    ``EVAL_THRESHOLD_RETRIEVAL_RECALL``（0.70）。
  - ``hit@1``：首位是否命中；``mrr``：首个命中项排名倒数的均值（越靠前越高）；
    ``ndcg@3``：兼顾命中数与位置的排序质量，理想排序为 1。
  - ``text_only.*`` 为同题纯文本模式对照；``multimodal_gain@3`` = 两模式 Recall@3 之差，
    用来量化「文查图」增益。
  - 几个检索指标的用途差异：Recall@k 看「找全没」，Hit@k 看「有没有」，
    MRR 看「第一个命中的多靠前」，nDCG@k 在命中基础上再惩罚位置靠后。
"""
from __future__ import annotations

from core.config import settings
from core.infra.db import db
from core.rag.multimodal_rag import multimodal_rag
from eval import metrics
from eval.suites._common import build_result, load_cases, map_limited, skipped

SUITE = "retrieval"
TITLE = "检索（多模态 vs 纯文本）"
_KS = (1, 3)


async def _rank(query: str, text_only: bool) -> list[str]:
    """检索并按来源文件去重，保留首次出现的顺序作为排名。

    参数：
        query：查询文本。
        text_only：True 走纯文本检索，False 走多模态检索（二者互为对照）。
    返回：
        去重后的来源文件名列表，按检索得分从高到低排列（即排名）。
    """
    results = await multimodal_rag.search(query=query, text_only=text_only)
    ranked: list[str] = []
    for item in results:
        source = (item.get("metadata") or {}).get("source")
        if source and source not in ranked:  # 同一来源只保留排名最高的一次
            ranked.append(source)
    return ranked


def _score(ranked: list[str], gold: list[str]) -> dict:
    """对单条 query 的排名算一组检索指标。

    参数：
        ranked：去重后的来源排名列表（越靠前越相关）。
        gold：金标相关来源列表。
    返回：
        指标名 → 值的 dict：hit@1 / mrr / ndcg@3 以及各 k 的 recall@k。
    """
    out = {"hit@1": metrics.hit_at_k(ranked, gold, 1),
           "mrr": round(metrics.reciprocal_rank(ranked, gold), 4),
           "ndcg@3": round(metrics.ndcg_at_k(ranked, gold, 3), 4)}
    for k in _KS:
        out[f"recall@{k}"] = metrics.recall_at_k(ranked, gold, k)
    return out


async def _one(case: dict) -> dict:
    """跑单条检索用例：同一 query 分别用多模态与纯文本检索并各自算指标。

    参数：
        case：用例，含 ``id`` / ``query`` / ``gold``（相关来源列表）。
    返回：
        明细 dict：两种模式的排名与指标（``multimodal`` / ``text_only``）。
    异常：
        不向外抛异常；检索失败时返回带 error、两种模式指标为空的明细。
    """
    query = case.get("query", "")
    gold = case.get("gold") or []
    try:
        # 同一 query 跑两种模式，便于直接对比「文查图」增益
        mm_ranked = await _rank(query, text_only=False)
        to_ranked = await _rank(query, text_only=True)
    except Exception as exc:  # noqa: BLE001
        return {"id": case.get("id"), "query": query, "gold": gold,
                "multimodal": {}, "text_only": {}, "error": str(exc)}
    return {
        "id": case.get("id"),
        "query": query,
        "gold": gold,
        "multimodal_ranked": mm_ranked,
        "text_only_ranked": to_ranked,
        "multimodal": _score(mm_ranked, gold),
        "text_only": _score(to_ranked, gold),
        "error": "",
    }


def _aggregate(items: list[dict], key: str) -> dict:
    """把逐条明细里某个模式（multimodal / text_only）的指标取均值。

    参数：
        items：逐条明细列表。
        key：取值键，"multimodal" 或 "text_only"。
    返回：
        指标名 → 均值的 dict；无有效条目时返回空 dict。
    """
    valid = [it for it in items if it.get(key)]  # 检索失败的条目该键为空，需排除
    if not valid:
        return {}
    out: dict = {}
    for metric_name in valid[0][key]:  # 各条指标的键集合一致，取首条即可
        out[metric_name] = round(
            metrics.mean(it[key][metric_name] for it in valid), 4
        )
    return out


async def run(limit: int | None = None) -> dict:
    """执行检索套件：多模态 vs 纯文本对照评测。

    参数：
        limit：最多评测条数；None 时用 ``EVAL_MAX_CASES``。
    返回：
        统一结果 dict；若知识库未就绪则返回 SKIP 占位结果（不误报为 FAIL）。
    """
    cases = load_cases("retrieval_cases.jsonl", limit)
    if not cases:
        return build_result(SUITE, TITLE, {}, [], notes=["无可用用例（datasets/retrieval_cases.jsonl 为空）"])

    # 前置检查：金标来源必须都在已索引文档中，否则检索必然找不中（属环境问题）→ SKIP
    indexed = {doc.get("filename") for doc in await db.list_documents()}
    missing = sorted(
        {src for case in cases for src in (case.get("gold") or [])} - indexed
    )
    if missing:
        return skipped(
            SUITE,
            TITLE,
            "演示知识库未就绪，缺少金标来源：" + "、".join(missing)
            + "。请先在「知识库管理」执行『加载演示知识库』后重跑。",
        )

    items = await map_limited(cases, _one)  # 受 EVAL_CONCURRENCY 限流地并发执行
    mm = _aggregate(items, "multimodal")
    to = _aggregate(items, "text_only")

    # 用前缀区分两种模式，避免指标名冲突
    metrics_map = {f"mm.{k}": v for k, v in mm.items()}
    metrics_map.update({f"text_only.{k}": v for k, v in to.items()})
    if "recall@3" in mm and "recall@3" in to:
        # 增益差：多模态相对纯文本在 Recall@3 上提升多少，用于体现「文查图」价值
        metrics_map["multimodal_gain@3"] = round(mm["recall@3"] - to["recall@3"], 4)

    return build_result(
        SUITE,
        TITLE,
        metrics_map,
        items,
        criteria=[("mm.recall@3", settings.EVAL_THRESHOLD_RETRIEVAL_RECALL, False)],
        notes=["主指标为多模态模式的 Recall@3；text_only 为同题对照，用于体现「文查图」增益。"],
    )
