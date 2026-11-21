"""同步/异步桥：让 CrewAI 的同步 Tool 能调用中台的 async 能力。

CrewAI 的 ``crew.kickoff()`` 在 worker 线程中执行，Tool 的 ``_run`` 也是同步的；
而 RAG 检索、MCP 调用等都是 async。这里统一把协程投递回**主事件循环**执行
（在应用 lifespan 启动时用 set_main_loop 注册），避免各线程各自 asyncio.run
造成事件循环爆炸，也让 MCP 桥的常驻连接保持在同一循环内。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在应用启动时注册主事件循环。"""
    global _main_loop
    _main_loop = loop


def run_sync(coro: Coroutine[Any, Any, T], timeout: float = 120.0) -> T:
    """在同步上下文中执行协程。

    - 若已注册主事件循环且它正在运行，则投递到主循环执行（推荐路径）；
    - 否则在当前线程用 ``asyncio.run`` 兜底。
    """
    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    return asyncio.run(coro)
