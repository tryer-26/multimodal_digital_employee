"""权限校验中间件（见设计文档 11.5）。

【这个文件在系统里的角色】
FastAPI 的接口鉴权入口：从请求里取出 token → 校验出用户 → 检查角色是否允许访问。
它不改变请求/响应的数据，而是以"依赖（Depends）"的方式被路由函数引用，起到了
"进入接口前先过一道安检"的作用。

【关键概念扫盲：依赖注入与"依赖工厂"】
FastAPI 支持把"公共前置逻辑"写成依赖：路由函数声明 `user = Depends(xxx)`，框架会在
调用路由前先执行 xxx、把返回值注入给 user。若 xxx 里抛 HTTPException，请求就直接返回错误、
不会进入路由体。
本文件的 require_role(...) 是一个"依赖工厂"：调用它本身不校验、而是返回一个闭包 checker，
真正被 Depends 使用的是那个 checker。好处是可以提前把"允许哪些角色"传进去，
为不同接口"定制"出不同的校验依赖，例如 Depends(require_role("admin"))。
"""
from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request

from core.infra.auth import get_current_user


def _extract_token(request: Request) -> str:
    """从请求里提取 token。

    参数：
        request: 当前请求对象。
    返回：
        提取到的 token 字符串；没有则返回空串。

    说明：
        优先读标准的 Authorization: Bearer <token> 请求头（浏览器/前端常规用法）；
        因为浏览器的 WebSocket 与 <a> 下载无法自定义请求头，这里退而支持用
        query 参数 ?token= 传递。
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # WebSocket / 下载场景允许 query 传 token
    return request.query_params.get("token", "")


async def current_user_or_401(request: Request) -> dict:
    """校验登录态，未登录则抛 401。

    参数：
        request: 当前请求对象。
    返回：
        已登录的用户 dict。
    异常：
        未登录/登录失效时抛 HTTPException(401)。
    说明：
        校验通过后把用户挂到 request.state.user 上，后续同一请求内的其他代码
        都能直接取到当前用户，无需重复校验。
    """
    user = await get_current_user(_extract_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    request.state.user = user
    return user


def require_role(*allowed_roles: str) -> Callable:
    """角色校验依赖。用法：user = Depends(require_role("admin"))"""

    # 这个内层函数才是真正被 FastAPI 当作依赖执行的"checker"；
    # 它记住（闭包）了外层传入的 allowed_roles，因此不同接口能拥有各自的角色白名单。
    async def checker(request: Request) -> dict:
        # 先确认已登录（401），再看角色是否在允许列表内（403）。
        user = await current_user_or_401(request)
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return checker
