"""转人工判定、上下文同步与兜底（见设计文档第 6 章）。

- 转人工触发条件与顺序见 6.3；
- 摘要生成见 6.4；
- 无坐席兜底自动建单见 6.8；
- 会话关闭时的建单口径见 6.9。
"""
from __future__ import annotations

import logging
from typing import Optional

from core.config import settings
from core.infra.db import db
from core.ai.llm import available as llm_available
from core.ai.llm import chat_json
from core.infra.mcp_bridge import bridge

logger = logging.getLogger(__name__)

# 用户明确要求转人工的关键词（见 6.3 触发条件 1）。
# 注意：不能用裸词「人工」（会误命中「人工智能」等），必须搭配动词/称谓组合
HANDOFF_KEYWORDS = (
    "转人工", "接人工", "转接人工", "人工客服", "人工服务",
    "找人工", "要人工", "叫人工", "请人工", "换人工", "联系人工",
    "真人", "找人处理", "找客服", "转客服", "叫工程师", "找工程师",
    "帮我找人", "找个人", "人工介入",
)

# IT 问题信号词（纯字符串匹配，零 LLM）：置信度护栏的**降级判定**。
# 主判定用 LLM 识别出的 intent==it_issue（见 perceive_message）；关键词无法穷举，
# 仅在本轮没拿到 intent 时兜底（LLM 未配置/超时/解析失败，或走了关键词短路路径）
IT_KEYWORDS = (
    "vpn", "密码", "邮箱", "邮件", "软件", "网络", "系统", "账号", "帐号",
    "登录", "登陆", "权限", "打印机", "打印", "安装", "卸载", "驱动", "重启",
    "报销", "门户", "截图", "报错", "错误", "失败", "打不开", "连不上", "无法",
    "用不了", "没反应", "蓝屏", "崩溃", "卡顿", "死机", "重置", "解锁", "连不上网",
)

CATEGORY_ENUM = ("network", "password", "software", "finance", "email", "general")
PRIORITY_ENUM = ("high", "medium", "low")


async def detect_handoff_intent(text: str) -> bool:
    if not text:
        return False
    return any(keyword in text for keyword in HANDOFF_KEYWORDS)


def detect_it_issue(text: str) -> bool:
    """判断消息是否在描述 IT 问题（见 6.3 条件 3 的前置条件）。"""
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in IT_KEYWORDS)


async def get_negative_rounds(conv_id: str, window: int = 5) -> int:
    """统计最近连续几轮用户消息情绪为负面（见 6.3 触发条件 2）。"""
    rows = await db.query_all(
        "SELECT emotion FROM messages WHERE conversation_id=? AND role='user' ORDER BY id DESC LIMIT ?",
        (conv_id, window),
    )
    rounds = 0
    for row in rows:
        if row.get("emotion") == "negative":
            rounds += 1
        else:
            break
    return rounds


async def get_unresolved_rounds(conv_id: str, window: int = 5) -> int:
    """统计最近连续几轮「IT 问题」的 AI 回复未命中知识库（confidence 为空或 <= 0）。

    口径（见 6.3 条件 4）：**只有用户在问 IT 问题的轮次才计入**——该轮用户消息的
    ``intent`` 必须是 ``it_issue``，遇到寒暄/无关话题立即打断连续计数。

    为什么必须按 intent 过滤：寒暄/无关话题走的是短回复路径，不写 ``confidence``，
    若一并计入，闲聊几轮就会把计数垫满，导致紧随其后的真实 IT 问题被误转人工
    （实测「开场欢迎语 + 2 轮寒暄」即达阈值）。会话开场的欢迎语本身也被排除——
    它没有配对的用户消息。
    """
    rows = await db.query_all(
        "SELECT role, confidence, intent FROM messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT ?",
        (conv_id, window * 2 + 2),
    )
    rounds = 0
    reply: Optional[dict] = None  # 等待配对的 AI 回复
    for row in rows:
        role = row.get("role")
        if role == "assistant":
            reply = row
            continue
        if role != "user" or reply is None:
            continue
        if row.get("intent") != "it_issue":
            break  # 非 IT 轮次：连续计数到此为止
        confidence = reply.get("confidence")
        if confidence is None or float(confidence) <= 0:
            rounds += 1
            reply = None
            continue
        break  # 命中知识库：连续计数结束
    return rounds


