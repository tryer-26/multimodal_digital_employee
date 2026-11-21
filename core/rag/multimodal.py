"""多模态感知：ASR / OCR / 视觉理解 / 情绪识别 / TTS，见设计文档 8.1–8.4。

对外接口：
  - :func:`process_voice`  —— 语音转文字（ASR）
  - :func:`process_image`  —— 图片 OCR + 视觉理解（结构化视觉诊断，被 ScreenshotAnalyzeTool 调用）
  - :func:`describe_knowledge_image` —— 知识库配图描述（入库期，写入文本空间作主召回）
  - :func:`perceive_message` —— 会话前置识别（一次调用同时产出「来意 intent」与「情绪」）
  - :func:`synthesize_speech` —— 文字转语音（edge-tts）

所有网络调用均有超时（30s）与异常兜底，关键错误以中文日志打印。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from core.config import settings
from core.rag.embedding import build_data_url
from core.ai.llm import extract_json
from core.rag.parsers import downsample

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0


class PerceptionError(RuntimeError):
    """多模态感知能力调用失败（含中文可读信息）。"""


# ----------------------------------------------------------------------
# 视觉理解提示词
# ----------------------------------------------------------------------
_VLM_PROMPT = """你是 IT 服务台的截图诊断助手。请分析用户上传的报错截图 / 界面截图。
本地 OCR 已识别的文字（可能为空或不准确，请结合图片自行校正）：
{ocr}

请只输出一个 JSON 对象，不要输出任何多余文字，字段如下：
{{
  "ocr_text": "图中关键文字（错误码、报错信息、配置项等）",
  "description": "对截图内容与场景的一句话中文描述",
  "error_code": "错误码（如 502、619 等，无则空字符串）",
  "service": "涉及的服务/组件（如 nginx、VPN、邮箱等，无则空字符串）",
  "hint": "排障提示或可能原因（中文，一句话）",
  "error_type": "错误类型，只能取 网络 / 权限 / 软件 / 系统 之一"
}}"""

_KB_IMAGE_PROMPT = """你是企业IT知识库的配图标注助手。请客观描述这张知识库插图，供后续检索与作答使用。
只输出一段中文（不超过120字），依次包含：
1) 图类型（网络拓扑 / 流程图 / 界面截图 / 报错截图 / 照片 / 图表等）；
2) 主体与关键组件或系统（如 VPN 网关、域控、OA、打印机）；
3) 图中可见的关键文字、标签或参数（如 IP、域名、错误码、按钮）；
4) 该图表达的关键关系或结论（如访问路径、故障点、配置位置）。
不要臆测图中不存在的信息；不确定的项可省略。不要输出 JSON。"""

_PERCEIVE_PROMPT = """你是企业IT服务台的会话前置识别助手。请对用户这句话同时完成两件事，只输出一个 JSON 对象，不要任何多余文字：

1) 判断用户来意 intent，三选一：
   - "chitchat"：寒暄、致谢、道别、闲聊等没有实质诉求的话（如“你好”“谢谢”“咋样”“在吗”）
   - "it_issue"：描述或追问 IT 相关问题（VPN、密码、邮箱、软件、网络、系统、权限、报销系统、打印机等），也包括只发截图或只描述故障现象
   - "off_topic"：与 IT 无关的其他话题（如订外卖、问天气、问你是谁）

2) 判断用户当前情绪 emotion 与 level，其中 score 表示负面情绪强度（0 表示非常平静，1 表示极度不满）。

输出格式：
{"intent": "chitchat|it_issue|off_topic", "emotion": "positive|neutral|negative",
 "level": "愤怒|焦急|平静|满意|失望|疑惑", "score": 0~1,
 "reply": "仅当 intent 不是 it_issue 时填写的一句中文短回复：chitchat 友好回应并引导用户说出遇到的IT问题；off_topic 礼貌说明你只负责IT问题并引导。intent 为 it_issue 时该字段必须是空字符串"}

