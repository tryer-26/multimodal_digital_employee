"""定时任务（见设计文档 6.9 会话超时、第十六章数据保留）。

- 会话超时关闭：last_message_at 距今 > 30 分钟 → closed / end_reason='unresolved'；
- 媒体保留：语音/图片原文件保留 30 天；
- 会话数据保留：会话与消息保留 90 天。

【这个文件在系统里的角色】
把"不需要用户触发、到点就该做"的维护工作集中起来，交给 APScheduler 按计划执行：
一是把长时间没人说话的会话自动关闭（否则一直占着 active 状态和坐席接待数），
二是定期清理过期文件与历史数据（否则磁盘和数据库会无限增长）。

【关键概念扫盲：APScheduler 的两种触发器】
- interval（间隔触发）：如"每隔 5 分钟跑一次"，适合需要及时响应的轮询任务。
- cron（日历触发）：如"每天 03:00 整跑一次"，适合"半夜做、别影响白天"的清理任务。
本文件会话超时用 interval（要尽快关掉超时会话），清理类用 cron（每天凌晨低峰期跑）。
AsyncIOScheduler 与 asyncio 事件循环配合，可直接调度 async 函数。
"""
from __future__ import annotations

import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import settings
from core.infra.db import db

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_MINUTES = 30
MEDIA_RETENTION_DAYS = 30
CONVERSATION_RETENTION_DAYS = 90

# 模块级单例引用：start_scheduler 里赋值，shutdown_scheduler 里用它优雅停机。
_scheduler: AsyncIOScheduler | None = None


async def close_expired_conversations() -> int:
    """关闭超时会话，并在确有会话被关闭时推送最新统计给管理员。

    返回：
        本次被关闭的会话数。
    说明：
        push_stats_to_admins 用函数内导入（延迟导入），避免模块加载期的循环依赖：
        analytics 服务也会依赖数据库，此处只在真正用到时才导入。
    """
    count = await db.close_expired_conversations(SESSION_TIMEOUT_MINUTES)
    if count:
        logger.info("定时任务：自动关闭超时会话 %d 个", count)
        from core.service.analytics import push_stats_to_admins

        await push_stats_to_admins()
    return count


async def cleanup_media(days: int = MEDIA_RETENTION_DAYS) -> int:
    """清理超过保留期的媒体原文件（见第十六章）。

    参数：
        days: 保留天数，默认 30 天。
    返回：
        实际删除的文件数。
    说明：
        用文件修改时间 st_mtime 与"当前时间 - 保留天数"比较；单个文件删除失败（如被占用）
        只跳过、不中断整体清理（OSError 被吞掉）。
    """
    directory = settings.media_dir()
    if not directory.exists():
        return 0
    # cutoff 是"最早可保留时间"（秒级时间戳）：早于它的文件视为过期。
    cutoff = time.time() - days * 86400
    removed = 0
    for path in directory.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("定时任务：清理过期媒体文件 %d 个", removed)
    return removed


async def cleanup_conversations(days: int = CONVERSATION_RETENTION_DAYS) -> int:
    """清理超过保留期的会话与消息。

    参数：
        days: 保留天数，默认 90 天。
    返回：
        被清理的会话数。
    说明：
        先统计命中数量（用于日志与返回值），再依次删除关联消息、会话本体。
        关联的工具调用记录不在此处级联删除（与会话解耦，见 6.x）。
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    row = await db.query_one(
        "SELECT COUNT(*) AS n FROM conversations WHERE COALESCE(last_message_at, started_at) < ?",
        (cutoff,),
    )
    count = int((row or {}).get("n", 0))
    if count:
        # 先删子表（消息），再删主表（会话），避免留下孤儿消息。
        await db.execute(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE COALESCE(last_message_at, started_at) < ?)",
            (cutoff,),
        )
        await db.execute(
            "DELETE FROM conversations WHERE COALESCE(last_message_at, started_at) < ?", (cutoff,)
        )
        logger.info("定时任务：清理过期会话 %d 个", count)
    return count


def start_scheduler() -> AsyncIOScheduler:
    """创建并启动定时任务调度器，返回该调度器实例。

    返回：
        已启动的 AsyncIOScheduler。
    说明：
        只应在应用启动时调用一次。时区固定为 Asia/Shanghai，保证 cron 的"03:00"符合本地预期。
        - close_expired：interval 每 5 分钟轮询一次，及时关闭超时会话；
        - cleanup_media / cleanup_conversations：cron 每天凌晨 03:00 / 03:30 各跑一次，避开业务高峰。
    """
    global _scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(close_expired_conversations, "interval", minutes=5, id="close_expired")
    scheduler.add_job(cleanup_media, "cron", hour=3, minute=0, id="cleanup_media")
    scheduler.add_job(cleanup_conversations, "cron", hour=3, minute=30, id="cleanup_conversations")
    scheduler.start()
    _scheduler = scheduler
    logger.info("定时任务已启动：会话超时关闭（5分钟轮询）、媒体/会话保留清理（每日 03:00）")
    return scheduler


def shutdown_scheduler() -> None:
    """关闭调度器（应用退出时调用，释放后台任务）。

    说明：
        wait=False 表示不等待正在执行的任务结束、立即停止调度；
        用 try/except 兜底：即使关闭出错也不影响应用退出流程。
    """
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭定时任务失败：%s", exc)
        _scheduler = None
