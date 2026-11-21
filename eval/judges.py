"""LLM-as-judge：用于自由文本类输出的主观质量打分。

当前只服务 VLM「配图描述」评测的第三项——**幻觉检测**（描述里是否出现图中
不存在的实体）。复用 VLM 能力四元组（``VLM_BASE_URL/MODEL/API_KEY``），
未配置时返回 ``available=False``，由上层跳过而非报错。

已知局限：judge 与生成同源（同一 VLM）时存在同源偏差，报告需如实标注。
"""
from __future__ import annotations

import asyncio
import logging

from core.config import settings
from core.ai.llm import extract_json

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """你是图像描述质检员。下面是一张图片，以及一段对它的自动描述。
请判断该描述是否存在**幻觉**：即描述中出现了图片里根本不存在的内容（错误码、
系统名、端口、按钮、文字等）。

另外评估描述对以下关键要素的覆盖情况：{key_points}

只输出 JSON，不要解释：
{{"hallucinated": ["描述中提到但图中不存在的内容"], "coverage": 0.0, "reason": "一句话理由"}}
其中 coverage 为 0~1 的小数，表示关键要素被描述覆盖的比例；无幻觉时 hallucinated 为空数组。

待质检描述：
{caption}
"""


def _judge_sync(data_url: str, caption: str, key_points: list[str]) -> dict:
    """同步调用 VLM 做 judge（供 asyncio.to_thread 包装，避免阻塞事件循环）。

    参数：
        data_url：图片的 data URL（base64 内联，形如 ``data:image/...;base64,...``）。
        caption：待质检的描述文本。
        key_points：人工关键点列表，用于提示 judge 评估覆盖度。
    返回：
        dict：``available``（judge 是否可用）、``hallucinated``（幻觉内容列表）、
        ``coverage``（关键点覆盖度 0~1，可能为 None）、``reason``。
    说明：
        网络调用异常由外层 ``judge_caption`` 捕获；本函数对「输出无法解析」返回
        ``available=False``，绝不臆断为「无幻觉」。
    """
    from openai import OpenAI

    if not (settings.VLM_BASE_URL and settings.VLM_MODEL and settings.vlm_key):
        return {"available": False, "reason": "VLM 未配置"}  # 未配置则整体跳过，不报错

    client = OpenAI(base_url=settings.VLM_BASE_URL, api_key=settings.vlm_key)
    prompt = _JUDGE_PROMPT.format(
        key_points="、".join(key_points) or "（未提供）",
        caption=caption or "（空描述）",
    )
    resp = client.chat.completions.create(
        model=settings.VLM_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        timeout=60,
    )
    content = resp.choices[0].message.content if resp.choices else ""
    parsed = extract_json(content or "") or {}
    if not parsed:
        # 解析失败不得当作「无幻觉」：那样会把未测样本计入分母并判为合格，
        # 系统性抬高 hallucination_free_rate。统一按「不可用」交给上层跳过。
        return {"available": False, "reason": "judge 输出无法解析"}
    # 容错：模型可能返回非列表，统一规整为字符串列表
    hallucinated = parsed.get("hallucinated") or []
    if not isinstance(hallucinated, list):
        hallucinated = [str(hallucinated)]
    coverage = parsed.get("coverage")
    try:
        coverage = float(coverage) if coverage is not None else None
    except (TypeError, ValueError):
        coverage = None  # 非法数值统一置 None，避免脏数据污染指标
    return {
        "available": True,
        "hallucinated": [str(x) for x in hallucinated],
        "coverage": coverage,
        "reason": str(parsed.get("reason") or ""),
    }


async def judge_caption(image_bytes: bytes, caption: str, key_points: list[str]) -> dict:
    """判定一段配图描述是否存在幻觉（LLM-as-judge）。

    参数：
        image_bytes：图片字节。
        caption：VLM 生成的描述。
        key_points：人工关键点列表。
    返回：
        ``available`` / ``hallucinated`` / ``coverage`` / ``reason``。任何失败都返回
        ``available=False``，交由上层跳过而非报错。
    说明：
        同步的 OpenAI 调用用 ``asyncio.to_thread`` 丢到线程池，避免阻塞事件循环；
        调用前先降采样以控制请求体大小与 token 成本。
    """
    if not image_bytes:
        return {"available": False, "reason": "图片为空"}

    from core.rag.multimodal import build_data_url, downsample

    try:
        data, _ = await asyncio.to_thread(
            downsample,
            image_bytes,
            settings.EMBEDDING_MAX_IMAGE_SIDE,
            settings.EMBEDDING_MAX_IMAGE_BYTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("judge 降采样失败，使用原图：%s", exc)
        data = image_bytes  # 降采样失败不致命，退回原图继续

    try:
        data_url = build_data_url(data)
        # 把同步阻塞调用转成线程执行，避免卡住整个 asyncio 事件循环，
        # 否则并发跑多个 judge 时会退化成串行。
        return await asyncio.to_thread(_judge_sync, data_url, caption, key_points)
    except Exception as exc:  # noqa: BLE001
        logger.warning("配图描述 judge 失败：%s", exc)
        return {"available": False, "reason": f"judge 调用失败：{exc}"}
