"""工单系统 MCP Server（IT Service Desk）。

本模块是 MDE-LAB 多模态数字员工平台**唯一的外部系统**，通过 MCP 协议对外
暴露 8 个工单相关工具，由中台（MCP Client / Host）以 stdio 方式拉起并调用。

设计依据：设计文档 3.2、5.1 ~ 5.6。
- 使用 FastMCP 提供工具注册与 stdio 服务；
- 数据落 SQLite（tickets 表，见 5.3）；
- user_id 直接使用中台 users.id（见 5.6），Server 侧不另建用户表。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import string
import uuid
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# FastMCP 导入：优先官方推荐的 fastmcp 包，不可用时回退到 mcp 内置实现
# ----------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境依赖
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP


# ----------------------------------------------------------------------
# 数据库路径
#   优先读取环境变量 TICKETS_DB_PATH；
#   未设置时默认 <项目根>/mcp_servers/it_service/tickets.db。
# ----------------------------------------------------------------------
DEFAULT_DB_PATH: Path = Path(__file__).resolve().parent / "tickets.db"

# 建表语句（幂等），字段与设计文档 5.3 一致
SCHEMA: str = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT DEFAULT 'medium',
    category TEXT DEFAULT 'general',
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# 合法枚举（见设计文档 5.5）
PRIORITIES: frozenset[str] = frozenset({"high", "medium", "low"})
CATEGORIES: frozenset[str] = frozenset(
    {"network", "password", "software", "finance", "email", "general"}
)
STATUSES: frozenset[str] = frozenset({"open", "in_progress", "resolved", "closed"})

# FastMCP 实例
mcp: FastMCP = FastMCP("IT Service Desk")


def _resolve_db_path() -> Path:
    """解析当前生效的 SQLite 路径（运行时读取环境变量，便于测试注入）。"""
    env_path = os.environ.get("TICKETS_DB_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    """建立一个独立的 SQLite 连接（父目录不存在则创建）。

    每个工具调用各自建连、用完即关，避免跨线程复用（check_same_thread 默认）。
    """
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> Path:
    """幂等建表：确保 tickets 表存在，供中台启动时预建库。

    Returns:
        当前生效的数据库文件路径（Path）。
    """
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db_path


@mcp.tool()
def create_ticket(
    title: str,
    description: str,
    priority: str = "medium",
    category: str = "general",
    user_id: str = "",
) -> str:
    """创建一个 IT 服务工单。

    Args:
        title: 工单标题（必填）。
        description: 问题描述。
        priority: 优先级，枚举 high/medium/low，默认 medium；非法值回退 medium。
        category: 分类，枚举 network/password/software/finance/email/general，
            默认 general；非法值回退 general。
        user_id: 发起工单的用户 id（中台 users.id，见 5.6），可为空。

    Returns:
        形如 "工单已创建：TICKET-xxxxxxxx" 的文本；若入参枚举非法，
        会在结果末尾附带回退说明。
    """
    notes: list[str] = []

    if priority not in PRIORITIES:
        notes.append(f"优先级 '{priority}' 非法，已回退为 medium")
        priority = "medium"
    if category not in CATEGORIES:
        notes.append(f"分类 '{category}' 非法，已回退为 general")
        category = "general"

    ticket_id = f"TICKET-{uuid.uuid4().hex[:8]}"

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tickets "
            "(id, user_id, title, description, priority, category) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, user_id, title, description, priority, category),
        )
        conn.commit()
    finally:
        conn.close()

    result = f"工单已创建：{ticket_id}"
    if notes:
        result += "（" + "；".join(notes) + "）"
    return result


@mcp.tool()
def query_ticket(ticket_id: str) -> str:
    """根据工单号查询工单详情。

    Args:
        ticket_id: 工单编号，形如 TICKET-xxxxxxxx。

    Returns:
        JSON 字符串：命中时返回工单全部字段；未命中时返回
        {"found": false, "message": "未找到工单：..."}。
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return json.dumps(
            {"found": False, "message": f"未找到工单：{ticket_id}"},
            ensure_ascii=False,
        )
    payload: dict[str, Any] = dict(row)
    payload["found"] = True
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def list_user_tickets(user_id: str) -> str:
    """查询指定用户的所有工单，按创建时间倒序。

    Args:
        user_id: 用户 id（中台 users.id）。

    Returns:
        JSON 数组字符串；无工单时返回空数组 "[]"。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
def list_tickets(
    status: str = "",
    priority: str = "",
    category: str = "",
    keyword: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """管理端：按条件分页查询**全部**工单，按创建时间倒序。

    Args:
        status: 按状态过滤（open/in_progress/resolved/closed），空串表示不限。
        priority: 按优先级过滤（high/medium/low），空串表示不限。
        category: 按分类过滤，空串表示不限。
        keyword: 关键词，模糊匹配工单号 / 标题 / 描述，空串表示不限。
        limit: 每页条数，取值 1~100（越界自动收敛），默认 20。
        offset: 偏移量，默认 0。

    Returns:
        JSON 字符串：{"total": 命中总数, "items": [工单对象, ...]}。
    """
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if priority:
        where.append("priority = ?")
        params.append(priority)
    if category:
        where.append("category = ?")
        params.append(category)
    if keyword:
        where.append("(id LIKE ? OR title LIKE ? OR description LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    conn = _connect()
    try:
        total: int = conn.execute(
            f"SELECT COUNT(*) FROM tickets{clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM tickets{clause} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return json.dumps(
        {"total": total, "items": [dict(r) for r in rows]}, ensure_ascii=False
    )


@mcp.tool()
def delete_ticket(ticket_id: str) -> str:
    """删除工单（管理端使用）。

    Args:
        ticket_id: 工单编号，形如 TICKET-xxxxxxxx。

    Returns:
        JSON 字符串：成功时 {"ok": true, "id": ...}；
        工单不存在返回 {"ok": false, "message": "未找到工单：..."}。
    """
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return json.dumps(
                {"ok": False, "message": f"未找到工单：{ticket_id}"},
                ensure_ascii=False,
            )
    finally:
        conn.close()

    return json.dumps({"ok": True, "id": ticket_id}, ensure_ascii=False)


@mcp.tool()
def update_ticket_status(ticket_id: str, status: str) -> str:
    """更新工单状态，并同步刷新 updated_at。

    Args:
        ticket_id: 工单编号。
        status: 目标状态，枚举 open/in_progress/resolved/closed。

    Returns:
        JSON 字符串：成功时 {"ok": true, "ticket": {...}}；
        非法状态返回 {"ok": false, "message": ...}；
        工单不存在返回 {"ok": false, "message": "未找到工单：..."}。
    """
    if status not in STATUSES:
        return json.dumps(
            {
                "ok": False,
                "message": f"非法状态：{status}，可选值："
                f"{sorted(STATUSES)}",
            },
            ensure_ascii=False,
        )

    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE tickets SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, ticket_id),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return json.dumps(
                {"ok": False, "message": f"未找到工单：{ticket_id}"},
                ensure_ascii=False,
            )

        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        ticket: dict[str, Any] = dict(row) if row is not None else {}
    finally:
        conn.close()

    return json.dumps({"ok": True, "ticket": ticket}, ensure_ascii=False)


@mcp.tool()
def reset_password(user_id: str) -> str:
    """为用户重置密码，返回一次性临时密码。

    Args:
        user_id: 用户 id（中台 users.id）。

    Returns:
        JSON 字符串：{"user_id": ..., "temp_password": "8位随机",
        "expires_in_minutes": 30}。
    """
    alphabet = string.ascii_letters + string.digits
    temp_password = "".join(secrets.choice(alphabet) for _ in range(8))
    return json.dumps(
        {
            "user_id": user_id,
            "temp_password": temp_password,
            "expires_in_minutes": 30,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def request_software_access(user_id: str, software_name: str) -> str:
    """提交软件权限申请。

    Args:
        user_id: 用户 id（中台 users.id）。
        software_name: 申请的软件名称。

    Returns:
        JSON 字符串：{"request_id": "REQ-xxxxxxxx", "user_id": ...,
        "software": ..., "status": "submitted"}。
    """
    request_id = f"REQ-{uuid.uuid4().hex[:8]}"
    return json.dumps(
        {
            "request_id": request_id,
            "user_id": user_id,
            "software": software_name,
            "status": "submitted",
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    # 启动前先确保库表存在，再以 stdio 方式运行 MCP 服务
    ensure_db()
    mcp.run()
