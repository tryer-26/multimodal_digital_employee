"""用户端接口（见设计文档 12.3）。

本模块是「用户端（求助者/员工）路由文件」，统一挂载在 /api 前缀下，端点按业务分为三组：
  - 会话：GET/POST /api/conversations、GET /api/conversations/{id}/messages、
          DELETE /api/conversations/{id}、POST /api/conversations/{id}/close；
  - 我的工单：GET /api/tickets（经 MCP 查询工单系统）；
  - 媒体：POST /api/media/upload（上传图片/语音）、GET /api/media/{asset_id}（鉴权访问）。

鉴权说明：所有端点均要求登录，并通过 require_role 限制角色：
  - 会话与工单类端点要求 user 角色；
  - 媒体上传/读取允许 user / agent / admin（坐席与管理员也需要查看图片、语音）。

FastAPI 概念速览：
  - Depends(require_role("user"))：依赖注入，进入函数前先校验「已登录 + 角色匹配」，
    不匹配时直接返回 401/403，函数体根本不会执行。
  - pydantic BaseModel：声明请求体字段与类型，FastAPI 自动解析并做校验。
  - UploadFile：封装上传文件的句柄（含 filename / content_type，可用 await read() 读字节）。
  - FileResponse：把磁盘上的文件流式返回给客户端，适合媒体资源下载。
  - HTTPException：抛出带状态码的错误响应，如 400 / 404 / 503。
"""
from __future__ import annotations

import json
import logging
import mimetypes
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.service import session_router
from core.config import settings
from core.infra.db import db
from core.infra.media import ext_from_mime, media_file, media_url, save_media
from core.infra.mcp_bridge import bridge
from core.infra.middleware import require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
@router.get("/conversations")
async def list_conversations(limit: int = 50, user: dict = Depends(require_role("user"))) -> list[dict]:
    """列出当前用户的历史会话。

    调用方：用户端左侧会话列表。
    入参：limit（查询参数，默认 50）。
    鉴权：Depends(require_role("user"))，仅 user 角色。
    返回：会话列表；按 user["id"] 过滤，用户只能看到自己的会话。
    """
    return await db.list_conversations(user["id"], limit=limit)


class CreateConversationBody(BaseModel):
    """POST /api/conversations 的请求体；title 可选，缺省时为 None。"""

    title: Optional[str] = None


@router.post("/conversations")
async def create_conversation(body: CreateConversationBody | None = None,
                              user: dict = Depends(require_role("user"))) -> dict:
    """新建一个会话。

    调用方：用户端点击「新建对话」。
    入参：可选请求体 CreateConversationBody（title）。
    鉴权：仅 user 角色。
    返回：新建的会话对象；若请求里带了 title，则补写标题后再返回。
    说明：请求体声明为可选（body 可为 None），因此不带 body 调用也合法。
    """
    conversation = await session_router.create_conversation(user["id"])
    if body and body.title:
        await db.update_conversation(conversation["id"], title=body.title)
        conversation["title"] = body.title
    return conversation


async def _owned_conversation(conv_id: str, user: dict) -> dict:
    """取会话并校验归属，供多个端点复用（非路由函数）。

    若会话不存在，或不属于当前用户，一律抛 404「会话不存在」。
    刻意不用 403：403 会暴露「该会话确实存在、只是你没权限」，
    攻击者可借此枚举出系统中真实存在的会话 ID；统一 404 更安全。
    """
    conversation = await db.get_conversation(conv_id)
    if not conversation or conversation["user_id"] != user["id"]:
        # 「不存在」与「不属于自己」都返回 404：统一错误码，避免用 403 反推出会话是否存在
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, user: dict = Depends(require_role("user"))) -> list[dict]:
    """读取某会话的历史消息。

    调用方：用户端切换到某会话时拉取聊天记录。
    path 参数：conv_id（会话 ID）。
    鉴权：仅 user 角色；先经 _owned_conversation 校验会话归属（否则 404）。
    返回：该会话最近 200 条消息。
    """
    await _owned_conversation(conv_id, user)
    return await db.get_messages(conv_id, limit=200)


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, user: dict = Depends(require_role("user"))) -> dict:
    """删除自己的某条历史会话（含消息与工具调用记录）；工单不在级联范围内。

    调用方：用户端会话列表的删除按钮。
    鉴权：仅 user 角色；先校验归属（非本人会话返回 404）。
    错误码：404 —— 会话不存在或不属于当前用户。
    返回：{"deleted": True, "id": conv_id}；并写日志记录执行删除的用户。
    """
    await _owned_conversation(conv_id, user)
    await db.delete_conversation(conv_id)
    logger.info("用户 %s 删除会话 %s", user["id"], conv_id)
    return {"deleted": True, "id": conv_id}


