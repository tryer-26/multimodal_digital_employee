"""管理端接口（见设计文档 12.5 / 11.3）。

本模块是「管理端路由文件」，挂载在 /api/admin 前缀下，供管理员后台使用，按业务分为六组：
  - 数据看板：GET /dashboard；
  - 对话日志：GET /logs、GET /logs/{id}、DELETE /logs/{id}；
  - 工单管理：GET /tickets、GET /tickets/{id}、PUT /tickets/{id}/status、DELETE /tickets/{id}；
  - 用户管理：GET/POST /users、PUT /users/{id}、PUT /users/{id}/password、DELETE /users/{id}；
  - 工具调用日志：GET /tools、DELETE /tools/{id}；
  - 知识库管理：GET /knowledge、POST /knowledge/upload、DELETE /knowledge/{id}、
    GET /knowledge/{id}/chunks、POST /knowledge/rebuild、POST /knowledge/test_search、
    POST /knowledge/seed、GET /knowledge/stats；
  - 全栈评测：GET /eval/suites、POST /eval/run、GET /eval/runs、
    GET /eval/runs/{id}、DELETE /eval/runs/{id}、POST /eval/runs/{id}/baseline。

鉴权说明：本文件所有端点都要求 admin 角色（Depends(require_role("admin"))），
是权限最高的一组接口；写操作大多会写审计日志（logger.info 记录管理员与操作对象）。

FastAPI / 业务概念速览：
  - Form(...)：接收 multipart 表单里的普通字段（如知识库上传的分类 category）；
  - File(...) + UploadFile：接收上传文件；
  - 文件限制：接受格式见 ALLOWED_EXT，单文件大小受 settings.KB_MAX_FILE_MB 约束，
    文档总数受 settings.KB_MAX_DOCUMENTS 约束；
  - 工单数据不在本进程，而是经 MCP 桥（bridge）调用工单系统的工具，故失败时返回 503。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core.service import analytics
from core.infra.auth import hash_password
from core.config import settings
from core.infra.db import db
from core.service.handoff import CATEGORY_ENUM
from core.infra.media import media_file
from core.infra.mcp_bridge import bridge
from core.infra.middleware import require_role
from core.rag.multimodal_rag import multimodal_rag
from core.rag.vector_store import vector_store
from eval import runner as eval_runner
from eval import store as eval_store
from eval.suites import DEFAULT_ORDER, TITLES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

ALLOWED_EXT = {".md", ".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg"}
# 演示依赖的默认账号，不允许删除（前端据此置灰删除按钮）
PROTECTED_USERNAMES = ("user01", "agent01", "admin")
# 工单状态枚举（与工单 MCP Server 的 STATUSES 一致）
TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")


def _knowledge_dir() -> Path:
    """返回知识库源文件目录 ./data/knowledge，不存在则递归创建（非路由函数）。"""
    directory = Path("./data/knowledge")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ----------------------------------------------------------------------
# 数据看板
# ----------------------------------------------------------------------
@router.get("/dashboard")
async def dashboard(days: int = 7, user: dict = Depends(require_role("admin"))) -> dict:
    """数据看板：近 N 天的运营统计。

    调用方：管理端首页看板。
    入参：days（查询参数，默认 7 天）。
    鉴权：仅 admin。
    返回：analytics.dashboard 汇总的统计字典。
    """
    return await analytics.dashboard(days=days)


# ----------------------------------------------------------------------
# 对话日志
# ----------------------------------------------------------------------
@router.get("/logs")
async def logs(
    mode: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(require_role("admin")),
) -> dict:
    """对话日志列表（筛选 + 分页），分页口径与工单管理一致。

    调用方：管理端「对话日志」页。
    入参：mode / keyword（筛选）、page / page_size（分页）。
    鉴权：仅 admin。
    返回：{"total", "items"}；page 至少为 1，page_size 被夹在 1~100；
          每条记录附带 message_count（消息数）与 is_handoff（是否转人工）。
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    total = await db.count_conversations(mode=mode, keyword=keyword)
    conversations = await db.list_all_conversations(
        limit=page_size, offset=(page - 1) * page_size, mode=mode, keyword=keyword
    )
    items: list[dict] = []
    for conv in conversations:
        count = await db.count_messages(conv["id"])
        items.append(
            {
                **conv,
                "message_count": count,
                "is_handoff": bool(conv.get("handoff_reason")),
            }
        )
    return {"total": total, "items": items}