判断 intent 时以“是否有需要 IT 支持的实质诉求”为准，不要因为句子短就判成 chitchat。"""

# 会话前置识别的来意枚举（见 6.3）与失败回退
INTENT_ENUM = ("chitchat", "it_issue", "off_topic")
# 识别失败时按 it_issue 兜底：宁可照常检索，也不要因识别失败漏答真实问题
PERCEIVE_FALLBACK = {
    "intent": "it_issue", "emotion": "neutral", "level": "未知", "score": 0.0, "reply": "",
}
_REPLY_MAX_CHARS = 160


# ----------------------------------------------------------------------
# ASR：语音转文字
# ----------------------------------------------------------------------
def _asr_api_sync(audio_bytes: bytes, filename: str) -> str:
    if not (settings.ASR_BASE_URL and settings.ASR_MODEL and settings.asr_key):
        raise PerceptionError(
            "ASR 未配置：请设置 ASR_BASE_URL / ASR_MODEL / ASR_API_KEY"
            "（未单独配置 Key 时回退 SILICONFLOW_API_KEY）。"
        )
    client = OpenAI(base_url=settings.ASR_BASE_URL, api_key=settings.asr_key)
    resp = client.audio.transcriptions.create(
        model=settings.ASR_MODEL,
        file=(filename, audio_bytes),
        timeout=_REQUEST_TIMEOUT,
    )
    return (getattr(resp, "text", "") or "").strip()


def _asr_local_sync(audio_bytes: bytes, filename: str) -> str:
    try:
        import funasr  # noqa: F401
    except Exception as exc:
        raise PerceptionError(
            "本地 ASR 需要安装 funasr（pip install funasr），或改用 ASR_PROVIDER=api。"
        ) from exc

    from funasr import AutoModel  # type: ignore

    suffix = Path(filename).suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        temp_path = tmp.name
    try:
        model = AutoModel(model="paraformer-zh")
        result = model.generate(input=temp_path)
        if result and isinstance(result, list):
            return str(result[0].get("text", "")).strip()
        return ""
    except Exception as exc:
        raise PerceptionError(f"本地 ASR 执行失败：{exc}") from exc
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


async def process_voice(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """语音转文字（ASR）。

    识别失败一律抛 :class:`PerceptionError`（携带中文可读信息），**不返回错误文案**——
    否则调用方会把提示串误当成用户转写内容落库、送 LLM/检索。调用方（如
    ``session_flow.preflight``）捕获异常后提示用户重录；评测套件亦按失败计入。

    - ``ASR_PROVIDER=="api"``：走 OpenAI 兼容 /audio/transcriptions；
    - ``ASR_PROVIDER=="local"``：尝试 funasr。
    """
    if not audio_bytes:
        raise PerceptionError("音频内容为空，请重新录制。")

    provider = (settings.ASR_PROVIDER or "api").lower()
    try:
        if provider == "local":
            text = await asyncio.to_thread(_asr_local_sync, audio_bytes, filename)
        else:
            text = await asyncio.to_thread(_asr_api_sync, audio_bytes, filename)
    except PerceptionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("语音识别调用失败：%s", exc)
        raise PerceptionError(f"语音识别调用失败：{exc}（请检查网络或 ASR 服务配置）") from exc

    text = (text or "").strip()
    if not text:
        raise PerceptionError("语音识别未返回有效文本，请重新录制或改用文字输入。")
    return text


# ----------------------------------------------------------------------
# OCR（本地 PaddleOCR，可选）
# ----------------------------------------------------------------------
def _ocr_local_sync(image_bytes: bytes) -> str:
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:
        raise PerceptionError(
            "本地 OCR 需要安装 PaddleOCR（pip install paddleocr paddlepaddle），"
            "或设置 OCR_PROVIDER=api 复用 VLM。"
        ) from exc

    try:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        array = np.array(image)
        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

        lines: list[str] = []
        try:
            result = ocr.ocr(array, cls=True)
        except (AttributeError, TypeError):
            # PaddleOCR 3.x 的 predict 接口
            result = ocr.predict(array)
            for item in result or []:
                if isinstance(item, dict):
                    lines.extend(str(t) for t in item.get("rec_texts", []) or [])

        if not lines:
            for block in result or []:
                for line in block or []:
                    # 结构：[box, (text, score)]
                    if isinstance(line, (list, tuple)) and len(line) >= 2 and isinstance(
                        line[1], (list, tuple)
                    ):
                        lines.append(str(line[1][0]))
        return "\n".join(lines).strip()
    except PerceptionError:
        raise
    except Exception as exc:
        raise PerceptionError(f"本地 OCR 执行失败：{exc}") from exc


# ----------------------------------------------------------------------
# 视觉理解（VLM）
# ----------------------------------------------------------------------
def _vlm_sync(data_url: str, ocr_text: str) -> dict:
    if not (settings.VLM_BASE_URL and settings.VLM_MODEL and settings.vlm_key):
        raise PerceptionError(
            "视觉理解未配置：请设置 VLM_BASE_URL / VLM_MODEL / VLM_API_KEY"
            "（未单独配置 Key 时回退 SILICONFLOW_API_KEY / LLM_API_KEY）。"
        )
    client = OpenAI(base_url=settings.VLM_BASE_URL, api_key=settings.vlm_key)
    prompt = _VLM_PROMPT.format(
        ocr=ocr_text or "（本地 OCR 未提供文本，请自行识别图中文字）"
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
        timeout=_REQUEST_TIMEOUT,
    )
    content = ""
    if resp.choices:
        content = resp.choices[0].message.content or ""
    parsed = extract_json(content) or {}
    if not parsed:
        # 非 JSON 输出：整段文本作为描述兜底
        parsed = {"description": content.strip()}
    return parsed


def _describe_sync(data_url: str) -> str:
    if not (settings.VLM_BASE_URL and settings.VLM_MODEL and settings.vlm_key):
        raise PerceptionError(
            "知识库配图描述未配置：请设置 VLM_BASE_URL / VLM_MODEL / VLM_API_KEY"
            "（未单独配置 Key 时回退 SILICONFLOW_API_KEY / LLM_API_KEY）。"
        )
    client = OpenAI(base_url=settings.VLM_BASE_URL, api_key=settings.vlm_key)
    resp = client.chat.completions.create(
        model=settings.VLM_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _KB_IMAGE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        timeout=_REQUEST_TIMEOUT,
    )
    content = ""
    if resp.choices:
        content = resp.choices[0].message.content or ""
    return content.strip()


async def describe_knowledge_image(image_bytes: bytes) -> str:
    """为知识库配图生成一段客观描述（入库期调用，见《多模态 RAG 文本优先改造方案》）。

    描述用于把图片内容转成文本，写入唯一文本集合参与检索与作答（图片原图不参与检索）。
    VLM 未配置或调用失败时**返回空串**，由调用方回退页正文，绝不阻断入库。
    """
    if not image_bytes:
        return ""
    try:
        data, _ = await asyncio.to_thread(
            downsample,
            image_bytes,
            settings.EMBEDDING_MAX_IMAGE_SIDE,
            settings.EMBEDDING_MAX_IMAGE_BYTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("配图描述降采样失败，使用原图：%s", exc)
        data = image_bytes
    try:
        return await asyncio.to_thread(_describe_sync, build_data_url(data))
    except PerceptionError as exc:
        logger.warning("配图描述跳过：%s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("配图描述调用异常：%s", exc)
        return ""


async def process_image(image_bytes: bytes) -> dict:
    """图片处理：OCR + 视觉理解（结构化截图诊断）。

    返回字段：``ocr_text / description / error_code / service / hint / error_type``。
    """
    result: dict[str, Any] = {
        "ocr_text": "",
        "description": "",
        "error_code": "",
        "service": "",
        "hint": "",
        "error_type": "",
    }
    if not image_bytes:
        result["error_type"] = "系统"
        result["hint"] = "图片内容为空。"
        result["description"] = "未能读取图片内容。"
        return result

    # 1. 降采样（与入库口径一致：像素 + 字节双约束），并转为 data URL
    try:
        data, _ = await asyncio.to_thread(
            downsample,
            image_bytes,
            settings.EMBEDDING_MAX_IMAGE_SIDE,
            settings.EMBEDDING_MAX_IMAGE_BYTES,
        )
    except Exception as exc:
        logger.warning("图片降采样失败，使用原图：%s", exc)
        data = image_bytes
    data_url = build_data_url(data)

    # 2. OCR（local 时先本地 OCR；api 或本地不可用时由 VLM 完成）
    ocr_text = ""
    ocr_provider = (settings.OCR_PROVIDER or "local").lower()
    if ocr_provider == "local":
        try:
            ocr_text = await asyncio.to_thread(_ocr_local_sync, data)
        except PerceptionError as exc:
            logger.warning("本地 OCR 不可用，回退 VLM OCR：%s", exc)
        except Exception as exc:
            logger.warning("本地 OCR 异常，回退 VLM OCR：%s", exc)

    # 3. 视觉理解
    try:
        vlm = await asyncio.to_thread(_vlm_sync, data_url, ocr_text)
    except PerceptionError as exc:
        logger.error("视觉理解失败：%s", exc)
        result["ocr_text"] = ocr_text
        result["description"] = f"视觉理解失败：{exc}"
        result["error_type"] = "系统"
        result["hint"] = "请检查视觉理解服务配置。"
        return result
    except Exception as exc:
        logger.error("视觉理解调用异常：%s", exc)
        result["ocr_text"] = ocr_text
        result["description"] = f"视觉理解失败：{exc}"
        result["error_type"] = "网络"
        result["hint"] = "请检查网络或视觉理解服务可用性。"
        return result

    result["ocr_text"] = ocr_text or str(vlm.get("ocr_text", "") or "")
    result["description"] = str(vlm.get("description", "") or "")
    result["error_code"] = str(vlm.get("error_code", "") or "")
    result["service"] = str(vlm.get("service", "") or "")
    result["hint"] = str(vlm.get("hint", "") or "")

    error_type = str(vlm.get("error_type", "") or "")
    if error_type not in ("网络", "权限", "软件", "系统"):
        error_type = "系统" if not error_type else error_type
    result["error_type"] = error_type
    return result


# ----------------------------------------------------------------------
# 会话前置识别（来意 + 情绪）
# ----------------------------------------------------------------------
def _perceive_sync(text: str, audio_features: Optional[dict]) -> dict:
    if not (settings.LLM_BASE_URL and settings.LLM_MODEL and settings.llm_key):
        raise PerceptionError(
            "会话前置识别未配置：请设置 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY"
            "（未单独配置 Key 时回退 SILICONFLOW_API_KEY）。"
        )
    client = OpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.llm_key)
    user_content = f"用户文本：{text}"
    if audio_features:
        user_content += f"\n语音特征（参考）：{json.dumps(audio_features, ensure_ascii=False)}"
    resp = client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": _PERCEIVE_PROMPT},
            {"role": "user", "content": user_content},
        ],
        timeout=_REQUEST_TIMEOUT,
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return extract_json(content or "") or {}


async def perceive_message(text: str, audio_features: Optional[dict] = None) -> dict:
    """会话前置识别（见 6.3）：**一次 LLM 调用**同时产出「来意 intent」与「情绪 emotion」。

    - ``intent`` 决定走知识库 RAG 还是短回复，并作为置信度护栏的前置条件；
    - ``intent`` 非 ``it_issue`` 时，同一次调用直接产出回复正文 ``reply``，
      避免为了回一句寒暄再多跑一次 LLM；
    - 失败（未配置/超时/JSON 解析不出）回退 ``intent="it_issue"``。

    返回 ``{"intent","emotion","level","score","reply"}``。
    """
    if not text or not text.strip():
        return dict(PERCEIVE_FALLBACK)
    try:
        data = await asyncio.to_thread(_perceive_sync, text, audio_features)
    except PerceptionError as exc:
        logger.error("会话前置识别失败：%s", exc)
        return dict(PERCEIVE_FALLBACK)
    except Exception as exc:  # noqa: BLE001
        logger.error("会话前置识别调用异常：%s", exc)
        return dict(PERCEIVE_FALLBACK)

    intent = str(data.get("intent") or "").strip().lower()
    if intent not in INTENT_ENUM:
        intent = "it_issue"

    emotion = str(data.get("emotion") or "neutral").lower()
    if emotion not in ("positive", "neutral", "negative"):
        emotion = "neutral"
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    reply = str(data.get("reply") or "").strip()[:_REPLY_MAX_CHARS]
    if intent == "it_issue":
        reply = ""
    return {
        "intent": intent,
        "emotion": emotion,
        "level": str(data.get("level") or "未知"),
        "score": score,
        "reply": reply,
    }


# ----------------------------------------------------------------------
# TTS：文字转语音（edge-tts）
# ----------------------------------------------------------------------
def _tts_sync(text: str, voice: str) -> bytes:
    try:
        import edge_tts
    except Exception as exc:
        raise PerceptionError(
            "TTS 依赖 edge-tts 未安装（pip install edge-tts）。"
        ) from exc

    async def _run() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            temp_path = tmp.name
        try:
            await communicate.save(temp_path)
            return Path(temp_path).read_bytes()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return asyncio.run(_run())


async def synthesize_speech(text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
    """文字转语音，返回 ``(mp3_bytes, "mp3")``。失败抛 :class:`PerceptionError`。"""
    if not text or not text.strip():
        raise PerceptionError("语音合成失败：待合成文本为空。")
    chosen_voice = voice or settings.TTS_VOICE
    try:
        audio = await asyncio.to_thread(_tts_sync, text, chosen_voice)
    except PerceptionError:
        raise
    except Exception as exc:
        logger.error("语音合成失败：%s", exc)
        raise PerceptionError(f"语音合成失败：{exc}") from exc
    return audio, "mp3"


__all__ = [
    "PerceptionError",
    "process_voice",
    "process_image",
    "describe_knowledge_image",
    "perceive_message",
    "synthesize_speech",
    "INTENT_ENUM",
]
