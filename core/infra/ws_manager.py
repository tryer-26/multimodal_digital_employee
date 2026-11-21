"""WebSocket 连接管理与广播（见设计文档 12.7 / 10.2）。

按角色维护连接：用户 / 坐席 / 管理员，互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# 同账号在别处登录时，旧连接被踢下线的关闭码（应用自定义码段）。
# 前端收到 4409 后停止自动重连并提示，避免「旧端被踢→重连→踢掉新端」的互踢死循环
WS_CLOSE_KICKED = 4409


class WSManager:
    def __init__(self) -> None:
        self.user_connections: dict[str, WebSocket] = {}   # user_id -> ws
        self.agent_connections: dict[str, WebSocket] = {}  # agent_id -> ws
        self.admin_connections: list[WebSocket] = []

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------
    async def register_user(self, user_id: str, ws: WebSocket) -> None:
        old = self.user_connections.get(user_id)
        if old is not None and old is not ws:
            await self._safe_close(old, code=WS_CLOSE_KICKED, reason="账号已在别处登录")
        self.user_connections[user_id] = ws

    def unregister_user(self, user_id: str, ws: WebSocket) -> None:
        """仅当映射里仍是这条连接时才移除。

        同一 user 重连时 register_user 会关闭旧连接，旧连接的 finally 随后触发本方法；
        若按 user_id 无条件 pop，会把刚注册的新连接误删，导致该用户之后所有推送
        （bot_reply / agent_message）被静默丢弃，而客户端连接看似正常、不会重连。
        """
        if self.user_connections.get(user_id) is ws:
            self.user_connections.pop(user_id, None)

    async def register_agent(self, agent_id: str, ws: WebSocket) -> None:
        old = self.agent_connections.get(agent_id)
        if old is not None and old is not ws:
            await self._safe_close(old, code=WS_CLOSE_KICKED, reason="账号已在别处登录")
        self.agent_connections[agent_id] = ws

    def unregister_agent(self, agent_id: str, ws: WebSocket) -> None:
        """同 unregister_user：避免旧连接关闭时误删新连接。"""
        if self.agent_connections.get(agent_id) is ws:
            self.agent_connections.pop(agent_id, None)

    async def register_admin(self, ws: WebSocket) -> None:
        if ws not in self.admin_connections:
            self.admin_connections.append(ws)

    def unregister_admin(self, ws: WebSocket) -> None:
        if ws in self.admin_connections:
            self.admin_connections.remove(ws)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def _safe_send(self, ws: WebSocket, message: dict) -> bool:
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebSocket 发送失败（连接可能已断开）：%s", exc)
            return False

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        ws = self.user_connections.get(user_id)
        if ws is None:
            return False
        return await self._safe_send(ws, message)

    async def broadcast_to_agents(self, message: dict) -> None:
        for ws in list(self.agent_connections.values()):
            await self._safe_send(ws, message)

    async def broadcast_to_admins(self, message: dict) -> None:
        for ws in list(self.admin_connections):
            await self._safe_send(ws, message)

    async def broadcast_stats(self, stats: dict) -> None:
        """向管理端推送看板实时数据。"""
        await self.broadcast_to_admins({"type": "stats_update", "data": stats})

    async def _safe_close(self, ws: WebSocket, code: int = 1000, reason: str = "") -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


ws_manager = WSManager()


class GenerationRegistry:
    """按会话记录正在执行的 AI 回合任务，支持用户「停止生成」。

    每个用户消息回合在后台任务中执行；前端点「停止」时按 conversation_id
    定位并取消该任务，从而中断流式生成、不再推送后续增量与最终回复。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, conv_id: str, task: asyncio.Task) -> None:
        self._tasks[conv_id] = task

    def cancel(self, conv_id: str) -> bool:
        """取消指定会话正在执行的回合；返回是否确实取消了任务。"""
        task = self._tasks.get(conv_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def discard(self, conv_id: str, task: Optional[asyncio.Task] = None) -> None:
        """回合结束后移除登记。

        必须传回该回合自己的 task：上一回合的 done 回调可能在新回合 register 之后才执行
        （如 stream_end 已推送、但 trigger_handoff/统计推送仍在收尾），若按 conv_id 无条件
        pop，会把新回合的 task 误删，导致新回合「停止」失效。
        """
        if task is None or self._tasks.get(conv_id) is task:
            self._tasks.pop(conv_id, None)


generation_registry = GenerationRegistry()
