"""CrewAI Flow 会话编排骨架（教学版）。

用 CrewAI Flow 重新表达「一条用户消息进来，一条回复（或转人工）出去」，
节点必须在 ``SessionTurnFlow`` 类体内定义（FlowMeta 通过类命名空间扫描装饰器元数据）。

节点按类型分三类，阶梯式展示 Agent 能力：
  ① 函数节点 —— 零 LLM，负责闸门 / 上下文准备 / 装配交付 / 终端；
  ② Agent 节点 —— 单 Agent + 工具，负责知识库检索（prepare_context）；
                单 Agent 无工具，负责意图感知（perceive_intent）；
  ③ Crew 节点  —— 多 Agent + 多工具，负责回答合成与按需建单（generate_reply）。

节点 DAG（7 核心 + 5 终端）：

    preflight (@start) 函数
        │
        ▼
    gate_intent (@router 函数) ── 五分支 ──▶ handle_closed           （终端 函数）
        │                                  ├▶ forward_to_agent        （终端 函数）
        │                                  ├▶ handle_handoff_keyword  （终端 函数）
        │                                  ├▶ handle_chitchat         （终端 函数）
        │                                  └▶ perceive_intent  [Agent 无工具]
        ▼
    perceive_intent [Agent 无工具]
        │
        ▼
    route_intent (@router 函数) ── 三分支 ──▶ handle_chitchat  （终端 函数）
        │                                  └▶ prepare_context  [Agent 单工具]
        ▼
    prepare_context [Agent · multimodal_search]
        │
        ▼
    generate_reply [Crew · 一线合成 + 工单 + 质检]
        │
        ▼
    gate_and_deliver (@router 函数) ── 两分支 ──▶ trigger_handoff （终端 函数）
                                                 └▶ 完成（deliver.pass）

对外入口：start(conv_id, user_id, **kwargs) → 实例化 SessionTurnFlow → kickoff_async。
回复经 WebSocket 由节点内部推送（与现有 session_router 事件格式一致）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from crewai.flow import Flow, listen, router, start
from pydantic import BaseModel, Field

from core.service import handoff as handoff_mod
from core.ai import llm as llm_mod
from core.service.analytics import push_stats_to_admins as _push_admins_stats
from core.config import settings
from core.infra.db import db, now_iso
from core.infra.media import media_url, save_media
from core.rag.multimodal import process_image, process_voice, synthesize_speech
from core.rag.multimodal_rag import multimodal_rag
from core.service.turn_context import TurnRecorder
from core.rag.vector_store import vector_store
from core.infra.ws_manager import ws_manager

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 路由 key（统一前缀，避免不同 router 的事件名互相串扰）
# ----------------------------------------------------------------------
# route_intent 的分支（提前定义，供 gate_intent 的 emit 复用寒暄事件名）
ROUTE_CHITCHAT = "route.chitchat"         # intent = chitchat / off_topic
ROUTE_IT_ISSUE = "route.it_issue"         # intent = it_issue → 检索
ROUTE_KEYS = (ROUTE_CHITCHAT, ROUTE_IT_ISSUE)

# gate_intent 的分支（寒暄复用 ROUTE_CHITCHAT，让 handle_chitchat 单点监听即命中）
GATE_CLOSED = "flow.closed"            # 会话已关闭
GATE_HUMAN = "flow.human"              # 人工模式/排队中
GATE_HANDOFF_KW = "flow.handoff_kw"    # 关键词命中转人工（零 LLM）
GATE_ASR_FAILED = "flow.asr_failed"    # 语音转写失败：提示重录，不落库/不检索
GATE_PERCEIVE = "flow.perceive"        # 进入 LLM 感知
GATE_ROUTE_KEYS = (
    GATE_CLOSED, GATE_HUMAN, GATE_HANDOFF_KW, GATE_ASR_FAILED, ROUTE_CHITCHAT, GATE_PERCEIVE,
)

# gate_and_deliver 的分支
DELIVER_PASS = "deliver.pass"            # 通过护栏，回复完成
DELIVER_HANDOFF = "deliver.handoff"      # 触发转人工
DELIVER_KEYS = (DELIVER_PASS, DELIVER_HANDOFF)


class TurnState(BaseModel):
    """Flow 的状态载体：输入 + 各节点产出，节点间通过 self.state 传递。"""

    # 输入
    conv_id: str
    user_id: str
    content: str = ""
    modality: str = "text"
    image_path: Optional[str] = None
    image_bytes: Optional[bytes] = None
    audio_bytes: Optional[bytes] = None
    # 用户上传图片/语音的 asset_id：用于落库 image_url/audio_url 并透传坐席（刷新不丢）
    image_asset: Optional[str] = None
    audio_asset: Optional[str] = None

    # preflight 注入的会话上下文
    status: str = "active"              # active / closed
    mode: str = "bot"                   # bot / waiting_human / human
    history: str = ""                   # 最近对话文本
    agent_note: str = ""                # 坐席备注
    user_name: str = ""
    asr_failed: bool = False            # 语音转写失败：提示重录，不落库/不检索

    # gate_intent 零 LLM 闸门产物
    handoff_keyword: bool = False

    # 意图感知（perceive_intent）。转人工的情绪判定只用 emotion 布尔
    # （落库 messages.emotion → get_negative_rounds），不需要 level/score 明细。
    intent: str = "it_issue"            # chitchat / off_topic / it_issue
    emotion: str = "neutral"
    perceive_reply: str = ""            # 感知阶段产出的短回复（非 it_issue 时）
    # 用户消息是否已落库（幂等标志）。不能用「perceive_reply 为空」推断，
    # 否则感知路径返回 chitchat 但缺 reply 字段时会重复落库
    user_msg_persisted: bool = False

    # 检索（prepare_context）
    retrieved: list[dict] = Field(default_factory=list)
    top_score: Optional[float] = None
    screenshot_hint: str = ""
    context_text: str = ""              # 检索结果格式化后的上下文文本

    # 生成（generate_reply）
    reply_raw: str = ""                 # 未加参考来源的正文
    body: str = ""                      # 装配引用后的最终正文
    confidence: Optional[float] = None  # 有据回填 top_score，无据为 None
    attachments: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)

    # 护栏（gate_and_deliver）
    need_handoff: bool = False
    handoff_reason: str = ""
    kb_nonempty: bool = False


# ----------------------------------------------------------------------
# 模块级辅助：推送 / 装配 / 审计
# ----------------------------------------------------------------------
def _append_citations(reply: str, results: list[dict]) -> str:
    """正文末尾追加「参考来源」；未命中不显示。"""
    if not results or not reply:
        return reply
    seen: list[str] = []
    for item in multimodal_rag.build_citations(results):
        source = item.get("source") or "未知"
        page = item.get("page")
        label = f"{source}（第{page}页）" if page else str(source)
        if label not in seen:
            seen.append(label)
    if not seen:
        return reply
    return reply.rstrip() + "\n\n参考来源：" + "、".join(seen)


def _log_flow_step(recorder: Optional[TurnRecorder], step: str, params: dict,
                   latency_ms: int = 0, status: str = "success") -> None:
    logger.info("Flow 节点 %s status=%s %dms", step, status, latency_ms)
    if recorder is not None:
        recorder.record_tool_call(f"flow.{step}", params, "", latency_ms, status)


async def _history_text(conv_id: str, limit: int = 10) -> str:
    messages = await db.get_messages(conv_id, limit=200)
    lines: list[str] = []
    for msg in messages[-limit:]:
        label = {"user": "用户", "assistant": "AI客服", "agent": "人工客服", "system": "系统"}.get(
            msg.get("role", ""), "未知"
        )
        lines.append(f"{label}：{msg.get('content', '')}")
    return "\n".join(lines)


async def _last_agent_note(conv_id: str) -> str:
    rows = await db.query_all(
        "SELECT content FROM messages WHERE conversation_id=? AND role='system' "
        "AND content LIKE '[人工坐席备注]%' ORDER BY id DESC LIMIT 1",
        (conv_id,),
    )
    if not rows:
        return ""
    return rows[0]["content"].replace("[人工坐席备注]", "").strip()


async def _persist_tool_calls(recorder: Optional[TurnRecorder]) -> None:
    if recorder is None:
        return
    for call in recorder.tool_calls:
        await db.add_tool_call(
            conv_id=recorder.conversation_id,
            tool_name=call["tool_name"],
            input_params=call["input_params"],
            output_result=call["output_result"],
            status=call["status"],
            latency_ms=call["latency_ms"],
        )


async def _set_title(st: TurnState) -> None:
    conv = await db.get_conversation(st.conv_id)
    if not conv or (conv.get("title") and conv["title"] != "新会话"):
        return
    title = st.content.strip().replace("\n", " ")[:20]
    if title:
        await db.update_conversation(st.conv_id, title=title)
        await ws_manager.send_to_user(
            st.user_id,
            {"type": "conversation_update", "conversation_id": st.conv_id,
             "data": {"id": st.conv_id, "title": title}},
        )


async def _persist_user_message(st: TurnState) -> None:
    """落库用户消息并用感知后的真实 intent/emotion，附带设标题。

    感知前 intent/emotion 是默认值，必须在此（感知完成后）落库，
    保证 DB 里存的是真实值——should_handoff 的情绪/未解决判定依赖它。
    由各分支节点调用（it_issue / chitchat / handoff / human 各落一次）；
    本函数幂等，重复调用不会产生重复轮次。
    """
    if st.user_msg_persisted:
        return
    await db.add_message(
        st.conv_id, "user", st.content,
        modality=st.modality, intent=st.intent, emotion=st.emotion,
        # 落库图片/语音地址：否则用户刷新或切换会话（从 DB 重载）后附件消失
        image_url=media_url(st.image_asset) if st.image_asset else None,
        audio_url=media_url(st.audio_asset) if st.audio_asset else None,
    )
    await _set_title(st)
    st.user_msg_persisted = True


def _read_bytes(path: Optional[str]) -> Optional[bytes]:
    """从路径读取图片字节（截图链路兜底，见 prepare_context）。"""
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError as exc:  # noqa: BLE001
        logger.warning("读取上传文件失败：%s", exc)
        return None


async def _synthesize_audio_url(text: str) -> Optional[str]:
    """为一段文本生成朗读语音，返回可鉴权访问的 audio_url；失败返回 None。

    让「所有 AI 回复」（流式回答 / 闲聊短回复）统一带语音朗读。网络 I/O，
    失败静默降级（前端无 audio_url 则不显示朗读图标），不阻断主流程。
    """
    if not text or not text.strip():
        return None
    try:
        audio, _ext = await synthesize_speech(text)
        if not audio:
            return None
        return media_url(save_media(audio, "mp3", prefix="audio"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS 合成失败（降级为纯文本）：%s", exc)
        return None


# ----------------------------------------------------------------------
# Flow 定义（节点必须作为类方法）
# ----------------------------------------------------------------------
class SessionTurnFlow(Flow[TurnState]):
    """会话回合 Flow。（节点列表见模块 docstring 的 DAG 图。）"""

    # 本轮工具调用/检索记录器，由 preflight 注入，贯通各节点、供护栏与审计
    recorder: Optional[TurnRecorder] = None

    # ---- 节点 1（函数 @start）：入口准备 --------------------------------
    @start()
    async def preflight(self) -> None:
        """函数节点：会话校验、ASR、拉历史/备注/用户名。

        注意：**不在本节点落库用户消息**——此时 intent/emotion 尚为默认值。
        落库统一放到感知完成后（见 _persist_user_message 与各分支节点），
        保证 DB 里存的 intent/emotion 是感知后的真实值（老编排为「先感知再落库」）。
        """
        st = self.state
        self.recorder = TurnRecorder(conversation_id=st.conv_id, user_id=st.user_id)

        conv = await db.get_conversation(st.conv_id)
        if conv:
            st.status = conv.get("status", "active")
            st.mode = conv.get("mode", "bot")
        # 注入用户显示名（不是内部 user_id），供 CrewAI 任务描述「用户：xxx」使用
        user = await db.get_user(st.user_id)
        if user:
            st.user_name = user.get("display_name") or user.get("username") or ""

        # 语音先转文字（ASR）。识别失败不把提示串当正文，置 asr_failed 由 gate_intent 短路
        if st.modality == "voice" and st.audio_bytes:
            try:
                transcribed = await process_voice(st.audio_bytes)
                if transcribed:
                    st.content = transcribed
                else:
                    st.asr_failed = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("语音转写失败：%s", exc)
                st.asr_failed = True

        st.history = await _history_text(st.conv_id)
        st.agent_note = await _last_agent_note(st.conv_id)
        _log_flow_step(self.recorder, "preflight", {"conv_id": st.conv_id})

    # ---- 节点 2（函数 @router）：模式 + 关键词闸门 ------------------------
    @router("preflight", emit=GATE_ROUTE_KEYS)
    async def gate_intent(self) -> str:
        """函数节点：先判会话/模式，再走三层意图分流的「零 LLM」前两层。"""
        from core.service.session_router import _is_chitchat
        st = self.state

        if st.status == "closed":
            return GATE_CLOSED
        if st.mode in ("waiting_human", "human"):
            return GATE_HUMAN
        if st.asr_failed:
            return GATE_ASR_FAILED

        st.handoff_keyword = await handoff_mod.detect_handoff_intent(st.content)
        if st.handoff_keyword:
            return GATE_HANDOFF_KW
        if _is_chitchat(st.content):
            st.intent = "chitchat"
            # 复用感知路径的寒暄事件名，让 handle_chitchat 单点监听即命中，
            # 避免多条寒暄事件需要 or_ 组合（or_ 在一次性 kickoff 竞态复杂）。
            return ROUTE_CHITCHAT
        return GATE_PERCEIVE

    # ---- 节点 3（Agent 无工具）：意图感知 ---------------------------------
    @listen(GATE_PERCEIVE)
    async def perceive_intent(self) -> None:
        """Agent 节点（无工具）：真 Agent 实例产出 intent + emotion + 短回复。

        教学演示「单 Agent · 无工具」：CrewAI Agent 无工具可调，不会进入工具
        调用循环，等价于一次结构化 LLM 往返。LLM 未配置或调用失败时回退到
        perceive_message（同一次 LLM 往返）或保守 intent=it_issue。
        """
        from core.rag.multimodal import (
            PERCEIVE_FALLBACK,
            _PERCEIVE_PROMPT,
            perceive_message,
        )
        st = self.state

        # 纯截图消息（正文为空）：截图本身即 IT 问题，跳过 LLM 感知直接判 it_issue。
        # 否则空文本会被感知模型判成 off_topic/chitchat，route_intent 随即走寒暄短回复，
        # 检索与截图分析都不会执行（回归：纯截图被判 off_topic 后只回了一句开场白）。
        if (st.image_bytes or st.image_path) and not (st.content or "").strip():
            st.intent = "it_issue"
            st.emotion = "neutral"
            st.perceive_reply = ""
            await _persist_user_message(st)
            _log_flow_step(self.recorder, "perceive_intent",
                           {"intent": "it_issue", "skip_llm": True})
            return

        result: dict = {}
        try:
            from crewai import Agent, Task
            from core.ai.crew_agents import build_llm

            agent = Agent(
                role="IT服务台会话感知专员",
                goal="同时识别用户来意 intent 与情绪 emotion，一次调用产出结构化 JSON",
                backstory="你负责会话前置识别，只依据指令输出结构化 JSON，不调用任何工具。",
                tools=[],           # 无工具；感知是一次纯 LLM 判定
                llm=build_llm(),
                max_iter=1,
                verbose=settings.CREW_VERBOSE,
            )
            # 单 Agent 执行一个 Task（CrewAI 无 agent.run，正确入口是 execute_task）
            task = Task(
                description=f"{_PERCEIVE_PROMPT}\n\n用户原文：{st.content}",
                expected_output="一个 JSON 对象：{\"intent\", \"emotion\", \"level\", \"score\", \"reply\"}",
                agent=agent,
            )
            raw = await asyncio.to_thread(agent.execute_task, task)
            result = llm_mod.extract_json(str(raw or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("感知 Agent 不可用/失败，回退 perceive_message：%s", exc)
            result = await perceive_message(st.content)
        except asyncio.CancelledError:
            raise

        if not result:
            result = dict(PERCEIVE_FALLBACK)
        st.intent = str(result.get("intent") or PERCEIVE_FALLBACK["intent"])
        st.emotion = str(result.get("emotion") or "neutral")
        st.perceive_reply = str(result.get("reply") or "")
        await _persist_user_message(st)
        _log_flow_step(self.recorder, "perceive_intent", {"intent": st.intent})

    # ---- 节点 4（函数 @router）：按感知结果路由 ----------------------------
    @router("perceive_intent", emit=ROUTE_KEYS)
    async def route_intent(self) -> str:
        """函数节点：intent 分流 —— 非 IT 问题直接短回复，it_issue 走检索。"""
        if self.state.intent in ("chitchat", "off_topic"):
            return ROUTE_CHITCHAT
        return ROUTE_IT_ISSUE

    # ---- 节点 5（Agent 单工具）：知识库检索 --------------------------------
    @listen(ROUTE_IT_ISSUE)
    async def prepare_context(self) -> None:
        """Agent 节点（单工具 multimodal_search）：自主检索知识库，装配上下文。

        截图分析无决策空间（有图就分析），作为前置函数计算，不占独立节点。
        检索由 multimodal_rag.search 完成（Agent 经 MultimodalSearchTool 调用），
        结果写入 state，供 generate_reply 使用并作护栏依据。
        """
        st = self.state

        # 截图预分析（前置函数计算，无决策空间）。
        # 截图链路：ws_api 只传 image_path（不传 image_bytes），
        # 这里统一用 image_bytes 优先、再以 _read_bytes(image_path) 兜底补读，
        # 保证截图分析不因字节未传入而失效（与老编排的 _read_bytes 一致）。
        screenshot_bytes = st.image_bytes or _read_bytes(st.image_path)
        if screenshot_bytes:
            try:
                shot = await process_image(screenshot_bytes)
                self.recorder.screenshot = shot  # type: ignore[union-attr]
                # process_image 视觉理解失败时不抛异常，而是在 description 里写
                # 「视觉理解失败：…」。原样注入会被模型转述给用户（回归：出现
                # 「系统在视觉理解时遇到了网络超时」并让用户重发截图）。失败时改用
                # 中性提示，引导其依据知识库作答且不暴露内部错误。
                desc = str(shot.get("description", "") or "")
                if desc.startswith("视觉理解失败"):
                    logger.warning("截图解析未成功，改用中性提示：%s", desc)
                    st.screenshot_hint = json.dumps(
                        {
                            "description": (
                                "（截图未能自动解析，请仅依据知识库给出通用排查步骤，"
                                "不要提及内部错误，也不要让用户重复发送截图）"
                            )
                        },
                        ensure_ascii=False,
                    )
                else:
                    st.screenshot_hint = json.dumps(shot, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("预分析截图失败：%s", exc)

        # Agent 检索：单 Agent · 单工具（multimodal_search），模型自主决定检索词，
        # 经 MultimodalSearchTool 调用后，命中结果由工具写回 recorder.retrieved。
        try:
            from crewai import Agent, Task
            from core.ai.crew_agents import build_llm
            from core.ai.crew_tools import MultimodalSearchTool

            retriever = Agent(
                role="IT知识库检索专员",
                goal="调用 multimodal_search 检索与用户问题最相关的知识片段",
                backstory=(
                    "你是企业IT知识库检索专员。遇到IT问题必须调用 multimodal_search "
                    "检索解决方案，不凭空编造。"
                ),
                tools=[MultimodalSearchTool(recorder=self.recorder)],
                llm=build_llm(),
                max_iter=3,
                verbose=settings.CREW_VERBOSE,
            )
            # 截图消息：把文件路径写进任务描述，检索 Agent 才能把它作为 image_path
            # 传给 multimodal_search（纯截图消息正文为空，仅靠文本 query 检索不到）。
            image_hint = (
                f"\n用户上传的截图文件路径：{st.image_path}"
                "（截图类问题请把该路径作为 image_path 传给 multimodal_search）"
                if screenshot_bytes else ""
            )
            task = Task(
                description=(
                    f"用户问题：{st.content or '（用户上传了一张截图）'}\n"
                    "请调用 multimodal_search 检索相关知识，随后用一句话复述你检索到的来源。"
                    f"{image_hint}"
                ),
                expected_output="一句中文说明：检索命中了哪些知识片段及其来源。",
                agent=retriever,
            )
            await asyncio.to_thread(retriever.execute_task, task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("检索 Agent 不可用/失败，回退直接检索：%s", exc)
            try:
                results = await multimodal_rag.search(
                    query=st.content, query_image=screenshot_bytes,
                )
                if results:
                    self.recorder.retrieved = results
                    self.recorder.top_score = float(results[0].get("score", 0.0))
            except Exception as exc2:  # noqa: BLE001
                logger.warning("兜底检索失败：%s", exc2)

        # 检索结果以 recorder 为准（Agent 工具或兜底检索均写回 recorder）
        if self.recorder and self.recorder.retrieved:
            st.retrieved = self.recorder.retrieved
            st.top_score = self.recorder.top_score
        st.context_text = (
            multimodal_rag.format_results(st.retrieved)
            if st.retrieved else "（知识库未命中相关内容）"
        )
        _log_flow_step(self.recorder, "prepare_context",
                       {"retrieved": len(st.retrieved), "top_score": st.top_score})

    # ---- 节点 6（Crew 节点）：回答合成 + 按需建单 ---------------------------
    @listen("prepare_context")
    async def generate_reply(self) -> None:
        """Crew 节点：复用现有 crew_agents.run_crew_turn，多 Agent 协作产出回复。

        Crew 内部（见 crew_agents.py）：
            - 一线合成 —— 基于 state.retrieved 把知识翻译成排查步骤，
                         判断检索是否够用，不够令 [ESCALATE: yes]；
            - 工单处理 —— 按需触发，仅 [ESCALATE: yes] 时执行建单/改密/权限；
            - 质检     —— 默认关闭，旁路复核。
        流式：on_delta 逐 token 推 bot_stream_delta，与现有编排共用同一套事件。
        """
        st = self.state

        async def _on_delta(text: str) -> None:
            await ws_manager.send_to_user(
                st.user_id,
                {"type": "bot_stream_delta", "conversation_id": st.conv_id,
                 "data": {"delta": text}},
            )

        user_parts = [f"【知识库检索结果】\n{st.context_text}"]
        if st.agent_note:
            user_parts.append(f"【人工坐席备注】{st.agent_note}")
        if st.history:
            user_parts.append(f"【最近对话】\n{st.history}")
        if st.screenshot_hint:
            user_parts.append(f"【系统预分析（截图，已由视觉模型完成）】\n{st.screenshot_hint}")
        user_parts.append(f"【用户本次消息】{st.content or '（用户发送了一张截图）'}")
        task_input = "\n\n".join(user_parts)

        reply = ""
        if settings.CREW_ENABLED:
            try:
                from core.ai.crew_agents import run_crew_turn
                result = await run_crew_turn(
                    recorder=self.recorder,  # type: ignore[arg-type]
                    user_message=st.content,
                    history_text=st.history,
                    image_path=st.image_path,
                    agent_note=st.agent_note,
                    user_name=st.user_name,
                    knowledge_hint="",   # 检索已由 prepare_context 完成，不重复预检索
                    screenshot_hint=st.screenshot_hint,
                    on_delta=_on_delta,
                )
                reply = result.reply.strip() if (result.ok and result.reply) else ""
            except asyncio.CancelledError:
                logger.info("用户停止生成，已中断 CrewAI 回合：conv=%s", st.conv_id)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("CrewAI 回合异常：%s", exc)

        if not reply and llm_mod.available():
            # 直连回退（CREW_ENABLED=false，或 Crew 失败）：直连 LLM 流式
            buffered: list[str] = []
            try:
                async for piece in llm_mod.chat_stream(
                    [
                        {"role": "system",
                         "content": "你是企业IT服务台的数字员工（AI客服）。只依据提供的知识库内容作答，不要编造；信息不足时主动追问。"},
                        {"role": "user", "content": task_input},
                    ]
                ):
                    buffered.append(piece)
                    await _on_delta(piece)
                reply = "".join(buffered).strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("流式生成失败：%s", exc)
                reply = "".join(buffered).strip()

        st.reply_raw = reply

        # Crew 路径下若 Agent 本轮未调检索，补一次兜底检索，仅供护栏（不作正文依据）
        if settings.CREW_ENABLED and not st.retrieved:
            try:
                found = await multimodal_rag.search(
                    query=st.content,
                    query_image=st.image_bytes or _read_bytes(st.image_path),
                )
                if found:
                    st.retrieved = found
                    st.top_score = float(found[0].get("score", 0.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Crew 兜底检索失败：%s", exc)

        _log_flow_step(self.recorder, "generate_reply", {"reply_len": len(reply)})

    # ---- 节点 7（函数 @router）：置信度护栏 + 装配交付 ----------------------
    @router("generate_reply", emit=DELIVER_KEYS)
    async def gate_and_deliver(self) -> str:
        """函数节点：置信度护栏判定 + 装配引用/附件 + 入库 + 推送。"""
        st = self.state

        if not st.reply_raw:
            st.reply_raw = "抱歉，AI 服务暂时不可用。已为您记录问题，将转交人工处理。"

        answer_grounded = (not settings.CREW_ENABLED) or bool(st.retrieved)
        st.kb_nonempty = vector_store.count() > 0

        need, reason = await handoff_mod.should_handoff(
            st.conv_id,
            turn_confidence=st.top_score if st.retrieved else None,
            user_text=st.content,
            kb_nonempty=st.kb_nonempty,
            intent=st.intent,
        )
        st.need_handoff = need
        st.handoff_reason = reason

        if answer_grounded:
            st.attachments = multimodal_rag.build_attachments(st.retrieved) if st.retrieved else []
            st.citations = multimodal_rag.build_citations(st.retrieved) if st.retrieved else []
            st.body = _append_citations(st.reply_raw, st.retrieved) if st.retrieved else st.reply_raw
            st.confidence = st.top_score if st.retrieved else None
        else:
            st.attachments, st.citations, st.body, st.confidence = [], [], st.reply_raw, None

        # TTS 出口：为 AI 回答生成朗读语音（教学闭环的「出」）。
        # 失败静默降级为纯文本，不阻断主流程（前端无 audio_url 则不显示朗读图标）。
        audio_url = await _synthesize_audio_url(st.body)

        await db.add_message(
            st.conv_id, "assistant", st.body,
            audio_url=audio_url,
            attachments=st.attachments, citations=st.citations, confidence=st.confidence,
        )
        await _persist_tool_calls(self.recorder)
        await ws_manager.send_to_user(
            st.user_id,
            {
                "type": "bot_stream_end",
                "conversation_id": st.conv_id,
                "data": {
                    "role": "assistant",
                    "content": st.body,
                    "modality": "text",
                    "audio_url": audio_url,
                    "attachments": st.attachments,
                    "citations": st.citations,
                    "confidence": st.confidence,
                    "timestamp": now_iso(),
                },
            },
        )
        await _push_admins_stats()
        _log_flow_step(self.recorder, "gate_and_deliver",
                       {"need_handoff": need, "reason": reason})
        return DELIVER_HANDOFF if need else DELIVER_PASS

    # ---- 终端节点（函数） --------------------------------------------------
    @listen(GATE_CLOSED)
    async def handle_closed(self) -> None:
        """会话已关闭：提示用户，不生成回复。"""
        st = self.state
        await ws_manager.send_to_user(
            st.user_id,
            {"type": "system", "conversation_id": st.conv_id,
             "data": {"role": "system", "content": "该会话已结束，如需继续请新建会话。"}},
        )

    @listen(GATE_ASR_FAILED)
    async def handle_asr_failed(self) -> None:
        """语音转写失败：提示用户重录，不落库、不进检索/LLM（避免错误提示串当正文）。"""
        st = self.state
        await ws_manager.send_to_user(
            st.user_id,
            {"type": "system", "conversation_id": st.conv_id,
             "data": {"role": "system",
                      "content": "语音识别失败，请重新录制或改用文字输入。"}},
        )

    @listen(GATE_HUMAN)
    async def forward_to_agent(self) -> None:
        """人工/排队模式：AI 静默，消息推给坐席。用户消息在此落库（感知/闸门未走）。"""
        st = self.state
        await _persist_user_message(st)
        await ws_manager.broadcast_to_agents(
            {
                "type": "user_message",
                "conversation_id": st.conv_id,
                "data": {"role": "user", "content": st.content, "modality": st.modality,
                         "image_url": media_url(st.image_asset) if st.image_asset else None,
                         "audio_url": media_url(st.audio_asset) if st.audio_asset else None,
                         "emotion": st.emotion},
            }
        )
        if st.mode == "waiting_human":
            await ws_manager.send_to_user(
                st.user_id,
                {"type": "system", "conversation_id": st.conv_id,
                 "data": {"role": "system", "content": "正在为您转接人工客服，请稍候…"}},
            )
        await _push_admins_stats()

    @listen(GATE_HANDOFF_KW)
    async def handle_handoff_keyword(self) -> None:
        """关键词命中转人工：零 LLM 直达 trigger_handoff。用户消息在此落库。"""
        await _persist_user_message(self.state)
        from core.service.session_router import trigger_handoff
        await trigger_handoff(self.state.conv_id, "用户明确要求转人工")
        await _push_admins_stats()

    @listen(ROUTE_CHITCHAT)
    async def handle_chitchat(self) -> None:
        """寒暄/无关话题：短回复，不检索、不进护栏。

        用户消息在此落库（幂等）：感知路径已落库则自动跳过，关键词路径在此落库。
        """
        st = self.state
        await _persist_user_message(st)
        reply = st.perceive_reply
        if not reply:
            reply = "您好，我是IT服务台数字员工，请问您遇到了什么IT问题？"
        # 闲聊也可朗读：同样带 TTS 语音（失败降级为纯文本）
        audio_url = await _synthesize_audio_url(reply)
        await db.add_message(st.conv_id, "assistant", reply, audio_url=audio_url)
        await ws_manager.send_to_user(
            st.user_id,
            {"type": "bot_reply", "conversation_id": st.conv_id,
             "data": {"role": "assistant", "content": reply, "modality": "text",
                      "audio_url": audio_url,
                      "attachments": [], "citations": [], "timestamp": now_iso()}},
        )
        await _push_admins_stats()

    @listen(DELIVER_HANDOFF)
    async def trigger_handoff(self) -> None:
        """护栏触发转人工：走 session_router.trigger_handoff（切状态/摘要/建单/推坐席）。"""
        st = self.state
        from core.service.session_router import trigger_handoff
        await trigger_handoff(st.conv_id, st.handoff_reason)
        await _push_admins_stats()


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
async def start(
    conv_id: str,
    user_id: str,
    content: str = "",
    modality: str = "text",
    image_path: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    audio_bytes: Optional[bytes] = None,
    image_asset: Optional[str] = None,
    audio_asset: Optional[str] = None,
) -> None:
    """启动一轮会话回合（对外入口）。"""
    state = TurnState(
        conv_id=conv_id,
        user_id=user_id,
        content=content,
        modality=modality,
        image_path=image_path,
        image_bytes=image_bytes,
        audio_bytes=audio_bytes,
        image_asset=image_asset,
        audio_asset=audio_asset,
    )
    # Flow 通过 initial_state 接收状态载体的 BaseModel 实例（pydantic 字段）
    flow = SessionTurnFlow(initial_state=state)
    try:
        await flow.kickoff_async()
    except asyncio.CancelledError:
        logger.info("用户停止生成，已中断 Flow 回合：conv=%s", conv_id)
    except Exception:  # noqa: BLE001
        logger.exception("Flow 回合异常：conv=%s", conv_id)