async def should_handoff(
    conv_id: str,
    turn_confidence: Optional[float] = None,
    user_text: str = "",
    kb_nonempty: bool = True,
    intent: Optional[str] = None,
) -> tuple[bool, str]:
    """判断是否需要转人工，返回 (是否转人工, 原因)。判定顺序见 6.3。"""
    # 1. 用户明确要求
    if await detect_handoff_intent(user_text):
        return True, "用户明确要求转人工"

    # 2. 情绪激动（持续 2 轮以上）
    negative_rounds = await get_negative_rounds(conv_id)
    if negative_rounds >= settings.HANDOFF_NEGATIVE_ROUNDS:
        return True, f"用户情绪持续负面，已持续{negative_rounds}轮"

    # 3. AI 置信度低（见 6.3）。三个前置条件缺一不可：
    #    - 知识库非空，否则空库必然低分而误触发；
    #    - turn_confidence 非 None，即「本轮确实检索到了内容」。不再回退取上一轮助手消息的
    #      置信度——那是拿旧轮次的分数判本轮，语义不成立，且会让「本轮根本没检索」也命中；
    #    - 用户确实在问 IT 问题。优先用本轮 LLM 识别出的 intent（见 perceive_message）；
    #      未识别时（如关键词短路路径、单测）回退关键词判定，保证 LLM 不可用时不退化。
    #      寒暄/闲聊/无关提问的检索分天然趋近 0，一并判定会把这类用户误转人工
    #      （实测「你好」top1=0.0076 → 误转人工）。
    #    本轮无置信度时本条不判定，交给条件 4「连续未解决」兜底。
    it_issue = intent == "it_issue" if intent else detect_it_issue(user_text)
    if kb_nonempty and turn_confidence is not None and it_issue:
        if 0 < turn_confidence < settings.HANDOFF_MIN_CONFIDENCE:
            return True, f"知识库检索置信度{turn_confidence:.2f}，低于阈值{settings.HANDOFF_MIN_CONFIDENCE}"

    # 4. 连续未解决（只统计「IT 问题」轮次，寒暄/无关话题会打断计数，见 get_unresolved_rounds）
    unresolved = await get_unresolved_rounds(conv_id)
    if unresolved >= settings.HANDOFF_UNRESOLVED_ROUNDS:
        return True, f"连续{unresolved}轮未解决"

    return False, ""


async def generate_handoff_summary(conv_id: str, reason: str = "") -> dict:
    """生成转人工摘要（见 6.4）：坐席无需重新问一遍。"""
    messages = await db.get_messages(conv_id, limit=100)
    conversation = await db.get_conversation(conv_id) or {}

    transcript_lines: list[str] = []
    for msg in messages[-30:]:
        role_label = {"user": "用户", "assistant": "AI客服", "agent": "人工客服", "system": "系统"}.get(
            msg.get("role", ""), msg.get("role", "")
        )
        transcript_lines.append(f"{role_label}：{msg.get('content', '')}")

    summary = ""
    if llm_available() and transcript_lines:
        prompt = (
            "请用不超过 3 句话总结这次IT服务台客服对话：\n"
            "1) 用户的问题是什么；2) AI 已经做了什么；3) 当前卡在哪里。\n"
            "只输出总结正文，不要任何前后缀。\n\n对话记录：\n" + "\n".join(transcript_lines)
        )
        result = await chat_json(
            "请直接输出 JSON：{\"summary\": \"...\"}。\n\n" + prompt,
            system="你是客服值班主管，擅长用简洁语言做对话交接摘要。",
        )
        if result and result.get("summary"):
            summary = str(result["summary"]).strip()
    if not summary:
        # 无 LLM 时的兜底摘要
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        summary = f"用户问题：{last_user[:80]}；已进行 {len(messages)} 条消息交互，需人工介入。"

    emotional = await get_negative_rounds(conv_id)
    return {
        "summary": summary,
        "handoff_reason": reason or conversation.get("handoff_reason", ""),
        "message_count": len(messages),
        "emotion_trend": "negative" if emotional >= 2 else "normal",
        "last_user_message": next((m["content"] for m in reversed(messages) if m.get("role") == "user"), ""),
    }


