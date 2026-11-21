"""VLM 评测套件：图文理解（截图诊断 + 配图描述）。

VLM 在本项目有两个量纲完全不同的出口，分开评：

1. **截图诊断** ``process_image()`` —— 输出结构化字段（error_code / service /
   error_type 等），天然可算**字段级准确率**（error_code 精确匹配、service 宽松
   子串匹配、error_type 枚举匹配）。

2. **配图描述** ``describe_knowledge_image()`` —— 自由文本，用三合一：
   ① 与人工参考描述的**嵌入余弦**（初筛）；
   ② **关键要素命中率**（主指标，人工列 key_points）；
   ③ **LLM-as-judge 幻觉检测**（描述中是否出现图中不存在的实体）。

图片为确定性合成图（见 ``eval/images.py``），金标写在 ``datasets/*.jsonl``。

指标与判定（两类出口量纲不同，故用多指标联合判定，两项都达标才算 PASS）：
  - 截图诊断：``error_code_accuracy`` / ``error_type_accuracy``（归一化精确匹配）、
    ``service_hit_rate``（子串宽松匹配）、``diagnosis_field_accuracy``（三字段命中率
    均值，主指标，阈值 ``EVAL_THRESHOLD_VLM_FIELD_ACC``）。
  - 配图描述：``caption_keypoint_hit``（关键点命中率，主指标，阈值
    ``EVAL_THRESHOLD_CAPTION_KEYPOINT``）、``caption_cosine``（与参考描述的嵌入余弦，
    仅作初筛，不参与判定）、``hallucination_free_rate``（无幻觉比例，由 LLM 评判）。
  - 字段准确率 / 关键点命中率口径相同：「命中数 / 总数」，越大越好，1 为满分。
前置条件：图片由 ``eval/images.py`` 合成，无需外部素材；配图描述依赖视觉模型可用。
"""
from __future__ import annotations

from core.config import settings
from core.rag.multimodal import describe_knowledge_image, process_image
from core.rag.multimodal_rag import multimodal_rag
from eval import metrics
from eval.images import materialize
from eval.judges import judge_caption
from eval.suites._common import build_result, load_cases, map_limited

SUITE = "vlm"
TITLE = "图文理解（截图诊断 + 配图描述）"

_FIELDS = ("error_code", "service", "error_type")


async def _diag_one(case: dict) -> dict:
    """跑单条「截图诊断」用例：取图 → VLM 结构化诊断 → 逐字段比对金标。

    参数：
        case：用例，含 ``id`` / ``image``（图片文件名）/ ``gold``（金标字段 dict）。
    返回：
        明细 dict：``pred`` 预测字段、``matches`` 各字段是否命中、``field_hit``
        （三字段命中率）、``ok`` 等。
    异常：
        不向外抛异常；取图或诊断失败时返回带 error 的 ``ok=False`` 明细。
    """
    gold = case.get("gold") or {}
    try:
        image = materialize(case.get("image", ""))  # 按文件名取或即时生成合成图
        pred = await process_image(image)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "diagnosis", "id": case.get("id"), "image": case.get("image"),
                "gold": gold, "pred": {}, "matches": {}, "ok": False, "error": str(exc)}

    # 分字段比对：error_code/error_type 用归一化精确匹配；service 用子串宽松匹配
    # （模型常带修饰词，如 gold="VPN"、pred="VPN 客户端"，严格相等会误判）
    matches = {
        "error_code": metrics.exact_match(pred.get("error_code"), gold.get("error_code")),
        "service": metrics.substring_match(pred.get("service"), gold.get("service")),
        "error_type": metrics.exact_match(pred.get("error_type"), gold.get("error_type")),
    }
    return {
        "kind": "diagnosis",
        "id": case.get("id"),
        "image": case.get("image"),
        "gold": gold,
        "pred": {k: pred.get(k) for k in _FIELDS},
        "matches": matches,
        "field_hit": sum(1 for v in matches.values() if v) / len(matches),  # 命中字段数/字段总数
        "ok": True,
        "error": "",
    }


