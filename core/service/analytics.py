"""数据看板统计（见设计文档 12.5 指标口径）。

本模块职责：面向管理端「实时数据看板」，把散落在 conversations / messages /
tool_calls 等表里的原始记录，聚合成一组可直接展示的统计指标，并通过 WebSocket
推送给在线管理员（见 12.7）。它只做「读 + 聚合」，不修改任何业务数据。

指标口径（冻结定义）：
- 会话总数：统计周期内新建会话数（所有比率的分母）；
- 解决率：(end_reason='resolved') / 会话总数；
- 转人工率：发生过 handoff 的会话数 / 会话总数；
- 平均响应时间：Σ(用户消息 → AI 首次回复) / 消息对数（仅 bot 模式）；
- 平均人工处理时长：Σ(坐席接入 → 会话结束) / 人工会话数。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from core.infra.db import db

logger = logging.getLogger(__name__)

# 看板意图分布关键词：按业务话术归并成中文标签。与知识库/工单使用的英文 category
# 枚举（network / password / ...）**不是同一套口径**，两者不通用（见 5.5）
# 注意：字典的遍历顺序就是匹配优先级（Python 3.7+ 保持插入顺序），
# classify_intent 命中第一个标签即返回，所以标签顺序会影响归类结果。
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "VPN/网络": ("vpn", "网络", "连不上", "断网", "上网", "网络连接"),
    "密码/账号": ("密码", "重置", "登录", "账号", "账户", "锁定"),
    "软件": ("软件", "安装", "授权", "报错", "客户端", "崩溃"),
    "财务/报销": ("报销", "财务", "发票", "结算"),
    "邮箱": ("邮箱", "邮件", "收发", "outlook", "mail"),
}


def classify_intent(text: str) -> str:
    """把一段用户文本粗略归类到 INTENT_KEYWORDS 的某个中文标签。

    参数：
        text: 用户消息文本（可能为空）。

    返回：
        命中的标签名；空文本或一个关键词都没命中时统一返回 "其他"。

    实现说明：
        先把文本转小写（lower）以兼容英文关键词的大小写差异，再按标签顺序
        逐个做子串包含判断；因此这是「关键词匹配」而非模型分类，误差可接受，
        目的是给看板一个直观的分布，不用于严肃统计。
    """
    if not text:
        return "其他"
    lowered = text.lower()
    for label, keywords in INTENT_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return label
    return "其他"


def _parse(ts: Optional[str]) -> Optional[datetime]:
    """把数据库里的 ISO 时间字符串解析成 datetime，解析失败返回 None。

    参数：
        ts: 时间字符串，可能为 None（例如会话尚未结束、last_message_at 为空）。

    返回：
        datetime 对象；若 ts 为空或格式非法则返回 None。

    边界说明：
        用 try/except 兜底，是因为历史数据/脏数据可能导致 fromisoformat 抛
        TypeError 或 ValueError；调用方拿到 None 后自行跳过该条即可，不会中断
        整个统计流程。
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


