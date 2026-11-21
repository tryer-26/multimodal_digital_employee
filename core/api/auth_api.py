"""认证接口（见设计文档 11.4 / 11.6）。

本模块是「认证路由文件」，挂载在 /api/auth 前缀下，负责整个平台的登录入口与身份查询：
  - POST /api/auth/login  ：账号密码登录，换取访问令牌（token）；
  - POST /api/auth/logout ：退出登录，作废当前令牌；
  - GET  /api/auth/me     ：查询当前登录用户的身份信息；
  - POST /api/auth/init   ：初始化演示账号（要求 admin 身份，本地演示用）。

鉴权说明：除 /login 外，其余端点都要求在请求头携带 `Authorization: Bearer <token>`，
校验逻辑集中在 core.infra.middleware，通过 FastAPI 的「依赖注入」(Depends) 在进入函数体之前完成。

FastAPI 概念速览：
  - APIRouter：把一组相关的接口聚合成一个「路由表」，最终由 core/main.py 统一挂载；
    prefix 是这组接口共用的 URL 前缀，tags 只影响自动生成的接口文档分组。
  - Depends：依赖注入，声明「进入本函数前先执行某个函数」，用于统一做登录与角色校验。
  - pydantic BaseModel：用声明式的方式描述请求体（JSON）的字段与类型，
    FastAPI 会自动解析、类型转换，并在格式不合法时返回 422。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.infra.auth import login as do_login
from core.infra.auth import logout as do_logout
from core.infra.middleware import current_user_or_401, require_role
from core.infra.init_data import init_users

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    """POST /api/auth/login 的请求体模型。

    用 pydantic BaseModel 定义请求体，FastAPI 会自动把客户端发来的 JSON
    映射到这两个字段；若缺字段或类型不符，会在进入 login() 之前直接返回 422。

    - username：登录用户名；
    - password：明文密码（后端用哈希比对，绝不落库明文）。
    """

    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody) -> dict:
    """账号密码登录，成功后签发访问令牌。

    调用方：前端登录页（匿名调用，无需 token）。
    入参：请求体 LoginBody（username / password）。
    返回：do_login 的结果，通常包含 token 与用户基本信息。
    错误码：401 —— 用户名或密码错误（do_login 返回 error 时抛出）。
    """
    result = await do_login(body.username, body.password)
    if "error" in result:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(current_user_or_401)) -> dict:
    """退出登录，作废当前令牌。

    调用方：已登录用户端（前端「退出」按钮）。
    鉴权：Depends(current_user_or_401) —— 先校验登录态，未登录返回 401。
    入参：从请求头 Authorization 中取出 Bearer token（无请求体）。
    返回：{"ok": True}；已作废的 token 再次使用将无法通过鉴权。
    """
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    await do_logout(token)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user_or_401)) -> dict:
    """查询当前登录用户的身份信息。

    调用方：前端启动/刷新后据此判断登录状态与角色，进而渲染对应界面。
    鉴权：Depends(current_user_or_401)，未登录返回 401。
    返回：仅暴露展示所需字段（id/username/role/display_name/department/status），
          刻意不含 password_hash 等敏感字段。
    """
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "display_name": user["display_name"],
        "department": user.get("department"),
        "status": user.get("status"),
    }


@router.post("/init")
async def init(user: dict = Depends(require_role("admin"))) -> dict:
    """初始化演示账号（⚠️ 仅本地演示执行，见 11.4 安全警示）。

    启动时 lifespan 已自动差量补种（见 core/main.py），此端点仅供管理员手动重跑；
    要求 admin 身份，避免未认证者借其创建或探测账号。
    """
    return await init_users()
