"""用户图片/语音的落库与透传测试（见 8.x 多模态消息）。

回归背景：用户上传的图片/语音只解析成内存字节，未把 asset_id 带进消息，
导致 (1) 刷新或切换会话后附件消失；(2) 转人工时坐席看不到用户图片/语音。
"""
from __future__ import annotations

import pytest

import core.service.session_flow as sf
from core.infra.db import Database, now_iso


@pytest.fixture()
async def test_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    await database.execute(
        """INSERT INTO users (id, username, password_hash, display_name, email,
           department, role, status, created_at)
           VALUES (?,?,?,?,'','', 'user', 'active', ?)""",
        ("u-1", "u-1", "x", "u-1", now_iso()),
    )
    monkeypatch.setattr(sf, "db", database)
    return database


class _FakeWS:
    def __init__(self) -> None:
        self.agent_broadcasts: list[dict] = []
        self.user_messages: list[dict] = []

    async def broadcast_to_agents(self, message: dict) -> None:
        self.agent_broadcasts.append(message)

    async def send_to_user(self, user_id: str, message: dict) -> None:
        self.user_messages.append(message)


async def _noop_stats() -> None:
    return None


async def test_user_media_persisted_to_db(test_db):
    conv = await test_db.create_conversation("u-1")
    st = sf.TurnState(
        conv_id=conv, user_id="u-1", content="VPN 连不上",
        modality="image", image_asset="img-abc123", audio_asset="aud-xyz789",
    )

    await sf._persist_user_message(st)
    # 幂等：重复调用不产生第二条用户消息
    await sf._persist_user_message(st)

    messages = await test_db.get_messages(conv)
    users = [m for m in messages if m["role"] == "user"]
    assert len(users) == 1
    assert users[0]["image_url"] == "/api/media/img-abc123"
    assert users[0]["audio_url"] == "/api/media/aud-xyz789"


async def test_forward_to_agent_carries_media(test_db, monkeypatch):
    ws = _FakeWS()
    monkeypatch.setattr(sf, "ws_manager", ws)
    monkeypatch.setattr(sf, "_push_admins_stats", _noop_stats)

    conv = await test_db.create_conversation("u-1")
    st = sf.TurnState(
        conv_id=conv, user_id="u-1", content="", modality="voice",
        mode="human", image_asset="img-abc123", audio_asset="aud-xyz789",
    )
    flow = sf.SessionTurnFlow(initial_state=st)
    await flow.forward_to_agent()

    assert ws.agent_broadcasts, "应向坐席广播用户消息"
    data = ws.agent_broadcasts[0]["data"]
    assert data["image_url"] == "/api/media/img-abc123"
    assert data["audio_url"] == "/api/media/aud-xyz789"


async def test_gate_routes_asr_failure(test_db):
    conv = await test_db.create_conversation("u-1")
    st = sf.TurnState(conv_id=conv, user_id="u-1", modality="voice", asr_failed=True)
    flow = sf.SessionTurnFlow(initial_state=st)
    assert await flow.gate_intent() == sf.GATE_ASR_FAILED