class CloseBody(BaseModel):
    """POST /api/conversations/{id}/close 的请求体；reason 默认 user_closed。"""

    reason: str = "user_closed"


@router.post("/conversations/{conv_id}/close")
async def close_conversation(conv_id: str, body: CloseBody,
                             user: dict = Depends(require_role("user"))) -> dict:
    """关闭（结束）某会话。

    调用方：用户端点击「结束会话」。
    入参：请求体 CloseBody.reason，只接受 "resolved"（已解决）或 "user_closed"（用户主动关闭）。
    鉴权：仅 user 角色；先校验归属（否则 404）。
    错误码：400 —— reason 非法；404 —— 会话不存在或不属于自己。
    返回：session_router.close_conversation 的结果。
    """
    await _owned_conversation(conv_id, user)
    if body.reason not in ("resolved", "user_closed"):
        raise HTTPException(status_code=400, detail="reason 只能是 resolved 或 user_closed")
    return await session_router.close_conversation(conv_id, body.reason)


# ----------------------------------------------------------------------
# 我的工单（经 MCP 查询工单系统）
# ----------------------------------------------------------------------
@router.get("/tickets")
async def my_tickets(user: dict = Depends(require_role("user"))) -> list[dict]:
    """查询当前用户名下的工单（数据来自工单 MCP Server）。

    调用方：用户端「我的工单」页面。
    鉴权：仅 user 角色。
    实现：通过 bridge.call_tool 跨进程调用工单系统的 list_user_tickets 工具，
          以 user["id"] 作为过滤条件，用户只能查到自己的工单。
    错误码：503 —— 工单系统不可用（bridge 未连接）或查询过程抛异常。
    返回：工单列表；若返回内容不是合法 JSON 或不是数组，则返回空列表。
    """
    if not bridge.available:
        raise HTTPException(status_code=503, detail=f"工单系统不可用：{bridge.error or '未连接'}")
    try:
        text = await bridge.call_tool("list_user_tickets", {"user_id": user["id"]})
    except Exception as exc:  # noqa: BLE001
        # 外部依赖（MCP 工单系统）异常统一映射为 503 Service Unavailable，
        # 与业务错误区分开，前端可据此提示「稍后重试」
        raise HTTPException(status_code=503, detail=f"工单查询失败：{exc}") from exc
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


# ----------------------------------------------------------------------
# 媒体
# ----------------------------------------------------------------------
@router.post("/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    user: dict = Depends(require_role("user", "agent", "admin")),
) -> dict:
    """上传媒体文件（图片或语音）。

    调用方：各端发送带图/语音消息前先上传。
    入参：multipart 表单字段 file（UploadFile），File(...) 表示该字段必填。
    鉴权：允许 user / agent / admin。
    错误码：400 —— 文件为空或超过 settings.KB_MAX_FILE_MB 大小限制。
    返回：{"asset_id", "url", "modality"}；modality 按 MIME 类型推断为 image / voice。
    """
    # 依赖注入已在进入函数体前完成登录与角色校验，因此此处读文件时调用者必定合法
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > settings.KB_MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"文件超过 {settings.KB_MAX_FILE_MB}MB 限制")

    content_type = file.content_type or ""
    if content_type.startswith("image/"):
        modality, prefix = "image", "img"
    elif content_type.startswith("audio/"):
        modality, prefix = "voice", "aud"
    else:
        modality, prefix = "image", "img"

    ext = ext_from_mime(content_type, default=(file.filename or "bin").rsplit(".", 1)[-1])
    asset_id = save_media(data, ext, prefix=prefix)
    return {"asset_id": asset_id, "url": media_url(asset_id), "modality": modality}


@router.get("/media/{asset_id}")
async def get_media(asset_id: str, user: dict = Depends(require_role("user", "agent", "admin"))):
    """按 asset_id 读取媒体文件（鉴权访问）。

    调用方：前端 <img>/<audio> 标签加载图片、语音。
    鉴权：允许 user / agent / admin（未登录无法直接访问媒体 URL）。
    错误码：404 —— asset_id 对应的文件不存在。
    返回：FileResponse，媒体类型按扩展名猜出（猜不到则用 application/octet-stream）。
    """
    path = media_file(asset_id)
    if path is None:
        raise HTTPException(status_code=404, detail="媒体不存在")
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)
