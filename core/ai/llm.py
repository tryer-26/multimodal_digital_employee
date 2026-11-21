"""中台直连 LLM（OpenAI 兼容）的轻量封装。

用于不经过 CrewAI 的辅助能力：转人工摘要、AI 辅助推荐、工单优先级/分类判定等。
走与 CrewAI 相同的「能力四元组」配置（LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import AsyncIterator, Iterator, Optional

from core.config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 调用失败。"""


def _client():
    if not settings.LLM_BASE_URL or not settings.LLM_MODEL or not settings.llm_key:
        raise LLMError("LLM 未配置：请设置 LLM_MODEL / LLM_BASE_URL 与 API Key。")
    from openai import OpenAI

    return OpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.llm_key, timeout=60)


def chat_sync(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 1200,
) -> str:
    """同步多轮对话补全，返回纯文本。"""
    client = _client()
    try:
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM 调用失败：%s", exc)
        raise LLMError(f"LLM 调用失败：{exc}") from exc
    if not resp.choices:
        return ""
    return (resp.choices[0].message.content or "").strip()


async def chat(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 1200,
) -> str:
    return await asyncio.to_thread(chat_sync, messages, temperature, max_tokens)


def chat_stream_sync(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 1200,
) -> Iterator[str]:
    """同步流式补全，逐块产出增量文本。"""
    client = _client()
    try:
        stream = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM 流式调用失败：%s", exc)
        raise LLMError(f"LLM 流式调用失败：{exc}") from exc
    try:
        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                yield text
    finally:
        # 消费方提前退出（如用户点击「停止」）时关闭底层 HTTP 流，及时释放连接
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


async def chat_stream(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 1200,
) -> AsyncIterator[str]:
    """异步流式补全：在线程中驱动同步 SDK，经队列逐块产出，避免阻塞事件循环。

    同步 SDK 无原生异步流式接口，这里用「工作线程 + asyncio.Queue」桥接；
    消费方被取消（用户停止生成）时，通过 stop 事件让工作线程尽快收尾并关闭流。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    sentinel = object()

    def worker() -> None:
        try:
            for piece in chat_stream_sync(messages, temperature, max_tokens):
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, piece)
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()


def extract_json(text: str) -> Optional[dict]:
    """从模型输出中容错提取 JSON 对象。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(candidate[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def chat_json(prompt: str, system: str = "", temperature: float = 0.2, max_tokens: int = 800) -> Optional[dict]:
    """要求模型返回 JSON 并容错解析。"""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        text = await chat(messages, temperature=temperature, max_tokens=max_tokens)
    except LLMError:
        return None
    return extract_json(text)


def available() -> bool:
    return bool(settings.LLM_BASE_URL and settings.LLM_MODEL and settings.llm_key)