async def dashboard(days: int = 7) -> dict[str, Any]:
    """聚合最近 days 天的看板数据，返回前端可直接渲染的统计字典。

    参数：
        days: 统计周期（天），默认 7。只影响「周期内」的指标（趋势、解决率、
            响应时间、情感/意图/工具分布等），不改变返回结构。

    返回：
        dict，含 session_total / today_sessions / resolution_rate /
        handoff_rate / avg_response_ms / avg_human_ms / trend /
        emotion_dist / intent_dist / tool_calls / recent_handoffs。

    说明（给弱基础）：
        - async 表示这是「协程」：函数内部用 await 查询数据库，等待 IO 期间
          会让出执行权，Web 服务借此同时服务多个请求，而不是干等数据库；
        - conversations 只取周期内的会话；all_conversations 在周期内无数据时
          回退成全量，主要是让演示环境里的「意图分布 / 最近转人工」不至于空白。
    """
    # 周期起点（ISO 字符串）与今天的日期串；SQLite 中时间以字符串存储，可直接比较
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y-%m-%d")

    # 只取统计周期内新建的会话，按开始时间倒序（后续「最近转人工」依赖这个顺序）
    conversations = await db.query_all(
        "SELECT * FROM conversations WHERE started_at >= ? ORDER BY started_at DESC", (since,)
    )
    # all_conversations：周期内为空时回退到全量，避免看板「意图/转人工」全空；
    # 注意 total 仍只按周期内会话数计算，回退数据仅用于展示类分布
    all_conversations = conversations or await db.query_all("SELECT * FROM conversations")
    total = len(conversations) or 0

    # 以下三个计数在 Python 侧遍历：它们要按行内字段做条件判断，数据量小、
    # 逻辑比写 SQL 更直观；所有比率的分母统一用 total（周期内会话总数）
    resolved = sum(1 for c in conversations if c.get("end_reason") == "resolved")
    handoff_count = sum(1 for c in conversations if c.get("handoff_reason"))
    today_count = sum(1 for c in conversations if (c.get("started_at") or "").startswith(today))

    # 比率四舍五入保留 4 位小数；total 为 0 时直接给 0.0，避免除零报错
    resolution_rate = round(resolved / total, 4) if total else 0.0
    handoff_rate = round(handoff_count / total, 4) if total else 0.0

    # 响应时间需按消息时间配对计算（见 _avg_response_ms）；人工时长在 Python 侧算
    avg_response_ms = await _avg_response_ms(since)
    avg_human_ms = _avg_human_duration(conversations)

    # 对话量趋势：先按天铺满整个周期（无数据的日期也补 0，折线图不断点），
    # 再把每条会话按 started_at 前 10 位（YYYY-MM-DD）落进对应的桶
    trend_map: dict[str, int] = {}
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        trend_map[day] = 0
    for conv in conversations:
        day = (conv.get("started_at") or "")[:10]
        if day in trend_map:
            trend_map[day] += 1

    # 情绪分布：交给 SQL 的 GROUP BY + COUNT 聚合。为什么不在 Python 里遍历？
    # 因为聚合交给数据库只回传「每种情绪一行」，比把所有消息读进内存再统计更省资源。
    # 口径：只统计 user 角色、emotion 非空、且落在周期内的消息
    emotion_rows = await db.query_all(
        "SELECT emotion, COUNT(*) AS n FROM messages WHERE role='user' AND emotion IS NOT NULL "
        "AND created_at >= ? GROUP BY emotion",
        (since,),
    )
    # 文案与前端 utils/format.js 的 emotionLabel 保持一致（积极/中性/消极），
    # 避免同一概念出现「正面/负面」与「积极/消极」两套用词
    emotion_label = {"positive": "积极", "neutral": "中性", "negative": "消极"}
    emotion_dist = [
        {"name": emotion_label.get(r["emotion"], r["emotion"]), "value": int(r["n"])}
        for r in emotion_rows
    ] or [{"name": "中性", "value": 0}]

    # 意图分布（按每个会话首条用户消息分类）
    # 口径：一个会话只算一次，取该会话 id 最小的那条 user 消息代表用户诉求；
    # 用 all_conversations（可能包含回退的全量）以覆盖更多样本，让分布更有意义
    intent_map: dict[str, int] = {}
    for conv in all_conversations:
        first = await db.query_one(
            "SELECT content FROM messages WHERE conversation_id=? AND role='user' ORDER BY id ASC LIMIT 1",
            (conv["id"],),
        )
        label = classify_intent((first or {}).get("content", ""))
        intent_map[label] = intent_map.get(label, 0) + 1
    # 按次数降序排序，方便前端直接画柱状图/饼图
    intent_dist = [{"name": k, "value": v} for k, v in sorted(intent_map.items(), key=lambda x: -x[1])]

    # 工具调用次数：同样让 SQL 用 GROUP BY 汇总，再按调用次数降序
    tool_rows = await db.query_all(
        "SELECT tool_name, COUNT(*) AS n FROM tool_calls WHERE created_at >= ? GROUP BY tool_name ORDER BY n DESC",
        (since,),
    )
    tool_calls = [{"name": r["tool_name"], "value": int(r["n"])} for r in tool_rows]

    # 最近转人工会话：过滤出发生过 handoff 的会话，最多取前 10 条
    # （all_conversations 已按 started_at 倒序，所以前 10 条就是最近的转人工）
    recent_handoffs = []
    for conv in [c for c in all_conversations if c.get("handoff_reason")][:10]:
        agent_name = ""
        if conv.get("assigned_agent_id"):
            # 按坐席 id 反查用户表拿展示名；查不到就留空字符串
            agent = await db.get_user(conv["assigned_agent_id"])
            agent_name = (agent or {}).get("display_name", "")
        user = await db.get_user(conv["user_id"]) if conv.get("user_id") else None
        # 结束时间优先取 ended_at，缺失时退回 last_message_at；两者都拿不到时长为 0
        started, ended = _parse(conv.get("started_at")), _parse(conv.get("ended_at") or conv.get("last_message_at"))
        duration = int((ended - started).total_seconds()) if started and ended else 0
        recent_handoffs.append(
            {
                "time": (conv.get("started_at") or "")[11:16],  # 截取 HH:MM，便于列表展示
                "user": (user or {}).get("display_name", conv.get("user_id", "")),
                "reason": conv.get("handoff_reason") or "",
                "agent": agent_name,
                "duration_seconds": duration,
            }
        )

    # 统一成前端约定的字段名返回；空列表用「暂无数据」占位，避免图表空白
    return {
        "session_total": total,
        "today_sessions": today_count,
        "resolution_rate": resolution_rate,
        "handoff_rate": handoff_rate,
        "avg_response_ms": avg_response_ms,
        "avg_human_ms": avg_human_ms,
        "trend": [{"date": k, "count": v} for k, v in trend_map.items()],
        "emotion_dist": emotion_dist,
        "intent_dist": intent_dist or [{"name": "暂无数据", "value": 0}],
        "tool_calls": tool_calls or [{"name": "暂无数据", "value": 0}],
        "recent_handoffs": recent_handoffs,
    }


