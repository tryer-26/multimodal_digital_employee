"""会话路由：会话生命周期、转人工与坐席操作（见设计文档第 6 章、4.5、7.7）。

职责：
- 会话生命周期：创建 / 关闭（resolved / user_closed / unresolved 口径见 6.9）；
- 转人工与兜底：触发转人工、生成交接摘要、无坐席兜底建单（见 6.3 / 6.4 / 6.8）；
- 坐席操作：接入会话、人工回复、交还 AI、结束人工服务；
- 坐席辅助：摘要与 AI 辅助推荐（见 12.4）。

说明：**逐轮对话编排不在本模块**——一条用户消息进来到生成回复（三层意图分流、
知识库检索、生成、护栏与交付）由 ``core.service.session_flow``（CrewAI Flow）负责，
本模块只提供其调用的会话级原子操作与 WebSocket 推送工具。
"""
from __future__ import annotations

import logging
from typing import Optional

from core.service import handoff as handoff_mod
from core.ai import llm as llm_mod
from core.service.analytics import push_stats_to_admins as _push_admins_stats
from core.infra.db import db, now_iso
from core.rag.multimodal_rag import multimodal_rag
from core.infra.ws_manager import ws_manager

logger = logging.getLogger(__name__)

# 寒暄/口水话关键词（纯字符串匹配，零 LLM）：意图分流的**快速预过滤层**。
# 命中的明显寒暄直接短路，连一次 LLM 分类都省掉；没命中的交给 perceive_message
# 用大模型判（关键词无法穷举，模型负责兜底其余说法）。
# 这类消息必须绕开知识库检索与置信度护栏：检索分天然趋近 0（实测「你好」=0.0076），
# 会被置信度护栏当成「低置信度」而把用户误转人工
CHITCHAT_KEYWORDS = (
    "你好", "您好", "哈喽", "哈啰", "嗨", "在吗", "在么", "在不在", "有人吗",
    "谢谢", "多谢", "感谢", "辛苦了", "麻烦你了", "好的", "好啊", "收到",
    "明白", "知道了", "嗯", "哦", "哦哦", "好吧", "再见", "拜拜", "回头聊",
    "咋样", "怎么样", "干啥", "干嘛",
)
# 整句由这些英文词组成时也算寒暄（"hi there" / "thank you" / "ok"），
# 逐个 token 判定避免 "hi" 命中 shipping 之类的词中位置
_CHITCHAT_LATIN = ("hi", "hello", "hey", "there", "thanks", "thank", "you", "ok", "okay", "yo")
_CHITCHAT_STRIP = "，。！？、,.!?~～ \t\r\n:：;；\"'“”‘’"
# 剔除寒暄词与标点后，剩余内容不超过这个长度即视为寒暄（"你好啊" → "啊" → 寒暄）
_CHITCHAT_MAX_RESIDUE = 2


