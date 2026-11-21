"""WebSocket 接口（见设计文档 10.1 / 10.2 / 12.7）。

本模块提供三个 WebSocket 端点，是平台的实时通道（HTTP 只能「一问一答」，
而聊天消息、AI 流式输出、队列变化都需要服务端主动推送，因此用 WebSocket 长连接）：
  - /ws/user  ：用户端，收发消息、接收 AI 流式回复、请求「停止生成」；
  - /ws/agent ：坐席端，收发人工消息、接收队列与状态变更广播；
  - /ws/admin ：管理端，接收数据看板实时刷新。

鉴权与分组：三个端点进入时先做「握手鉴权」（见 _authenticate），
按角色校验通过后加入不同的广播组（user / agent / admin），
后续服务端可向某个组批量推送消息（见 core.infra.ws_manager）。

WebSocket 关键概念（对零基础）：
  - 握手鉴权：浏览器建立 WS 连接时无法自定义请求头，因此 token 通过 URL 查询参数
    ?token=xxx 传入，连上后第一件事就是校验；不通过则用自定义关闭码关闭连接。
  - 关闭码：4401 表示未登录（token 无效/缺失），4403 表示无权限（角色不符）。
    4400 段为应用自定义关闭码，避免与标准 1000/1001 等语义冲突。
  - 消息循环：`while True: await ws.receive_json()` 持续接收客户端发来的 JSON 消息，
    按消息里的 type 字段分发处理；连接断开时 receive_json 会抛 WebSocketDisconnect。
  - 后台任务：AI 回复耗时较长，用 asyncio.create_task 丢到后台执行，
    从而不阻塞消息循环（否则生成期间用户无法再发消息）；
    任务句柄存入 generation_registry，用于实现「停止生成」。
  - finally 注销：无论正常断开还是异常，都必须把连接从注册表里移除，
    否则会给已断开的连接推送消息、并造成内存泄漏。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.service import session_flow, session_router
from core.infra.auth import get_current_user
from core.infra.db import db
from core.infra.media import media_file
from core.infra.ws_manager import generation_registry, ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


async def _authenticate(ws: WebSocket, roles: tuple[str, ...]) -> Optional[dict]:
    """握手鉴权：校验 token 与角色，通过返回用户字典，否则关闭连接并返回 None。

    调用方：三个 ws 端点进入时的第一件事。
    - token 从 URL 查询参数取（WebSocket 无法像 HTTP 那样自定义请求头）；
    - 未登录：close(code=4401)；
    - 角色不在允许列表：close(code=4403)。
    注意：此时尚未 accept()，直接 close 即为拒绝握手；调用方据返回 None 提前 return。
    """
    token = ws.query_params.get("token", "")
    user = await get_current_user(token)
    if not user:
        await ws.close(code=4401)
        return None
    if user["role"] not in roles:
        await ws.close(code=4403)
        return None
    return user


async def _resolve_media(asset_id: Optional[str]):
    """由 asset_id 取本地文件路径与二进制（非路由函数）。

    返回 (path, bytes)；asset_id 为空或文件不存在时返回 (None, None)；
    读取发生 OSError（如文件被删）时返回 (path, None)，即只有路径没有字节。
    """
    if not asset_id:
        return None, None
    path = media_file(asset_id)
    if path is None:
        return None, None
    try:
        return str(path), path.read_bytes()
    except OSError:
        return str(path), None


# ----------------------------------------------------------------------
# 用户端
# ----------------------------------------------------------------------
@router.websocket("/ws/user")
async def ws_user(ws: WebSocket) -> None:
    """用户端 WebSocket 端点（仅 user 角色）。

    握手：_authenticate 校验 token 与角色（失败关闭码 4401 / 4403）。
    连上后：accept 并注册到用户广播组，进入消息循环，按消息 type 分发：
      - user_message：先校验会话存在且属于本人，然后后台启动一回合 AI 处理
        （asyncio.create_task），把任务句柄登记到 generation_registry 以支持「停止生成」；
        并用 add_done_callback 在任务结束时自动注销句柄，避免注册表无限增长。
      - stop：取消该会话正在执行的 AI 任务（含流式生成），回送 generation_stopped。
      - switch：返回该会话当前模式（mode_changed），供前端刷新展示。
      - ping：心跳，回 pong。
    断开：捕获 WebSocketDisconnect 正常退出，其它异常仅告警不抛出；
          finally 中注销连接，释放广播组里的引用。
    """
    user = await _authenticate(ws, ("user",))
    if user is None:
        return
    await ws.accept()
    await ws_manager.register_user(user["id"], ws)
    try:
        while True:
            payload = await ws.receive_json()
            msg_type = payload.get("type")
            conv_id = payload.get("conversation_id")
            data = payload.get("data") or {}

            if msg_type == "user_message" and conv_id:
                conversation = await db.get_conversation(conv_id)
                if not conversation or conversation["user_id"] != user["id"]:
                    await ws.send_json({"type": "system", "conversation_id": conv_id,
                                        "data": {"role": "system", "content": "会话不存在"}})
                    continue

                image_asset = _asset_from_url(data.get("image_url"))
                audio_asset = _asset_from_url(data.get("audio_url"))
                image_path, _ = await _resolve_media(image_asset)
                _, audio_bytes = await _resolve_media(audio_asset)

                # 后台执行 AI 回合，保持 WebSocket 可继续收消息；句柄存入注册表以支持「停止生成」
                # 交由 session_flow 编排（内部按 CREW_ENABLED 切换直连流式 / Crew 编排）。
                task = asyncio.create_task(
                    session_flow.start(
                        conv_id=conv_id,
                        user_id=user["id"],
                        content=data.get("content", "") or "",
                        modality=data.get("modality", "text"),
                        image_path=image_path,
                        audio_bytes=audio_bytes,
                        image_asset=image_asset,
                        audio_asset=audio_asset,
                    )
                )
                generation_registry.register(conv_id, task)
                task.add_done_callback(lambda t, cid=conv_id: generation_registry.discard(cid, t))
            elif msg_type == "stop" and conv_id:
                # 用户点击「停止」：取消该会话正在执行的 AI 回合（含流式生成）
                cancelled = generation_registry.cancel(conv_id)
                await ws.send_json(
                    {"type": "generation_stopped", "conversation_id": conv_id,
                     "data": {"cancelled": cancelled}}
                )
            elif msg_type == "switch" and conv_id:
                conversation = await db.get_conversation(conv_id)
                mode = (conversation or {}).get("mode", "bot")
                await ws.send_json({"type": "mode_changed", "conversation_id": conv_id,
                                    "data": {"mode": mode}})
            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("用户 WebSocket 异常：%s", exc)
    finally:
        ws_manager.unregister_user(user["id"], ws)


# ----------------------------------------------------------------------
# 坐席端
# ----------------------------------------------------------------------
@router.websocket("/ws/agent")
async def ws_agent(ws: WebSocket) -> None:
    """坐席端 WebSocket 端点（仅 agent 角色）。

    握手：_authenticate 校验 token 与角色（失败关闭码 4401 / 4403）。
    连上后：accept 并注册到坐席广播组；首次建档时默认置 online，并按事实重算接待数；
            随后立即推送一次 queue_update，让工作台一进来就拿到最新队列。
    消息循环按 type 分发：
      - agent_message：坐席发送人工回复；
      - join：坐席接入会话；
      - status：切换在线/忙碌/离线，并广播给其他坐席；
      - ping：心跳，回 pong。
    断开：finally 中注销连接，并按事实重算接待数（会话仍留在坐席名下，计数不会凭空归零）。
    """
    user = await _authenticate(ws, ("agent",))
    if user is None:
        return
    await ws.accept()
    await ws_manager.register_agent(user["id"], ws)
    # 状态由坐席自己切换，连接时不改写（否则「忙碌/离线」会被重连覆盖）；
    # 仅在首次建档时给出默认在线，并按事实重算接待数（顺带修正历史漂移，见 6.8）
    if not await db.get_agent_profile(user["id"]):
        await db.set_agent_status(user["id"], "online")
    await db.sync_agent_current_count(user["id"])
    await ws.send_json({"type": "queue_update", "data": {"queue": await _queue_payload()}})
    try:
        while True:
            payload = await ws.receive_json()
            msg_type = payload.get("type")
            conv_id = payload.get("conversation_id")
            data = payload.get("data") or {}

            if msg_type == "agent_message" and conv_id:
                await session_router.agent_reply(conv_id, user["id"], (data.get("content") or "").strip())
            elif msg_type == "join" and conv_id:
                await session_router.agent_join(conv_id, user["id"])
            elif msg_type == "status":
                status = data.get("status")
                if status in ("online", "busy", "offline"):
                    await db.set_agent_status(user["id"], status)
                    await ws_manager.broadcast_to_agents(
                        {"type": "agent_status", "data": {"agent_id": user["id"], "status": status}}
                    )
            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("坐席 WebSocket 异常：%s", exc)
    finally:
        ws_manager.unregister_agent(user["id"], ws)
        # 掉线时按事实重算接待数：会话仍在坐席名下（见 6.8「服务中会话保留」），
        # 因此计数不会凭空归零，但也不会再像增量维护那样只增不减。
        try:
            await db.sync_agent_current_count(user["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("掉线重算坐席接待数失败：%s", exc)


async def _queue_payload() -> list[dict]:
    """组装待接入队列的推送载荷（非路由函数，供坐席连接与广播复用）。"""
    waiting = await db.list_waiting_human()
    queue: list[dict] = []
    for conv in waiting:
        queue.append(
            {
                "id": conv["id"],
                "user_name": conv.get("user_name") or conv.get("user_id", ""),
                "title": conv.get("title") or "新会话",
                "handoff_reason": conv.get("handoff_reason") or "",
                "last_message_at": conv.get("last_message_at"),
                "mode": conv.get("mode"),
                "status": conv.get("status"),
            }
        )
    return queue


# ----------------------------------------------------------------------
# 管理端
# ----------------------------------------------------------------------
@router.websocket("/ws/admin")
async def ws_admin(ws: WebSocket) -> None:
    """管理端 WebSocket 端点（仅 admin 角色）。

    握手：_authenticate 校验 token 与角色（失败关闭码 4401 / 4403）。
    连上后：accept 并注册到管理端广播组，立即推送一次 stats_update（数据看板快照）。
    消息循环：ping 回 pong；refresh 重新计算并推送一次 stats_update。
    断开：finally 中注销连接。
    """
    user = await _authenticate(ws, ("admin",))
    if user is None:
        return
    await ws.accept()
    await ws_manager.register_admin(ws)
    from core.service.analytics import snapshot_for_ws

    await ws.send_json({"type": "stats_update", "data": await snapshot_for_ws()})
    try:
        while True:
            payload = await ws.receive_json()
            if payload.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif payload.get("type") == "refresh":
                await ws.send_json({"type": "stats_update", "data": await snapshot_for_ws()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("管理端 WebSocket 异常：%s", exc)
    finally:
        ws_manager.unregister_admin(ws)


def _asset_from_url(url: Optional[str]) -> Optional[str]:
    """从媒体 URL 中截取末尾的 asset_id（非路由函数）。

    例如 "/api/media/img-1234" -> "img-1234"；url 为空时返回 None。
    """
    if not url:
        return None
    return url.rstrip("/").split("/")[-1]