async def _avg_response_ms(since: str) -> int:
    """平均响应时间：用户消息 → 紧邻的下一条 assistant 消息（仅 bot 模式）。

    参数：
        since: 统计起始时间（ISO 字符串），只统计此时间之后的消息。

    返回：
        平均毫秒数（int，向下取整）；没有任何可配对的消息时返回 0。

    实现说明：
        一次性按 (conversation_id, id) 排序取出所有消息，然后在内存里顺序扫描：
        遇到 user 记下时间起点 pending，遇到紧随其后的 assistant 就算一次
        「响应耗时」。切换会话时重置 pending，避免把跨会话的两条消息错配成一对。
    """
    rows = await db.query_all(
        "SELECT conversation_id, role, created_at FROM messages WHERE created_at >= ? ORDER BY conversation_id, id",
        (since,),
    )
    deltas: list[float] = []
    pending: Optional[datetime] = None
    current_conv = None
    for row in rows:
        # 进入新会话就清空 pending，确保只配对本会话内的「用户→AI」消息
        if row["conversation_id"] != current_conv:
            current_conv = row["conversation_id"]
            pending = None
        if row["role"] == "user":
            # 记下用户发言时间，等待下一条 assistant 回复
            pending = _parse(row["created_at"])
        elif row["role"] == "assistant" and pending is not None:
            # 配对成功：计算毫秒差；算完后清空 pending，避免一条用户消息被重复配对
            ts = _parse(row["created_at"])
            if ts:
                deltas.append((ts - pending).total_seconds() * 1000)
            pending = None
    if not deltas:
        return 0
    # 算术平均后取整；deltas 为空的情况上面已提前返回，故不会除零
    return int(sum(deltas) / len(deltas))


def _avg_human_duration(conversations: list[dict]) -> int:
    """平均人工处理时长：仅统计已接入坐席的会话。

    参数：
        conversations: 会话记录列表（来自 dashboard 的查询结果）。

    返回：
        平均秒数（int）；没有「已接入坐席且有起止时间」的会话时返回 0。

    口径说明：
        只看 assigned_agent_id 非空的会话（即真正流转到人工的会话）；
        起止时间优先 ended_at，缺失时退回 last_message_at，与响应时间口径保持
        一致的容错策略；两者缺一则该条跳过，不计入平均。
    """
    durations: list[float] = []
    for conv in conversations:
        if not conv.get("assigned_agent_id"):
            continue
        started, ended = _parse(conv.get("started_at")), _parse(conv.get("ended_at") or conv.get("last_message_at"))
        if started and ended:
            durations.append((ended - started).total_seconds())
    if not durations:
        return 0
    return int(sum(durations) / len(durations))


async def snapshot_for_ws() -> dict[str, Any]:
    """轻量快照，用于 WebSocket 实时推送（见 12.7）。

    与 dashboard 的区别：这里不追求丰富的分布数据，只取 4 个「计数器」以降低
    推送开销（看板定时刷新，频率高，必须足够轻）。

    返回：
        dict，含 session_total / today_sessions / active_sessions / waiting_human。

    实现说明：
        4 个数字全部用 SQL 的 COUNT(*) 直接算，让数据库完成计数，避免把整表
        记录读进内存；查询结果可能为 None（无行时），故用 (row or {}).get(...)
        兜底成 0。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # 会话总数：全表计数
    row_total = await db.query_one("SELECT COUNT(*) AS n FROM conversations")
    # 今日会话数：用 LIKE 'YYYY-MM-DD%' 匹配当天的时间戳前缀
    row_today = await db.query_one(
        "SELECT COUNT(*) AS n FROM conversations WHERE started_at LIKE ?", (f"{today}%",)
    )
    # 进行中的会话（status='active'）
    row_active = await db.query_one(
        "SELECT COUNT(*) AS n FROM conversations WHERE status='active'"
    )
    # 正在等待人工接入的会话（active 且 mode='waiting_human'），供坐席端提醒
    row_waiting = await db.query_one(
        "SELECT COUNT(*) AS n FROM conversations WHERE status='active' AND mode='waiting_human'"
    )
    return {
        "session_total": int((row_total or {}).get("n", 0)),
        "today_sessions": int((row_today or {}).get("n", 0)),
        "active_sessions": int((row_active or {}).get("n", 0)),
        "waiting_human": int((row_waiting or {}).get("n", 0)),
    }


async def push_stats_to_admins() -> None:
    """把看板快照推送给管理端 WebSocket（会话侧 / 定时任务共用）。

    实现说明：
        - WebSocket 是「服务端主动推送」的双向长连接：广播后所有在线管理员
          的看板都会实时刷新，无需前端轮询；
        - ws_manager 在函数内部延迟导入（import 放在函数体内），是为了避免
          service 层与 infra 层在模块加载阶段互相引用形成循环依赖；
        - 用 try/except 兜住广播异常：看板推送属于「尽力而为」，某个连接掉线
          不应该影响调用方的主流程（例如正在处理的会话）；异常只记 debug 日志。
    """
    from core.infra.ws_manager import ws_manager

    try:
        await ws_manager.broadcast_stats(await snapshot_for_ws())
    except Exception as exc:  # noqa: BLE001
        logger.debug("推送看板数据失败：%s", exc)
