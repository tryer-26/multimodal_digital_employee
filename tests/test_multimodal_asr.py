"""ASR 返回值语义测试：失败必须抛 PerceptionError，不得返回错误提示串。

回归背景：``process_voice`` 曾把「语音识别失败：…」当成正常返回值，
调用方 ``session_flow.preflight`` 会把它误当用户转写内容落库、送 LLM/检索。
"""
from __future__ import annotations

import pytest

import core.rag.multimodal as mm
from core.rag.multimodal import PerceptionError, process_voice


class _FakeSettings:
    ASR_PROVIDER = "api"


async def test_process_voice_raises_on_empty_audio():
    with pytest.raises(PerceptionError):
        await process_voice(b"")


async def test_process_voice_raises_on_api_failure(monkeypatch):
    monkeypatch.setattr(mm, "settings", _FakeSettings())

    def _boom(audio: bytes, filename: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(mm, "_asr_api_sync", _boom)
    with pytest.raises(PerceptionError):
        await process_voice(b"audio-bytes")


async def test_process_voice_raises_on_blank_text(monkeypatch):
    monkeypatch.setattr(mm, "settings", _FakeSettings())
    monkeypatch.setattr(mm, "_asr_api_sync", lambda audio, filename: "   ")
    with pytest.raises(PerceptionError):
        await process_voice(b"audio-bytes")


async def test_process_voice_returns_stripped_text(monkeypatch):
    monkeypatch.setattr(mm, "settings", _FakeSettings())
    monkeypatch.setattr(mm, "_asr_api_sync", lambda audio, filename: "  VPN 连不上 ")
    assert await process_voice(b"audio-bytes") == "VPN 连不上"
