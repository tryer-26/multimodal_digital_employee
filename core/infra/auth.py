"""认证：Token + Session（见设计文档 11.4）。

⚠️ 演示项目取舍：sha256 直接哈希，无加盐无慢哈希，禁止用于生产。

【这个文件在系统里的角色】
负责"你是谁"这一件事：把用户名密码换成 token，并在后续请求里用 token 还原出用户。
本项目的鉴权是"服务端会话式"：登录成功后把 token 存进 sessions 表，
之后每次请求带上 token，服务端查表判断是否有效——比 JWT 更直观，也方便随时踢下线。

【为什么用 sha256 是"取舍"而不是"最佳实践"】
真实生产应使用带盐、且计算故意很慢的算法（如 bcrypt/argon2），让攻击者难以用彩虹表
或暴力枚举破解。这里为了教学"零依赖、看得懂"，只用标准库 sha256，明文等价性一目了然，
但安全性不足，故严禁用于生产。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from core.config import settings
from core.infra.db import db


def hash_password(password: str) -> str:
    """把明文密码哈希成固定长度的十六进制字符串。

    参数：
        password: 明文密码。
    返回：
        64 位十六进制字符串（sha256 结果）。
    说明：
        同样的输入永远得到同样的输出（确定性），所以登录时可以用"再算一遍、比对字符串"
        的方式校验密码，而不必存明文。
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与已存哈希是否匹配。

    参数：
        password: 用户输入的明文密码。
        password_hash: 数据库里存的哈希值。
    返回：
        匹配返回 True，否则 False。
    """
    return hash_password(password) == password_hash


async def login(username: str, password: str) -> dict:
    """登录：校验账号密码，成功则签发 token 并落库。

    参数：
        username: 登录名。
        password: 明文密码。
    返回：
        成功时返回含 token/user_id/username/role/display_name 的 dict；
        失败时返回 {"error": "..."}（用统一结构代替抛异常，方便路由层直接判断）。

    边界与异常：
        - 用户不存在 或 密码不匹配：都返回"用户名或密码错误"（故意不区分，防止枚举用户名）；
        - 账号被禁用（status != active）：返回禁用提示，即使密码正确也不放行。
    """
    user = await db.get_user_by_username(username)
    if not user or user["password_hash"] != hash_password(password):
        return {"error": "用户名或密码错误"}
    # 密码对了还要看账号是否被禁用：禁用账号一律拒绝登录。
    if user["status"] != "active":
        return {"error": "账号已被禁用，请联系管理员"}

    # secrets.token_urlsafe 生成密码学安全的随机 token（不可预测，适合做凭证）。
    token = secrets.token_urlsafe(32)
    # 过期时间 = 现在 + 配置的小时数；服务端会据此判断会话是否失效。
    expires_at = datetime.now() + timedelta(hours=settings.TOKEN_EXPIRE_HOURS)
    await db.create_session(
        token=token, user_id=user["id"], role=user["role"], expires_at=expires_at
    )
    await db.set_last_login(user["id"])
    return {
        "token": token,
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "display_name": user["display_name"],
    }


async def get_current_user(token: str) -> Optional[dict]:
    """用 token 还原出当前用户；无效或过期返回 None。

    参数：
        token: 请求携带的登录令牌。
    返回：
        用户 dict；token 缺失/不存在/过期/用户不存在时为 None。

    说明：
        这是"鉴权"的核心：任何需要登录的接口都会调用它。过期会话会被顺手删除（惰性清理），
        避免 sessions 表无限膨胀。
    """
    if not token:
        return None
    session = await db.get_session(token)
    if not session:
        return None
    try:
        expires_at = datetime.fromisoformat(session["expires_at"])
    except (TypeError, ValueError):
        # 过期时间字段损坏（脏数据）时按无效处理，避免抛异常打挂接口。
        return None
    if expires_at < datetime.now():
        # 过期：删除该会话并视为未登录。
        await db.delete_session(token)
        return None
    return await db.get_user(session["user_id"])


async def logout(token: str) -> None:
    """登出：删除服务端会话，使该 token 立即失效。"""
    await db.delete_session(token)