def _is_chitchat(text: str) -> bool:
    """判断是否寒暄/口水话：剔除寒暄词与标点后没有实质内容。

    「你好，我的VPN连不上」剩余「我的VPN连不上」→ 不是寒暄，正常走 RAG。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    stripped = raw.strip(_CHITCHAT_STRIP)
    if stripped.isascii() and stripped:
        tokens = stripped.lower().replace(",", " ").replace(".", " ").split()
        if tokens and all(token in _CHITCHAT_LATIN for token in tokens):
            return True
    residue = raw
    for keyword in CHITCHAT_KEYWORDS:
        residue = residue.replace(keyword, "")
    return len(residue.strip(_CHITCHAT_STRIP)) <= _CHITCHAT_MAX_RESIDUE


# ----------------------------------------------------------------------
# 消息推送
# ----------------------------------------------------------------------
async def _push_user(conv_id: str, user_id: str, message: dict) -> None:
    await ws_manager.send_to_user(user_id, message)


async def _push_agents(message: dict) -> None:
    await ws_manager.broadcast_to_agents(message)


# ----------------------------------------------------------------------
# 会话生命周期
# ----------------------------------------------------------------------
async def create_conversation(user_id: str) -> dict:
    conv_id = await db.create_conversation(user_id=user_id, title="新会话")
    await db.add_message(conv_id, "assistant", "您好，我是IT服务台数字员工，有什么可以帮您？")
    conversation = await db.get_conversation(conv_id)
    await _push_admins_stats()
    return conversation or {"id": conv_id}


async def close_conversation(conv_id: str, reason: str) -> dict:
    """关闭会话（见 6.9）。reason: resolved / user_closed。

    user_closed 且无关联工单时自动建单并回执工单号。
    """
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        return {"ok": False, "error": "会话不存在"}
    if conversation["status"] == "closed":
        return {"ok": True, "already_closed": True}

    ticket_id: Optional[str] = None
    if reason == "user_closed":
        existing = await handoff_mod.list_conversation_tickets(conv_id)
        if existing:
            ticket_id = existing[-1]
        else:
            ticket_id = await handoff_mod.create_ticket_for_conversation(
                conv_id, conversation["user_id"], extra="用户主动结束会话且问题未解决"
            )

    await db.update_conversation(
        conv_id, status="closed", end_reason=reason, ended_at=now_iso(), mode="bot"
    )
    # 用户可能在人工服务中直接关闭会话，坐席接待数需同步回退（见 6.8）
    if conversation.get("assigned_agent_id"):
        await db.sync_agent_current_count(conversation["assigned_agent_id"])
    await db.add_message(
        conv_id,
        "system",
        "该会话已结束" + (f"，已为您创建工单 {ticket_id}，可在“我的工单”中跟踪。" if ticket_id else "。"),
    )
    payload = {"ok": True, "end_reason": reason, "ticket_id": ticket_id}
    await _push_user(
        conv_id,
        conversation["user_id"],
        {"type": "system", "conversation_id": conv_id,
         "data": {"role": "system", "content": "该会话已结束，如需继续请新建会话。",
                  "ticket_id": ticket_id, "end_reason": reason}},
    )
    await _push_admins_stats()
    return payload


# ----------------------------------------------------------------------
# 转人工（见 6.3 / 6.4 / 6.8）
# ----------------------------------------------------------------------
async def trigger_handoff(conv_id: str, reason: str) -> dict:
    conversation = await db.get_conversation(conv_id)
    if not conversation or conversation["status"] == "closed":
        return {"ok": False}

    # 无可用坐席兜底：提示繁忙 + 自动建单 + 回到 bot（见 6.8）。
    # 判定口径与坐席状态一致（status='online' 且未满员）：只看 WS 连接数会把
    # 已切到 busy/offline 的坐席也算作「有人」，导致漏建单。
    # 分支先于摘要生成：兜底路径不消费摘要，不必为此白跑一次 LLM（省成本与延迟）
    available_agents = await db.list_available_agents()
    if not available_agents:
        # 先给用户即时反馈（切 waiting_human + 提示「正在转接」），再建单。
        # 建单含一次分类 LLM 调用，排在用户可见反馈之后，用户不必干等
        await db.update_conversation(conv_id, mode="waiting_human", handoff_reason=reason)
        await _push_user(
            conv_id, conversation["user_id"],
            {"type": "handoff_triggered", "conversation_id": conv_id,
             "data": {"role": "system", "content": "正在为您转接人工客服，请稍候…",
                      "mode": "waiting_human", "reason": reason}},
        )
        # 去重：同一会话已有工单则复用，避免多次触发转人工时重复建单（口径同 close_conversation）
        existing = await handoff_mod.list_conversation_tickets(conv_id)
        ticket_id = existing[-1] if existing else await handoff_mod.create_ticket_for_conversation(
            conv_id, conversation["user_id"], extra=f"转人工原因：{reason}（当前无坐席在线）"
        )
        await db.update_conversation(conv_id, mode="bot", handoff_reason=reason)
        text = (
            f"当前人工坐席繁忙，已为您创建工单 {ticket_id}，请到“我的工单”查看进展。"
            if ticket_id else "当前人工坐席繁忙，已记录您的问题，稍后由工程师跟进。"
        )
        await db.add_message(conv_id, "system", text)
        await _push_user(
            conv_id, conversation["user_id"],
            {"type": "return_to_bot", "conversation_id": conv_id,
             "data": {"role": "system", "content": text, "ticket_id": ticket_id,
                      "mode": "bot", "reason": reason}},
        )
        await _push_admins_stats()
        return {"ok": True, "mode": "bot", "ticket_id": ticket_id, "no_agent": True}

    # 有坐席：先把状态切到 waiting_human 并立刻告知用户，再生成交接摘要推给坐席。
    # 摘要含一次 LLM 调用（数秒），排在用户可见的「已转接」之后，用户不必干等
    await db.update_conversation(conv_id, mode="waiting_human", handoff_reason=reason)
    await db.add_message(conv_id, "system", f"正在为您转接人工客服（原因：{reason}）")
    await _push_user(
        conv_id, conversation["user_id"],
        {"type": "handoff_triggered", "conversation_id": conv_id,
         "data": {"role": "system", "content": "正在为您转接人工客服，请稍候…",
                  "mode": "waiting_human", "reason": reason}},
    )

    summary = await handoff_mod.generate_handoff_summary(conv_id, reason)
    # 坐席端展示的用户名取自 users.display_name（与 /api/agent/queue 的 JOIN 口径一致）
    conv_user = await db.get_user(conversation["user_id"])
    await _push_agents(
        {
            "type": "handoff_triggered",
            "conversation_id": conv_id,
            "data": {
                "conversation_id": conv_id,
                "user_id": conversation["user_id"],
                "user_name": (conv_user or {}).get("display_name") or conversation["user_id"],
                "last_user_message": summary.get("last_user_message", ""),
                "handoff_reason": reason,
                "summary": summary.get("summary", ""),
                "last_message_at": conversation.get("last_message_at"),
                "emotion_trend": summary.get("emotion_trend", "normal"),
            },
        }
    )
    await _push_admins_stats()
    return {"ok": True, "mode": "waiting_human", "reason": reason}


# ----------------------------------------------------------------------
# 坐席动作（见 6.5 / 12.4）
# ----------------------------------------------------------------------
async def agent_join(conv_id: str, agent_id: str) -> dict:
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        return {"ok": False, "error": "会话不存在"}
    if conversation["mode"] != "waiting_human":
        return {"ok": False, "error": "该会话当前不在等待人工状态"}

    await db.update_conversation(conv_id, mode="human", assigned_agent_id=agent_id)
    # 状态由坐席自己切换，接入不改写；只按事实重算接待数（见 6.8）
    await db.sync_agent_current_count(agent_id)
    await db.add_message(conv_id, "system", "人工客服已接入")
    await _push_user(
        conv_id, conversation["user_id"],
        {"type": "agent_joined", "conversation_id": conv_id,
         "data": {"role": "system", "content": "人工客服已接入", "mode": "human"}},
    )
    await _push_agents(
        {"type": "queue_update", "conversation_id": conv_id,
         "data": {"joined": conv_id, "agent_id": agent_id}}
    )
    await _push_admins_stats()
    return {"ok": True, "mode": "human"}


async def agent_reply(conv_id: str, agent_id: str, content: str) -> dict:
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        return {"ok": False, "error": "会话不存在"}
    if conversation["mode"] != "human":
        return {"ok": False, "error": "会话当前不在人工服务状态"}
    # 归属校验：仅允许被分配到该会话的坐席回复，避免越权向他人会话注入消息
    if conversation.get("assigned_agent_id") != agent_id:
        return {"ok": False, "error": "该会话未分配给你"}

    await db.add_message(conv_id, "agent", content)
    await _push_user(
        conv_id, conversation["user_id"],
        {"type": "agent_message", "conversation_id": conv_id,
         "data": {"role": "agent", "content": content, "timestamp": now_iso()}},
    )
    await _push_agents(
        {"type": "agent_message", "conversation_id": conv_id,
         "data": {"role": "agent", "content": content, "agent_id": agent_id}}
    )
    return {"ok": True}


async def return_to_ai(conv_id: str, agent_id: str, note: str) -> dict:
    conversation = await db.get_conversation(conv_id)
    if not conversation:
        return {"ok": False, "error": "会话不存在"}
    # 归属校验：交还 AI / 结束服务同样只允许会话归属坐席本人操作
    if conversation["mode"] != "human" or conversation.get("assigned_agent_id") != agent_id:
        return {"ok": False, "error": "该会话未分配给你"}
    if note:
        await db.add_message(conv_id, "system", f"[人工坐席备注] {note}")
    await db.update_conversation(conv_id, mode="bot", assigned_agent_id=None)
    # 不改写坐席状态（保持其自己选定的在线/忙碌/离线），只按事实重算接待数（见 6.8）
    await db.sync_agent_current_count(agent_id)
    await _push_user(
        conv_id, conversation["user_id"],
        {"type": "return_to_bot", "conversation_id": conv_id,
         "data": {"role": "system", "content": "本次人工服务已结束，已转回智能客服。",
                  "mode": "bot", "note": note}},
    )
    await _push_agents({"type": "queue_update", "conversation_id": conv_id, "data": {"returned": conv_id}})
    await _push_admins_stats()
    return {"ok": True, "mode": "bot"}


async def end_agent_service(conv_id: str, agent_id: str) -> dict:
    """坐席「结束会话」：只切 mode=bot，会话本身不关闭（见 6.5 / 6.9）。"""
    return await return_to_ai(conv_id, agent_id, note="")


# ----------------------------------------------------------------------
# AI 辅助推荐（坐席端，见 12.4）
# ----------------------------------------------------------------------
async def build_agent_suggestions(conv_id: str) -> dict:
    messages = await db.get_messages(conv_id, limit=100)
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")

    knowledge: list[dict] = []
    tickets: list[dict] = []
    draft = ""

    if last_user:
        try:
            results = await multimodal_rag.search(query=last_user)
            for item in results[:3]:
                meta = item.get("metadata") or {}
                knowledge.append(
                    {
                        "title": f"{meta.get('source', '知识')}（第{meta.get('page', '-')}页·{meta.get('modality', 'text')}）",
                        "content": (meta.get("content") or meta.get("context") or "")[:160],
                        "score": round(float(item.get("score", 0.0)), 3),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("坐席辅助检索失败：%s", exc)

    try:
        existing = await handoff_mod.list_conversation_tickets(conv_id)
        for ticket_id in existing[-3:]:
            detail = await handoff_mod.bridge.call_tool("query_ticket", {"ticket_id": ticket_id})
            tickets.append({"id": ticket_id, "title": detail[:120]})
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询相似工单失败：%s", exc)

    if last_user and llm_mod.available():
        context = "\n".join(f"- {k['title']}: {k['content']}" for k in knowledge[:2])
        try:
            draft = await llm_mod.chat(
                [
                    {"role": "system", "content": "你是IT服务台人工坐席，请给出一句专业、友好的回复草稿。"},
                    {"role": "user", "content": f"用户问题：{last_user}\n可参考知识：\n{context}\n只输出回复正文。"},
                ],
                max_tokens=300,
            )
        except Exception:  # noqa: BLE001
            draft = ""

    return {"knowledge": knowledge, "tickets": tickets, "reply_draft": draft}
