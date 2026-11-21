"""转人工判定与会话超时关闭测试（见设计文档 6.3 / 6.9）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import core.service.handoff as handoff_mod
import core.service.session_router as session_router
from core.config import settings
from core.infra.db import Database, now_iso


@pytest.fixture()
async def test_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    # conversations.user_id 有外键约束，先建两个固定 id 的测试用户
    for user_id in ("u-1", "u-2"):
        await database.execute(
            """INSERT INTO users (id, username, password_hash, display_name, email,
               department, role, status, created_at)
               VALUES (?,?,?,?,'','', 'user', 'active', ?)""",
            (user_id, user_id, "x", user_id, now_iso()),
        )
    monkeypatch.setattr(handoff_mod, "db", database)
    return database


async def test_detect_handoff_intent():
    assert await handoff_mod.detect_handoff_intent("还是不行，帮我找人处理")
    assert await handoff_mod.detect_handoff_intent("我要转人工")
    assert not await handoff_mod.detect_handoff_intent("我的VPN连不上了")


async def test_handoff_on_user_request(test_db):
    conv = await test_db.create_conversation("u-1")
    need, reason = await handoff_mod.should_handoff(conv, user_text="麻烦帮我转人工")
    assert need and "用户明确要求" in reason


async def test_handoff_on_negative_emotion(test_db):
    conv = await test_db.create_conversation("u-1")
    await test_db.add_message(conv, "user", "怎么还是不行", emotion="negative")
    await test_db.add_message(conv, "assistant", "请再试一次", confidence=0.9)
    await test_db.add_message(conv, "user", "你们这系统真差劲", emotion="negative")
    # kb_nonempty=False 以隔离「置信度」规则，验证情绪规则单独生效
    need, reason = await handoff_mod.should_handoff(conv, user_text="你们这系统真差劲", kb_nonempty=False)
    assert need and "情绪" in reason


async def test_handoff_on_low_confidence(test_db):
    conv = await test_db.create_conversation("u-1")
    await test_db.add_message(conv, "user", "报销系统打不开", emotion="neutral")
    threshold = settings.HANDOFF_MIN_CONFIDENCE
    need, reason = await handoff_mod.should_handoff(
        conv, turn_confidence=threshold / 2, user_text="报销系统打不开", kb_nonempty=True
    )
    assert need and "置信度" in reason


async def test_handoff_on_unresolved_rounds(test_db):
    conv = await test_db.create_conversation("u-1")
    for i in range(settings.HANDOFF_UNRESOLVED_ROUNDS):
        await test_db.add_message(conv, "user", f"问题{i}", emotion="neutral", intent="it_issue")
        await test_db.add_message(conv, "assistant", "抱歉，暂未找到方案")
    need, reason = await handoff_mod.should_handoff(
        conv, turn_confidence=None, user_text="还是不行", kb_nonempty=True, intent="it_issue"
    )
    assert need and "未解决" in reason


async def test_unresolved_rounds_ignores_chitchat(test_db):
    """寒暄/无关话题轮次（短回复不写 confidence）不得计入「未解决」，否则闲聊几轮后
    首个真实 IT 问题会被误转人工。见 6.3 条件 4。"""
    conv = await test_db.create_conversation("u-1")
    # 开场欢迎语 + 3 轮寒暄：都不该计入
    await test_db.add_message(conv, "assistant", "您好，我是IT服务台数字员工")
    for text in ("你好", "嘿嘿", "谢谢"):
        await test_db.add_message(conv, "user", text, emotion="neutral", intent="chitchat")
        await test_db.add_message(conv, "assistant", "（寒暄短回复）")

    assert await handoff_mod.get_unresolved_rounds(conv) == 0
    need, reason = await handoff_mod.should_handoff(
        conv, turn_confidence=0.70, user_text="VPN连不上", kb_nonempty=True, intent="it_issue"
    )
    assert not need and reason == ""


async def test_no_handoff_when_solved(test_db):
    conv = await test_db.create_conversation("u-1")
    await test_db.add_message(conv, "user", "VPN连不上", emotion="neutral")
    await test_db.add_message(conv, "assistant", "请按以下步骤排查", confidence=0.95)
    need, reason = await handoff_mod.should_handoff(
        conv, turn_confidence=0.95, user_text="谢谢，好了", kb_nonempty=True
    )
    assert not need and reason == ""


class _FakeBridge:
    """假 MCP 桥：记录调用并返回含工单号的文本。"""

    def __init__(self, reply: str = "已创建工单 TICKET-abc12345，请在“我的工单”中跟踪。"):
        self.available = True
        self.calls: list[tuple[str, dict]] = []
        self._reply = reply

    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> str:
        self.calls.append((name, arguments))
        return self._reply


class _BoomBridge(_FakeBridge):
    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> str:
        self.calls.append((name, arguments))
        raise RuntimeError("mcp down")


async def test_auto_ticket_writes_tool_call(test_db, monkeypatch):
    """自动建单必须写入 tool_calls，否则 list_conversation_tickets 去重失效、重复建单。"""
    fake = _FakeBridge()
    monkeypatch.setattr(handoff_mod, "bridge", fake)
    # 无 LLM：_classify_ticket 走默认值，避免真实模型调用
    monkeypatch.setattr(handoff_mod, "llm_available", lambda: False)

    conv = await test_db.create_conversation("u-1")
    ticket_id = await handoff_mod.create_ticket_for_conversation(conv, "u-1")

    assert ticket_id == "TICKET-abc12345"
    assert [c[0] for c in fake.calls] == ["create_ticket"]
    # 关键断言：去重函数能查到刚建的工单（修复前恒为空）
    assert await handoff_mod.list_conversation_tickets(conv) == ["TICKET-abc12345"]

    rows = await test_db.query_all(
        "SELECT tool_name, status FROM tool_calls WHERE conversation_id=?", (conv,)
    )
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "create_ticket"
    assert rows[0]["status"] == "success"


async def test_auto_ticket_failure_logged_not_deduped(test_db, monkeypatch):
    """建单失败也要落一条 failed 审计；但不应被当成已建工单（去重仍为空）。"""
    fake = _BoomBridge()
    monkeypatch.setattr(handoff_mod, "bridge", fake)
    monkeypatch.setattr(handoff_mod, "llm_available", lambda: False)

    conv = await test_db.create_conversation("u-1")
    assert await handoff_mod.create_ticket_for_conversation(conv, "u-1") is None
    assert await handoff_mod.list_conversation_tickets(conv) == []

    rows = await test_db.query_all(
        "SELECT status FROM tool_calls WHERE conversation_id=?", (conv,)
    )
    assert [r["status"] for r in rows] == ["failed"]


class _FakeWS:
    def __init__(self) -> None:
        self.user_messages: list[dict] = []
        self.agent_messages: list[dict] = []

    async def send_to_user(self, user_id: str, message: dict) -> None:
        self.user_messages.append(message)

    async def broadcast_to_agents(self, message: dict) -> None:
        self.agent_messages.append(message)


async def _noop() -> None:
    return None


async def test_no_agent_handoff_reuses_existing_ticket(test_db, monkeypatch):
    """无坐席兜底转人工时，若会话已有工单则复用，不重复建单。"""
    monkeypatch.setattr(session_router, "db", test_db)
    monkeypatch.setattr(handoff_mod, "db", test_db)
    monkeypatch.setattr(session_router, "ws_manager", _FakeWS())
    monkeypatch.setattr(session_router, "_push_admins_stats", _noop)
    fake = _FakeBridge()
    monkeypatch.setattr(handoff_mod, "bridge", fake)

    async def _no_agents():
        return []

    monkeypatch.setattr(test_db, "list_available_agents", _no_agents)

    conv = await test_db.create_conversation("u-1")
    await test_db.add_tool_call(conv, "create_ticket", {}, "已建 TICKET-reuse123，请跟踪")

    result = await session_router.trigger_handoff(conv, "用户明确要求转人工")

    assert result["ok"] and result["ticket_id"] == "TICKET-reuse123"
    assert fake.calls == [], "已有工单时不应再次调用 MCP 建单"


async def test_close_expired_conversations(test_db):
    conv = await test_db.create_conversation("u-1")
    old = (datetime.now() - timedelta(minutes=40)).isoformat(timespec="seconds")
    await test_db.update_conversation(conv, last_message_at=old)

    fresh = await test_db.create_conversation("u-2")
    await test_db.update_conversation(fresh, last_message_at=now_iso())

    closed = await test_db.close_expired_conversations(30)
    assert closed == 1

    expired = await test_db.get_conversation(conv)
    assert expired["status"] == "closed"
    assert expired["end_reason"] == "unresolved"

    alive = await test_db.get_conversation(fresh)
    assert alive["status"] == "active"
