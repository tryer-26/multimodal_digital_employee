"""坐席端接口（见设计文档 12.4）。

本模块是「坐席端（人工客服）路由文件」，挂载在 /api/agent 前缀下，供坐席工作台使用：
  - 会话接续：GET /queue（待接入队列）、GET /sessions（我的服务中会话）、
              GET /session/{id}（会话详情）、GET /session/{id}/assist（AI 摘要+建议）；
  - 服务动作：POST /session/{id}/join、/reply、/return_to_ai、/end_service；
  - 坐席状态：GET/POST /api/agent/status。

鉴权说明：本模块端点均要求登录，并通过 require_role 限制角色：
  - 多数端点要求 agent；队列与会话详情允许 agent / admin（管理员可旁听/巡检）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.service import handoff as handoff_mod
from core.service import session_router
from core.infra.db import db
from core.infra.middleware import require_role

router = APIRouter(prefix="/api/agent", tags=["agent"])

# AI 摘要 + 辅助推荐缓存：key=会话 ID，value=(消息数, 结果)。
# 该结果含多次 LLM / 向量检索调用（数秒级），但内容只由对话历史决定；
# 坐席来回切换会话时若每次重算，既慢又白烧 token。消息数变化即失效。
# 进程内缓存：与 ws_manager 一致，中台为单进程（ChromaDB 嵌入式独占）。
_assist_cache: dict[str, tuple[int, dict]] = {}


async def _session_items(rows: list[dict]) -> list[dict]:
    """把会话行组装成坐席端列表项（队列 / 服务中列表共用）。"""
    items: list[dict] = []
    for conv in rows:
        last_user = await db.query_one(
            "SELECT content, emotion FROM messages WHERE conversation_id=? AND role='user' "
            "ORDER BY id DESC LIMIT 1",
            (conv["id"],),
        )
        items.append(
            {
                "id": conv["id"],
                "user_name": conv.get("user_name") or conv.get("user_id", ""),
                "title": conv.get("title") or "新会话",
                "handoff_reason": conv.get("handoff_reason") or "",
                "last_message_at": conv.get("last_message_at"),
                "started_at": conv.get("started_at"),
                "mode": conv.get("mode"),
                "status": conv.get("status"),
                "emotion": (last_user or {}).get("emotion"),
                "last_message": (last_user or {}).get("content", "")[:60],
            }
        )
    return items


def _ensure_viewable(conversation: dict, user: dict) -> None:
    """校验当前用户是否有权查看该会话。

    管理员不受限；坐席仅可查看「分配给自己的」或「仍在待接入队列中」的会话
    （队列会话需可见，坐席才能看摘要/建议后决定是否接入）。
    无权访问一律按 404 处理，避免用错误码探测会话是否存在。
    """
    if user.get("role") == "admin":
        return
    if conversation.get("assigned_agent_id") == user["id"]:
        return
    if conversation.get("mode") == "waiting_human" and not conversation.get("assigned_agent_id"):
        return
    raise HTTPException(status_code=404, detail="会话不存在")


@router.get("/queue")
async def get_queue(user: dict = Depends(require_role("agent", "admin"))) -> list[dict]:
    """待接入队列：等待人工接入的会话列表。

    调用方：坐席工作台队列区、管理员巡检。
    鉴权：agent / admin。
    返回：会话列表项（见 _session_items）。
    """
    return await _session_items(await db.list_waiting_human())


@router.get("/sessions")
async def get_my_sessions(user: dict = Depends(require_role("agent"))) -> list[dict]:
    """坐席「服务中」的会话列表。

    会话一旦进入 human 模式就不再出现在待接入队列里，而前端工作台状态仅在内存中；
    应用重启 / 页面刷新后必须靠本接口把未结束、未交回 AI 的会话取回来（见 6.8）。
    """
    return await _session_items(await db.list_agent_active_sessions(user["id"]))


@router.get("/session/{conv_id}")
async def get_session(conv_id: str, user: dict = Depends(require_role("agent", "admin"))) -> dict:
    """会话基本信息（对话历史）。

    刻意不含 AI 摘要与辅助建议：那两项各含一次 LLM 调用，合计约 8 秒；
    若塞在本接口里，坐席点击列表后要等 8 秒才有任何反馈，形同「点不动」。
    它们由 GET /session/{id}/assist 异步补齐（见 12.4）。
    """
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    _ensure_viewable(conversation, user)
    messages = await db.get_messages(conv_id, limit=200)
    return {"conversation": conversation, "messages": messages}


@router.get("/session/{conv_id}/assist")
async def get_session_assist(
    conv_id: str, user: dict = Depends(require_role("agent", "admin"))
) -> dict:
    """AI 摘要 + 辅助推荐（含 LLM 调用，耗时较长，单独拉取）。

    两项互相独立，用 gather 并行，延迟由「相加」变为「取较大者」；
    并按消息数缓存，坐席来回切换同一会话时秒回、不再重复调用模型。
    """
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    _ensure_viewable(conversation, user)

    count = await db.count_messages(conv_id)
    cached = _assist_cache.get(conv_id)
    if cached and cached[0] == count:
        return cached[1]

    summary, suggestions = await asyncio.gather(
        handoff_mod.generate_handoff_summary(
            conv_id, conversation.get("handoff_reason") or ""
        ),
        session_router.build_agent_suggestions(conv_id),
    )
    payload = {"summary": summary, "suggestions": suggestions}
    _assist_cache[conv_id] = (count, payload)
    return payload


@router.post("/session/{conv_id}/join")
async def join(conv_id: str, user: dict = Depends(require_role("agent"))) -> dict:
    """坐席接入某个待接入会话。

    调用方：坐席在工作台点击「接入」。
    鉴权：仅 agent 角色。
    错误码：400 —— 接入失败（如会话已被他人接入 / 状态不允许）。
    返回：session_router.agent_join 的结果。
    """
    result = await session_router.agent_join(conv_id, user["id"])
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "接入失败"))
    return result


class ReplyBody(BaseModel):
    """POST /session/{id}/reply 的请求体：坐席要发送的回复内容。"""

    content: str


@router.post("/session/{conv_id}/reply")
async def reply(conv_id: str, body: ReplyBody, user: dict = Depends(require_role("agent"))) -> dict:
    """坐席在会话中发送一条人工回复。

    调用方：坐席工作台输入框。
    入参：请求体 ReplyBody.content。
    鉴权：仅 agent 角色。
    错误码：400 —— 内容为空，或发送失败（如会话不在坐席名下）。
    返回：session_router.agent_reply 的结果。
    """
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="回复内容不能为空")
    result = await session_router.agent_reply(conv_id, user["id"], body.content.strip())
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "发送失败"))
    return result


class ReturnBody(BaseModel):
    """POST /session/{id}/return_to_ai 的请求体：交回 AI 时的备注（可选）。"""

    note: str = ""


@router.post("/session/{conv_id}/return_to_ai")
async def return_to_ai(conv_id: str, body: ReturnBody, user: dict = Depends(require_role("agent"))) -> dict:
    """把会话交回 AI 继续服务。

    调用方：坐席处理完后点击「交回 AI」（可附备注）。
    鉴权：仅 agent 角色。
    错误码：400 —— 操作失败（如会话不在坐席名下）。
    返回：session_router.return_to_ai 的结果。
    """
    result = await session_router.return_to_ai(conv_id, user["id"], body.note.strip())
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    return result


@router.post("/session/{conv_id}/end_service")
async def end_service(conv_id: str, user: dict = Depends(require_role("agent"))) -> dict:
    """结束坐席对某会话的服务（会话彻底结束，不再交回 AI）。

    调用方：坐席点击「结束服务」。
    鉴权：仅 agent 角色。
    返回：session_router.end_agent_service 的结果（内部自行判断失败情况）。
    """
    return await session_router.end_agent_service(conv_id, user["id"])


# ----------------------------------------------------------------------
# 坐席状态
# ----------------------------------------------------------------------
@router.get("/status")
async def get_status(user: dict = Depends(require_role("agent"))) -> dict:
    """查询当前坐席的状态与接待数。

    调用方：坐席工作台顶部的状态栏。
    鉴权：仅 agent 角色。
    返回：{"status", "current_count"}；未建档时 status 默认 offline。
    """
    profile = await db.get_agent_profile(user["id"]) or {}
    # 接待数按事实回算再返回：即便有改动绕过了应用（如后台直接改库），
    # 读取时也会自愈，「存储值 == 名下服务中会话数」这一不变量恒成立（见 6.8）
    count = await db.sync_agent_current_count(user["id"]) if profile else 0
    return {"status": profile.get("status", "offline"), "current_count": count}


class StatusBody(BaseModel):
    """POST /api/agent/status 的请求体：坐席状态字符串。"""

    status: str


@router.post("/status")
async def set_status(body: StatusBody, user: dict = Depends(require_role("agent"))) -> dict:
    """切换当前坐席的状态（在线/忙碌/离线）。

    调用方：坐席工作台状态切换控件。
    入参：请求体 StatusBody.status，只接受 online / busy / offline。
    鉴权：仅 agent 角色。
    错误码：400 —— status 取值非法。
    返回：{"ok": True, "status"}；并会广播 agent_status 事件给其他坐席，保持界面一致。
    """
    if body.status not in ("online", "busy", "offline"):
        raise HTTPException(status_code=400, detail="status 只能是 online / busy / offline")
    await db.set_agent_status(user["id"], body.status)
    await session_router._push_agents({"type": "agent_status", "data": {"agent_id": user["id"], "status": body.status}})
    return {"ok": True, "status": body.status}