async def _caption_one(case: dict) -> dict:
    """跑单条「配图描述」用例：取图 → VLM 自由描述 → 三合一打分。

    参数：
        case：用例，含 ``id`` / ``image`` / ``key_points``（人工关键点列表）/
            ``gold_caption``（人工参考描述）。
    返回：
        明细 dict：``caption`` 生成描述、``cosine`` 与参考描述的嵌入余弦（初筛）、
        ``keypoint_hit`` 关键点命中率（主指标）、``judge`` LLM 幻觉判定等。
    异常：
        不向外抛异常；取图或描述失败时返回 ``ok=False``。
    """
    key_points = case.get("key_points") or []
    gold_caption = case.get("gold_caption", "")
    try:
        image = materialize(case.get("image", ""))
        caption = await describe_knowledge_image(image)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "caption", "id": case.get("id"), "image": case.get("image"),
                "key_points": key_points, "caption": "", "cosine": None,
                "keypoint_hit": None, "judge": {"available": False}, "ok": False, "error": str(exc)}

    if not caption:
        # 描述为空（未配置 VLM 或返回空）：明确失败并记原因，避免下游指标除零
        return {"kind": "caption", "id": case.get("id"), "image": case.get("image"),
                "key_points": key_points, "caption": "", "cosine": None,
                "keypoint_hit": None, "judge": {"available": False, "reason": "VLM 未配置或返回空描述"},
                "ok": False, "error": "描述为空"}

    cosine = None
    try:
        # 嵌入余弦：把参考描述与生成描述编码为向量，算夹角余弦；仅作初筛参考，不判优劣
        vecs = await multimodal_rag.embedder.encode_texts([gold_caption, caption])
        if len(vecs) == 2:
            cosine = round(metrics.cosine(vecs[0], vecs[1]), 4)
    except Exception:  # noqa: BLE001
        cosine = None  # 嵌入服务不可用时留空，不影响主指标

    judge = await judge_caption(image, caption, key_points)  # LLM-as-judge 幻觉检测
    return {
        "kind": "caption",
        "id": case.get("id"),
        "image": case.get("image"),
        "key_points": key_points,
        "caption": caption,
        "cosine": cosine,
        # 关键点命中率：归一化后 key_point 作为描述的子串出现即算命中
        "keypoint_hit": round(metrics.key_point_hit_rate(caption, key_points), 4),
        "judge": judge,
        "ok": True,
        "error": "",
    }


async def run(limit: int | None = None) -> dict:
    """执行 VLM 套件：分别评测截图诊断与配图描述，再合并成一份报告。

    参数：
        limit：每类最多评测条数；None 时用 ``EVAL_MAX_CASES``。
    返回：
        统一结果 dict。采用 ``criteria`` 多指标联合判定：诊断字段准确率与描述关键点
        命中率两项都达标才算 PASS（一票否决）。
    """
    diag_cases = load_cases("vlm_cases.jsonl", limit)
    cap_cases = load_cases("caption_cases.jsonl", limit)

    diag_items = await map_limited(diag_cases, _diag_one) if diag_cases else []
    cap_items = await map_limited(cap_cases, _caption_one) if cap_cases else []

    ok_diag = [it for it in diag_items if it.get("ok")]  # 诊断成功条目
    ok_cap = [it for it in cap_items if it.get("ok")]    # 描述成功条目

    # 各字段准确率 = 该字段命中条数 / 诊断成功条数
    field_acc = {
        field: round(metrics.mean(1.0 if it["matches"].get(field) else 0.0 for it in ok_diag), 4)
        if ok_diag else None
        for field in _FIELDS
    }
    diagnosis_overall = None
    if ok_diag:
        diagnosis_overall = round(
            metrics.mean(it["field_hit"] for it in ok_diag), 4
        )
    # service 宽松匹配下整体口径仍按字段平均
    caption_cosine = round(metrics.mean([it["cosine"] for it in ok_cap if it["cosine"] is not None]), 4) \
        if any(it["cosine"] is not None for it in ok_cap) else None
    caption_keypoint = round(metrics.mean([it["keypoint_hit"] for it in ok_cap]), 4) if ok_cap else None

    # 只统计 judge 可用（available=True）的样本：未测的样本计入分母会系统性抬高该率
    judged = [it for it in ok_cap if (it.get("judge") or {}).get("available")]
    hallucination_free = round(
        metrics.mean(1.0 if not (it["judge"].get("hallucinated") or []) else 0.0 for it in judged), 4
    ) if judged else None

    metrics_map = {
        "error_code_accuracy": field_acc["error_code"],      # 错误码字段准确率
        "service_hit_rate": field_acc["service"],            # 服务名命中率（宽松）
        "error_type_accuracy": field_acc["error_type"],      # 错误类型准确率
        "diagnosis_field_accuracy": diagnosis_overall,       # 三字段综合准确率（主指标）
        "caption_cosine": caption_cosine,                    # 描述嵌入余弦（初筛）
        "caption_keypoint_hit": caption_keypoint,            # 关键点命中率（主指标）
        "hallucination_free_rate": hallucination_free,       # 无幻觉比例
        "judge_available": len(judged),                      # judge 可用的样本数
    }

    items = diag_items + cap_items
    if not items:
        return build_result(SUITE, TITLE, {}, [], notes=["无可用用例（datasets/vlm_cases.jsonl 为空）"])

    return build_result(
        SUITE,
        TITLE,
        metrics_map,
        items,
        criteria=[
            # (指标名, 阈值, 越低越好)：两项都要达线才 PASS
            ("diagnosis_field_accuracy", settings.EVAL_THRESHOLD_VLM_FIELD_ACC, False),
            ("caption_keypoint_hit", settings.EVAL_THRESHOLD_CAPTION_KEYPOINT, False),
        ],
        notes=["配图描述为自由文本，主指标为关键词命中率；余弦为初筛，幻觉由 LLM-as-judge 判定。"],
    )
