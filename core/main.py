"""MDE-LAB 多模态数字员工平台 —— 中台服务入口。

启动顺序（见设计文档 17.3 启动自检 / 13 项目结构）：
1. 配置自检（fail-fast，可通过 MDE_SKIP_CONFIG_CHECK=1 降级为告警）；
2. 初始化业务库与演示账号（差量补种）；
3. 预建工单库并拉起 MCP 常驻连接；
4. 启动定时任务（会话超时关闭、数据保留清理）；
5. 挂载 REST / WebSocket 路由与前端静态资源（如已构建）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.api import admin_api, agent_api, auth_api, chat_api, ws_api
from core.infra.async_bridge import set_main_loop
from core.config import ConfigError, settings, validate_config
from core.infra.db import db
from core.infra.init_data import init_users
from core.infra.mcp_bridge import bridge
from core.infra.scheduler import shutdown_scheduler, start_scheduler
from eval.store import recover_stale_runs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("mde-lab")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 注册主事件循环，供 CrewAI 同步工具回调 async 能力
    set_main_loop(asyncio.get_running_loop())

    # 1. 配置自检
    try:
        for warning in validate_config(check_embedding=True):
            logger.warning("配置告警：%s", warning)
    except ConfigError as exc:
        if os.getenv("MDE_SKIP_CONFIG_CHECK") == "1":
            logger.error("配置自检未通过（已按 MDE_SKIP_CONFIG_CHECK=1 跳过）：\n%s", exc)
        else:
            logger.critical("配置自检未通过，服务拒绝启动：\n%s", exc)
            raise

    # 2. 业务库与演示账号（差量补种）
    await db.init()
    result = await init_users()
    logger.info("演示账号初始化：新建 %s，已存在 %s", result["created"] or "无", result["skipped"] or "无")

    # 3. 媒体目录 + 工单库 + MCP 常驻连接
    settings.media_dir().mkdir(parents=True, exist_ok=True)
    tickets_db = settings.TICKETS_DB_PATH or str(
        PROJECT_ROOT / "mcp_servers" / "it_service" / "tickets.db"
    )
    os.environ["TICKETS_DB_PATH"] = tickets_db
    try:
        from mcp_servers.it_service.server import ensure_db

        logger.info("工单库就绪：%s", ensure_db())
    except Exception as exc:  # noqa: BLE001
        logger.warning("预建工单库失败（工单能力可能不可用）：%s", exc)

    await bridge.start()
    try:
        await bridge.wait_ready(timeout=20)
        if bridge.available:
            logger.info("工单 MCP Server 连接成功，工具数：%d", len(bridge.tool_schemas()))
        else:
            logger.error("工单 MCP Server 不可用：%s", bridge.error)
    except asyncio.TimeoutError:
        logger.error("等待工单 MCP Server 就绪超时。")

    # 4. 定时任务
    start_scheduler()

    # 4.1 收敛上次进程中断遗留的评测运行（running → failed），避免永久卡死
    try:
        recovered = recover_stale_runs()
        if recovered:
            logger.warning("已恢复 %d 条中断的评测运行（标记为 failed）", recovered)
    except Exception as exc:  # noqa: BLE001
        logger.warning("评测运行恢复失败：%s", exc)

    logger.info("MDE-LAB 中台已就绪：http://localhost:8000")
    try:
        yield
    finally:
        shutdown_scheduler()
        await bridge.stop()
        logger.info("MDE-LAB 中台已停止。")


app = FastAPI(title="MDE-LAB 多模态数字员工平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 路由 ----
app.include_router(auth_api.router)
app.include_router(chat_api.router)
app.include_router(agent_api.router)
app.include_router(admin_api.router)
app.include_router(ws_api.router)


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "llm_configured": bool(settings.LLM_MODEL and settings.LLM_BASE_URL and settings.llm_key),
        "mcp_available": bridge.available,
        "mcp_error": bridge.error,
        "mcp_tools": [t.get("name") for t in bridge.tool_schemas()],
    }


# ---- 前端静态资源（如已构建；开发期由 Vite 3000 端口提供）----
_dist = PROJECT_ROOT / "frontend" / "dist"
if _dist.exists():
    if (_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 顺序很关键：先判断文件是否存在，再判断保留前缀，最后回落到 index.html
        if full_path:
            candidate = (_dist / full_path).resolve()
            if candidate.is_file() and _dist.resolve() in candidate.parents:
                # HTML 外壳一律禁用缓存，避免浏览器复用旧外壳加载已删除的 chunk
                if candidate.suffix == ".html":
                    return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
                return FileResponse(candidate)
        if full_path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
else:

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "MDE-LAB 多模态数字员工平台",
            "frontend": "开发模式请访问 http://localhost:3000（Vite 未构建 dist 时）",
            "docs": "/docs",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("core.main:app", host="0.0.0.0", port=8000, reload=True)