async def _classify_ticket(conv_id: str, extra: str = "") -> dict:
    """用 LLM 判定工单标题/描述/优先级/分类（规则见 5.5）。"""
    messages = await db.get_messages(conv_id, limit=40)
    transcript = "\n".join(
        f"{'用户' if m.get('role') == 'user' else 'AI/坐席'}：{m.get('content', '')}" for m in messages[-20:]
    )
    default = {
        "title": "IT问题跟进",
        "description": f"会话 {conv_id} 问题未解决，需线下跟进。{extra}".strip(),
        "priority": "medium",
        "category": "general",
    }
    if not llm_available() or not transcript:
        return default

    prompt = (
        "根据下面的IT服务台对话，生成一个工单。严格只输出 JSON，字段：\n"
        '{"title": "不超过20字的标题", "description": "问题描述与已做排查", '
        '"priority": "high|medium|low", "category": "network|password|software|finance|email|general"}\n'
        "判定标准：多人受影响/核心系统不可用/明确加急=high；单人问题但有绕过方式=medium；"
        "咨询类不阻塞=low。\n\n对话：\n" + transcript
    )
    data = await chat_json(prompt, system="你是IT服务台工单受理员，输出必须是合法 JSON。")
    if not data:
        return default

    ticket = {
        "title": str(data.get("title") or default["title"])[:60],
        "description": str(data.get("description") or default["description"])[:1000],
        "priority": data.get("priority") if data.get("priority") in PRIORITY_ENUM else "medium",
        "category": data.get("category") if data.get("category") in CATEGORY_ENUM else "general",
    }
    if extra:
        ticket["description"] = f"{ticket['description']}\n备注：{extra}"
    return ticket


async def create_ticket_for_conversation(conv_id: str, user_id: str, extra: str = "") -> Optional[str]:
    """按会话内容自动建单（见 6.8 / 6.9），返回工单号；失败返回 None。"""
    if not bridge.available:
        logger.warning("工单 MCP Server 不可用，跳过自动建单。")
        return None
    ticket = await _classify_ticket(conv_id, extra)
    arguments = {
        "title": ticket["title"],
        "description": ticket["description"],
        "priority": ticket["priority"],
        "category": ticket["category"],
        "user_id": user_id,
    }
    try:
        text = await bridge.call_tool("create_ticket", arguments)
    except Exception as exc:  # noqa: BLE001
        logger.error("自动建单失败：%s", exc)
        # 落审计：失败也要留痕，与 CrewAI 工具路径的 _log_tool_call 口径一致
        await db.add_tool_call(conv_id, "create_ticket", arguments, str(exc), status="failed")
        return None
    # 必须写入 tool_calls：list_conversation_tickets 依赖它做去重（见 6.9），
    # 否则关闭会话时会认为「无关联工单」而重复建单。
    await db.add_tool_call(conv_id, "create_ticket", arguments, text, status="success")
    logger.info("自动建单成功：%s", text)
    if "TICKET-" in text:
        idx = text.find("TICKET-")
        return text[idx : idx + 15]
    return None


async def list_conversation_tickets(conv_id: str) -> list[str]:
    """从工具调用日志中提取该会话已创建的工单号（避免重复建单，见 6.9）。"""
    rows = await db.query_all(
        "SELECT output_result FROM tool_calls WHERE conversation_id=? AND tool_name='create_ticket'",
        (conv_id,),
    )
    tickets: list[str] = []
    for row in rows:
        text = row.get("output_result") or ""
        idx = text.find("TICKET-")
        if idx != -1:
            tickets.append(text[idx : idx + 15])
    return tickets