@router.get("/logs/{conv_id}")
async def log_detail(conv_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """对话日志详情：会话 + 消息 + 工具调用记录。

    调用方：管理端点击某条日志。
    鉴权：仅 admin。
    错误码：404 —— 会话不存在。
    返回：{"conversation", "messages"(最近 500 条), "tool_calls"(最多 100 条)}。
    """
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "conversation": conversation,
        "messages": await db.get_messages(conv_id, limit=500),
        "tool_calls": await db.list_tool_calls(limit=100, conv_id=conv_id),
    }


@router.delete("/logs/{conv_id}")
async def delete_log(conv_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """删除对话日志（会话及其消息、工具调用记录）。

    调用方：管理端日志列表的删除操作。
    鉴权：仅 admin。
    错误码：404 —— 会话不存在。
    返回：{"deleted": True, "id"}；并写审计日志记录执行删除的管理员。
    """
    if not await db.get_conversation(conv_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.delete_conversation(conv_id)
    logger.info("管理员 %s 删除会话 %s", user["id"], conv_id)
    return {"deleted": True, "id": conv_id}


# ----------------------------------------------------------------------
# 工单管理（经 MCP 读写工单系统，见 5.x）
# ----------------------------------------------------------------------
def _ticket_unavailable() -> HTTPException:
    """构造「工单系统不可用」的 503 异常（非路由函数，多处复用）。

    统一在这里拼 detail，避免每个工单端点重复写一遍。
    """
    return HTTPException(
        status_code=503, detail=f"工单系统不可用：{bridge.error or '未连接'}"
    )


@router.get("/tickets")
async def list_tickets(
    status: str = "",
    priority: str = "",
    category: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(require_role("admin")),
) -> dict:
    """工单列表（条件筛选 + 分页），数据来自工单 MCP Server。

    调用方：管理端「工单管理」页。
    入参：status / priority / category / keyword 筛选 + page / page_size 分页。
    鉴权：仅 admin。
    错误码：503 —— 工单系统未连接或查询异常。
    返回：{"total", "items"}；page_size 被夹在 1~100。
    """
    if not bridge.available:
        raise _ticket_unavailable()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    try:
        text = await bridge.call_tool(
            "list_tickets",
            {
                "status": status,
                "priority": priority,
                "category": category,
                "keyword": keyword,
                "limit": page_size,
                "offset": (page - 1) * page_size,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"工单查询失败：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    return {"total": data.get("total", 0), "items": data.get("items", [])}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """工单详情。

    调用方：管理端工单列表点击某工单。
    鉴权：仅 admin。
    错误码：404 —— 工单不存在；503 —— 工单系统不可用或查询异常。
    返回：工单系统返回的工单数据。
    """
    if not bridge.available:
        raise _ticket_unavailable()
    try:
        text = await bridge.call_tool("query_ticket", {"ticket_id": ticket_id})
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"工单查询失败：{exc}") from exc
    if not data.get("found"):
        raise HTTPException(status_code=404, detail=data.get("message") or "工单不存在")
    return data


class TicketStatusBody(BaseModel):
    """PUT /tickets/{id}/status 的请求体：目标状态。"""

    status: str


@router.put("/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: str, body: TicketStatusBody, user: dict = Depends(require_role("admin"))
) -> dict:
    """推进工单状态（open → in_progress → resolved → closed）。

    调用方：管理端工单操作。
    入参：请求体 TicketStatusBody.status。
    鉴权：仅 admin。
    错误码：400 —— 状态不在 TICKET_STATUSES 白名单；404 —— 工单不存在 / 更新失败；
            503 —— 工单系统不可用或更新异常。
    返回：工单系统返回的结果；并写审计日志。
    """
    if body.status not in TICKET_STATUSES:
        raise HTTPException(status_code=400, detail=f"非法状态：{body.status}")
    if not bridge.available:
        raise _ticket_unavailable()
    try:
        text = await bridge.call_tool(
            "update_ticket_status", {"ticket_id": ticket_id, "status": body.status}
        )
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"工单更新失败：{exc}") from exc
    if not data.get("ok"):
        raise HTTPException(status_code=404, detail=data.get("message") or "工单更新失败")
    logger.info("管理员 %s 将工单 %s 置为 %s", user["id"], ticket_id, body.status)
    return data


@router.delete("/tickets/{ticket_id}")
async def delete_ticket(ticket_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """删除工单（经工单 MCP Server）。

    调用方：管理端工单列表的删除操作。
    鉴权：仅 admin。
    错误码：404 —— 工单不存在或删除失败；503 —— 工单系统不可用或删除异常。
    返回：工单系统返回的结果；并写审计日志。
    """
    if not bridge.available:
        raise _ticket_unavailable()
    try:
        text = await bridge.call_tool("delete_ticket", {"ticket_id": ticket_id})
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"工单删除失败：{exc}") from exc
    if not data.get("ok"):
        raise HTTPException(status_code=404, detail=data.get("message") or "工单不存在")
    logger.info("管理员 %s 删除工单 %s", user["id"], ticket_id)
    return data


# ----------------------------------------------------------------------
# 用户管理
# ----------------------------------------------------------------------
@router.get("/users")
async def list_users(user: dict = Depends(require_role("admin"))) -> list[dict]:
    """用户列表。

    调用方：管理端「用户管理」页。
    鉴权：仅 admin。
    返回：用户列表；每条都会剔除 password_hash（绝不外泄密码哈希），
          并附 protected 标记（默认账号，前端据此置灰删除按钮）。
    """
    users = await db.list_users()
    for item in users:
        item.pop("password_hash", None)
        # 默认账号标记为受保护，前端据此置灰删除按钮（与后端删除校验同一来源）
        item["protected"] = item.get("username") in PROTECTED_USERNAMES
    return users


class CreateUserBody(BaseModel):
    """POST /users 的请求体：新建用户字段；password 默认 123456，role 默认 user。"""

    username: str
    password: str = "123456"
    role: str = "user"
    display_name: str = ""
    department: str = ""


@router.post("/users")
async def create_user(body: CreateUserBody, user: dict = Depends(require_role("admin"))) -> dict:
    """新建用户。

    调用方：管理端「用户管理」页的新增用户。
    入参：请求体 CreateUserBody。
    鉴权：仅 admin。
    错误码：400 —— 角色不在 user / agent / admin 之内，或用户名已存在。
    返回：{"id"}；密码经 hash_password 哈希后再入库，明文不落库。
    """
    if body.role not in ("user", "agent", "admin"):
        raise HTTPException(status_code=400, detail="角色只能是 user / agent / admin")
    if await db.get_user_by_username(body.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    user_id = await db.create_user(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        display_name=body.display_name or body.username,
        department=body.department,
    )
    return {"id": user_id}


class UpdateUserBody(BaseModel):
    """PUT /users/{id} 的请求体：可选更新字段，None 表示不修改。"""

    display_name: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None
    department: Optional[str] = None


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserBody, user: dict = Depends(require_role("admin"))) -> dict:
    """更新用户资料（显示名 / 角色 / 状态 / 部门）。

    调用方：管理端用户编辑。
    入参：请求体 UpdateUserBody；model_dump(exclude_none=True) 只提交非空字段，
          未填写的字段保持原值不变。
    鉴权：仅 admin。
    错误码：404 —— 用户不存在。
    返回：{"ok": True}。
    """
    if not await db.get_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.update_user(user_id, **body.model_dump(exclude_none=True))
    return {"ok": True}


class UpdatePasswordBody(BaseModel):
    """PUT /users/{id}/password 的请求体：新密码明文。"""

    password: str


@router.put("/users/{user_id}/password")
async def update_password(
    user_id: str, body: UpdatePasswordBody, user: dict = Depends(require_role("admin"))
) -> dict:
    """管理员直接为用户设置新密码（替代原「重置为随机临时密码」）。

    鉴权：仅 admin。
    错误码：400 —— 密码长度不足 6 位；404 —— 用户不存在。
    返回：{"ok": True}；新密码经 hash_password 哈希后入库。
    """
    if not await db.get_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少 6 位")
    await db.update_user(user_id, password_hash=hash_password(body.password))
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """删除用户（级联清理其会话/消息/工具调用/登录会话/坐席档案）；默认账号禁止删除。

    鉴权：仅 admin。
    错误码：400 —— 目标是默认账号（PROTECTED_USERNAMES）；404 —— 用户不存在。
    返回：{"deleted": True, "id"}；并写审计日志。
    """
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.get("username") in PROTECTED_USERNAMES:
        raise HTTPException(status_code=400, detail="默认账号不允许删除")
    await db.delete_user(user_id)
    logger.info("管理员 %s 删除用户 %s", user["id"], user_id)
    return {"deleted": True, "id": user_id}


# ----------------------------------------------------------------------
# 工具调用日志
# ----------------------------------------------------------------------
@router.get("/tools")
async def tool_logs(
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(require_role("admin")),
) -> dict:
    """工具调用日志列表（分页），分页口径与工单管理一致。

    调用方：管理端「工具调用日志」页。
    鉴权：仅 admin。
    返回：{"total", "items"}；page_size 被夹在 1~100。
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    total = await db.count_tool_calls()
    items = await db.list_tool_calls(limit=page_size, offset=(page - 1) * page_size)
    return {"total": total, "items": items}


@router.delete("/tools/{tool_call_id}")
async def delete_tool_log(
    tool_call_id: int, user: dict = Depends(require_role("admin"))
) -> dict:
    """删除单条工具调用日志。

    鉴权：仅 admin。
    错误码：404 —— 记录不存在（删除返回 false）。
    返回：{"deleted": True, "id"}；并写审计日志。
    """
    if not await db.delete_tool_call(tool_call_id):
        raise HTTPException(status_code=404, detail="工具调用记录不存在")
    logger.info("管理员 %s 删除工具调用记录 %s", user["id"], tool_call_id)
    return {"deleted": True, "id": tool_call_id}


# ----------------------------------------------------------------------
# 知识库管理（见 7.6）
# ----------------------------------------------------------------------
@router.get("/knowledge")
async def list_knowledge(user: dict = Depends(require_role("admin"))) -> list[dict]:
    """知识库文档列表。

    调用方：管理端「知识库管理」页。
    鉴权：仅 admin。
    返回：文档列表；modalities（各模态分块数）以 JSON 字符串存储，此处解析为字典，
          解析失败时降级为空字典。
    """
    documents = await db.list_documents()
    for doc in documents:
        try:
            doc["modalities"] = json.loads(doc.get("modalities") or "{}")
        except json.JSONDecodeError:
            doc["modalities"] = {}
    return documents


@router.post("/knowledge/upload")
async def upload_knowledge(
    file: UploadFile = File(...),
    category: str = Form("general"),
    user: dict = Depends(require_role("admin")),
) -> dict:
    """上传知识库文档并入库（切分、向量化、多模态处理）。

    调用方：管理端知识库上传。
    入参：multipart 表单——file（必填，UploadFile）、category（普通字段，默认 general）。
    鉴权：仅 admin。
    校验：扩展名须在 ALLOWED_EXT 内；文件非空且不超过 settings.KB_MAX_FILE_MB；
          文档总数不超过 settings.KB_MAX_DOCUMENTS；category 不在 CATEGORY_ENUM 时回退 general。
    错误码：400（格式/空文件/超限/文档数上限）、500（入库失败）。
    返回：{"id", "filename", "chunk_count", "modalities"}。
    说明：同名文件会自动追加 _1、_2 后缀避免覆盖；入库失败时登记一条 failed 记录便于排查。
    """
    filename = Path(file.filename or "untitled").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"不支持的格式 {ext}，仅支持 md/txt/pdf/docx/png/jpg")
    if category not in CATEGORY_ENUM:
        category = "general"

    # 鉴权由 Depends(require_role("admin")) 在函数体之前完成；此处读文件时已确认管理员身份
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > settings.KB_MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"文件超过 {settings.KB_MAX_FILE_MB}MB 限制")
    if await db.count_documents() >= settings.KB_MAX_DOCUMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"知识库文档数已达上限 {settings.KB_MAX_DOCUMENTS} 篇，请先清理。",
        )

    directory = _knowledge_dir()
    target = directory / filename
    index = 1
    while target.exists():
        target = directory / f"{Path(filename).stem}_{index}{ext}"
        index += 1
    target.write_bytes(data)

    document_id = f"doc-{uuid.uuid4().hex[:8]}"
    try:
        result = await multimodal_rag.add_document(str(target), category=category, document_id=document_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识库入库失败")
        await db.upsert_document(
            document_id, target.name, category, {}, 0, len(data), status="failed"
        )
        raise HTTPException(status_code=500, detail=f"入库失败：{exc}") from exc

    return {
        "id": document_id,
        "filename": target.name,
        "chunk_count": result.get("chunk_count", 0),
        "modalities": result.get("modalities", {}),
    }


@router.delete("/knowledge/{doc_id}")
async def delete_knowledge(doc_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """删除知识库文档（向量分块 + 源文件）。

    调用方：管理端知识库删除。
    鉴权：仅 admin。
    错误码：404 —— 文档不存在。
    返回：{"ok": True, "removed_chunks"}。
    """
    document = await db.get_document(doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    removed = await multimodal_rag.delete_document(doc_id)
    # 只删该文档自己的源文件（精确文件名）；用前缀 glob 会误删同前缀的其它文件，
    # 例如删 vpn.md 会连带删掉 vpn_error_619.png。媒体资产在 data/media，不在此清理
    source = _knowledge_dir() / Path(document["filename"]).name
    if source.is_file():
        try:
            source.unlink()
        except OSError:
            pass
    return {"ok": True, "removed_chunks": removed}


@router.get("/knowledge/{doc_id}/chunks")
async def list_knowledge_chunks(
    doc_id: str, user: dict = Depends(require_role("admin"))
) -> list[dict]:
    """列出某文档的全部分块内容（见 7.6「查看分块内容」）。

    调用方：管理端查看某文档的切分结果。
    鉴权：仅 admin。
    错误码：404 —— 文档不存在。
    返回：分块列表；content 按 content / caption / context 依次回退取值，
          图片等模态附带 asset_id 与可访问 url。
    """
    document = await db.get_document(doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")

    payload: list[dict] = []
    for item in vector_store.list_by_document(doc_id):
        meta = item.get("metadata") or {}
        asset_id = meta.get("asset_id")
        payload.append(
            {
                "chunk_index": meta.get("chunk_index"),
                "modality": meta.get("modality"),
                "page": meta.get("page"),
                "content": meta.get("content") or meta.get("caption") or meta.get("context") or "",
                "asset_id": asset_id,
                "url": f"/api/media/{asset_id}" if asset_id else None,
            }
        )
    return payload


@router.post("/knowledge/rebuild")
async def rebuild_knowledge(user: dict = Depends(require_role("admin"))) -> dict:
    """重建整个知识库向量索引（force=True 强制全量重建）。

    调用方：管理端知识库维护操作（如更换嵌入模型后）。
    鉴权：仅 admin。
    错误码：500 —— 重建过程异常。
    返回：multimodal_rag.rebuild_index 的结果。
    """
    try:
        return await multimodal_rag.rebuild_index(force=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("重建索引失败")
        raise HTTPException(status_code=500, detail=f"重建索引失败：{exc}") from exc


class TestSearchBody(BaseModel):
    """POST /knowledge/test_search 的请求体。

    - query：检索文本（可为空，仅给图片时也能检索）；
    - mode：multimodal（多模态）或 text_only（仅文本）；
    - image_url：以图搜图的截图 URL（可选）。
    """

    query: str = ""
    mode: str = "multimodal"  # multimodal | text_only
    image_url: str = ""


@router.post("/knowledge/test_search")
async def test_search(body: TestSearchBody, user: dict = Depends(require_role("admin"))) -> list[dict]:
    """检索测试：多模态 vs 纯文本对比（见 7.6 核心演示点）。

    调用方：管理端知识库「检索测试」面板。
    入参：请求体 TestSearchBody。
    鉴权：仅 admin。
    校验：query 与截图至少提供一个，否则 400。
    错误码：400 —— 两者皆空；500 —— 检索异常。
    返回：命中列表；score 保留 4 位小数，content 截断到 300 字，
          图片分块附带 asset_id 与可访问 url。
    """
    query_image: Optional[bytes] = None
    if body.image_url:
        asset_id = body.image_url.rstrip("/").split("/")[-1]
        path = media_file(asset_id)
        if path is not None:
            query_image = path.read_bytes()
    if not body.query and query_image is None:
        raise HTTPException(status_code=400, detail="请提供 query 或上传截图")

    try:
        results = await multimodal_rag.search(
            query=body.query or None,
            query_image=query_image,
            text_only=(body.mode == "text_only"),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"检索失败：{exc}") from exc

    payload: list[dict] = []
    for item in results:
        meta = item.get("metadata") or {}
        asset_id = meta.get("asset_id")
        payload.append(
            {
                "score": round(float(item.get("score", 0.0)), 4),
                "modality": meta.get("modality"),
                "source": meta.get("source"),
                "page": meta.get("page"),
                "content": (meta.get("content") or meta.get("caption") or meta.get("context") or "")[:300],
                "asset_id": asset_id,
                "url": f"/api/media/{asset_id}" if asset_id else None,
            }
        )
    return payload


@router.post("/knowledge/seed")
async def seed_knowledge(user: dict = Depends(require_role("admin"))) -> dict:
    """加载演示知识库（合成数据，见第十六章隐私合规）。

    调用方：管理端一键装载 Demo 数据。
    鉴权：仅 admin。
    错误码：500 —— 播种失败。
    返回：seed_knowledge_base 的结果。
    """
    from core.service.seed_knowledge import seed_knowledge_base

    try:
        return await seed_knowledge_base()
    except Exception as exc:  # noqa: BLE001
        logger.exception("播种演示知识库失败")
        raise HTTPException(status_code=500, detail=f"播种知识库失败：{exc}") from exc


@router.get("/knowledge/stats")
async def knowledge_stats(user: dict = Depends(require_role("admin"))) -> dict:
    """知识库统计（文档数 + 向量库整体统计）。

    调用方：管理端知识库概览。
    鉴权：仅 admin。
    返回：{"documents": 文档数, **向量库统计}。
    """
    return {"documents": await db.count_documents(), **multimodal_rag.stats()}


# ----------------------------------------------------------------------
# 全栈评测（ASR / TTS / VLM / 检索）
# ----------------------------------------------------------------------
# 后台评测任务引用，避免被 GC 回收
_eval_tasks: set[asyncio.Task] = set()


@router.get("/eval/suites")
async def eval_suites(user: dict = Depends(require_role("admin"))) -> dict:
    """可用评测套件与限流参数（前端渲染勾选项）。

    调用方：管理端评测页初始化。
    鉴权：仅 admin。
    返回：{"suites": [{name, title}...], "max_cases": settings.EVAL_MAX_CASES}。
    """
    return {
        "suites": [{"name": name, "title": TITLES[name]} for name in DEFAULT_ORDER],
        "max_cases": settings.EVAL_MAX_CASES,
    }


class EvalRunBody(BaseModel):
    """POST /eval/run 的请求体：要运行的评测套件名列表。"""

    suites: list[str] = []


@router.post("/eval/run")
async def start_eval(body: EvalRunBody, user: dict = Depends(require_role("admin"))) -> dict:
    """发起一次评测：建记录后立即返回 run_id，实际执行在后台任务中进行。

    调用方：管理端点击「开始评测」。
    入参：请求体 EvalRunBody.suites（套件名列表）。
    鉴权：仅 admin。
    错误码：400 —— 套件名非法（create_run 抛 ValueError）。
    返回：{"id", "status", "suites"}。
    说明：用 asyncio.create_task 后台执行，句柄加入 _eval_tasks 防止被 GC 回收，
          任务结束后由回调自动移除；并写审计日志。
    """
    try:
        run = eval_runner.create_run(body.suites)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    task = asyncio.create_task(eval_runner.execute(run["id"]))
    _eval_tasks.add(task)
    task.add_done_callback(_eval_tasks.discard)
    logger.info("管理员 %s 发起评测 %s：%s", user["id"], run["id"], run["suites"])
    return {"id": run["id"], "status": run["status"], "suites": run["suites"]}


@router.get("/eval/runs")
async def list_eval_runs(limit: int = 50, user: dict = Depends(require_role("admin"))) -> list[dict]:
    """历史评测运行摘要（倒序）。

    调用方：管理端评测页历史列表。
    鉴权：仅 admin。
    入参：limit（被夹在 1~200）。
    返回：运行摘要列表，最近的在前。
    """
    return eval_store.list_runs(limit=max(1, min(limit, 200)))


@router.get("/eval/runs/{run_id}")
async def eval_run_detail(run_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """单次评测详情（含基线，供前端计算 Δ）。

    调用方：管理端点击某条评测记录。
    鉴权：仅 admin。
    错误码：404 —— 评测记录不存在。
    返回：{"run": 详情, "baseline": 基线}。
    """
    run = eval_store.load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="评测记录不存在")
    return {"run": run, "baseline": eval_store.load_baseline()}


@router.delete("/eval/runs/{run_id}")
async def delete_eval_run(run_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """删除一条已完成/失败的评测记录；运行中的记录不可删，占用的基线一并清理。

    调用方：管理端评测历史列表的删除操作。
    鉴权：仅 admin。
    错误码：400 —— 记录仍在运行；404 —— 记录不存在。
    返回：{"deleted": True, "id"}；若被删记录正是当前基线，则同时清除基线；并写审计日志。
    """
    run = eval_store.load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="评测记录不存在")
    if run.get("status") == "running":
        raise HTTPException(status_code=400, detail="运行中的评测不可删除，请等待其完成")
    eval_store.delete_run(run_id)
    base = eval_store.load_baseline()
    if base and base.get("run_id") == run_id:
        eval_store.clear_baseline()
    logger.info("管理员 %s 删除评测记录 %s", user["id"], run_id)
    return {"deleted": True, "id": run_id}


@router.post("/eval/runs/{run_id}/baseline")
async def set_eval_baseline(run_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """把某次评测的指标固化为基线，后续运行据此展示 Δ。

    调用方：管理端评测页「设为基线」。
    鉴权：仅 admin。
    错误码：400 —— 记录状态不是 done；404 —— 记录不存在。
    返回：{"ok": True, "run_id"}；并写审计日志。
    """
    run = eval_store.load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="评测记录不存在")
    if run.get("status") != "done":
        raise HTTPException(status_code=400, detail="仅已完成的评测可设为基线")
    eval_store.save_baseline(run)
    logger.info("管理员 %s 将评测 %s 设为基线", user["id"], run_id)
    return {"ok": True, "run_id": run_id}
