"""CrewAI 多 Agent 编排（见设计文档 4.1 / 4.2）。

**接入方式（双路径开关）**：由 ``CREW_ENABLED`` 决定会话回复走哪条路——
``false``（默认）由 ``session_flow`` 走中台直连 LLM **流式生成**（逐 token 推送、首字快）；
``true`` 时由 ``session_flow`` 调本模块的 ``run_crew_turn``，由一线/工单/质检 Agent
协作产出回复（Agent 自主调用检索/截图/工单工具）。

流式：CrewAI 本身支持流式（``Crew(stream=True)`` → ``CrewStreamingOutput``，可 ``async for``
逐块迭代）。本模块用 ``await crew.kickoff_async()`` 真流式产出，逐块回调给中台推
``bot_stream_delta``。之所以要额外打一个 provider 补丁（``_strip_stream_usage``），是
**供应商侧不兼容**：硅基流动在流式响应中给每个 chunk 都回传 ``usage``，命中 CrewAI 流式
解析里「带 usage 即跳过」的分支（``_handle_streaming_completion`` 内 ``if usage: ... continue``），
导致正文 chunk 被整段丢弃、``full_response`` 恒为空。补丁对流式结果做生成器包装，凡
「有 usage 且有 choices」的 chunk 把 usage 置 None，即可让 CrewAI 正常累计正文。

三个角色：
- 一线客服 Agent：意图识别 + 多模态检索 + 截图分析，简单问题直接答复；
- 工单处理 Agent：**按需触发**（一线以 [ESCALATE: yes] 表态时才执行），
  通过 MCP 工具完成建单 / 查单 / 更新 / 密码重置 / 权限申请；
- 质检 Agent：旁路复核回复质量（不拦截、不改写，见 7.7 ⑤），默认关闭以省一次 LLM 往返。

顺序流程（Process.sequential），降低教学复杂度。
多轮上下文由**中台把历史消息摘要注入任务描述**实现，而非依赖 CrewAI memory
（避免其内置向量记忆与项目的 ChromaDB 冲突）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# 关闭 CrewAI 遥测，避免演示环境联网上报
os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

from core.config import settings
from core.ai.crew_tools import build_frontline_tools, build_ticket_tools
from core.service.turn_context import TurnRecorder

logger = logging.getLogger(__name__)


class CrewUnavailable(RuntimeError):
    """CrewAI 不可用（如 LLM 未配置）。"""


@dataclass
class CrewTurnResult:
    reply: str = ""
    qa_note: str = ""
    ok: bool = True
    error: str = ""
    raw_outputs: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------
def llm_configured() -> bool:
    return bool(settings.LLM_MODEL and settings.LLM_BASE_URL and settings.llm_key)


_NATIVE_TOOL_PATCH_FLAG = "_mde_no_native_tools"


def _force_react_text_tool_calling() -> None:
    """让 CrewAI 放弃原生函数调用、改用 ReAct 文本工具调用。

    硅基流动托管的 Qwen3-VL 系列（8B/30B/32B）不返回 OpenAI 原生 `tool_calls`，
    而是把工具调用写成 `<tool_call>{...}</tool_call>` 塞进 `reasoning_content`
    （`content` 为空、`tool_calls` 为 null）。CrewAI 依「非 o1 模型即支持原生函数
    调用」判定走原生路径，于是拿到空回复并抛
    `Invalid response from LLM call - None or empty`。

    这段文案不在 CrewAI 的「不支持原生工具」自动降级模式里，因此这里显式把 provider
    类的 `supports_function_calling` 置为 False，使 CrewAI 退回 ReAct 文本工具调用
    （工具描述写入提示词，模型以 Action/Action Input 文本触发调用）。

    注：`crewai.LLM(model=...)` 会按模型名路由到 provider 子类（本处为
    `OpenAICompletion`），子类化 LLM 不会生效，故在 provider 类上打补丁。
    """
    from crewai.llms.providers.openai.completion import OpenAICompletion

    current = OpenAICompletion.supports_function_calling
    if getattr(current, _NATIVE_TOOL_PATCH_FLAG, False):
        return

    def _no_native_tools(self) -> bool:  # noqa: ANN001
        return False

    setattr(_no_native_tools, _NATIVE_TOOL_PATCH_FLAG, True)
    OpenAICompletion.supports_function_calling = _no_native_tools


_STREAM_USAGE_PATCH_FLAG = "_mde_stream_usage_stripped"


def _strip_redundant_usage(stream: Any) -> Any:
    """生成器包装：剥掉「带 usage 的正文 chunk」上的 usage 字段。

    硅基流动在流式响应里给**每个** chunk 都回传 usage，命中 CrewAI 流式解析
    （``_handle_streaming_completion``）里「带 usage 即 continue」的分支，导致正文被
    整段丢弃、``full_response`` 恒为空。把这类 chunk 的 usage 置 None 后，CrewAI 恢复
    正常累计正文；末尾仅含 usage、无 choices 的统计 chunk 保持原样，以保留 token 计费口径。
    """
    for chunk in stream:
        try:
            if getattr(chunk, "usage", None) and getattr(chunk, "choices", None):
                chunk.usage = None
        except Exception:  # noqa: BLE001
            pass
        yield chunk


def _patch_stream_usage() -> None:
    """在 provider 类上打补丁，绕开供应商逐 chunk 回传 usage 导致的流式丢正文。

    与 ``_force_react_text_tool_calling`` 同属「provider 类级补丁」：CrewAI 内部固定实例化
    ``OpenAICompletion``，子类化 LLM 不生效，故直接包装其 ``_get_sync_client``，对流式结果
    套一层生成器包装（见 ``_strip_redundant_usage``）。幂等：客户端对象打标记，重复调用不叠加。
    """
    from crewai.llms.providers.openai.completion import OpenAICompletion

    original = OpenAICompletion._get_sync_client
    if getattr(original, _STREAM_USAGE_PATCH_FLAG, False):
        return

    def _wrapped(self: Any) -> Any:  # noqa: ANN001
        client = original(self)
        if getattr(client, _STREAM_USAGE_PATCH_FLAG, False):
            return client
        create = client.chat.completions.create

        def _create(*args: Any, **kwargs: Any) -> Any:
            result = create(*args, **kwargs)
            return _strip_redundant_usage(result) if kwargs.get("stream") else result

        client.chat.completions.create = _create
        setattr(client, _STREAM_USAGE_PATCH_FLAG, True)
        return client

    setattr(_wrapped, _STREAM_USAGE_PATCH_FLAG, True)
    OpenAICompletion._get_sync_client = _wrapped


def build_llm():
    """构造 OpenAI 兼容 LLM。生成输入为文本（图片以配图描述注入，见 7.7），无需视觉能力。"""
    if not llm_configured():
        raise CrewUnavailable(
            "LLM 未配置：请设置 LLM_MODEL / LLM_BASE_URL，并提供 LLM_API_KEY 或 SILICONFLOW_API_KEY。"
        )
    from crewai import LLM

    _force_react_text_tool_calling()
    _patch_stream_usage()

    model = settings.LLM_MODEL
    if not model.startswith("openai/"):
        model = f"openai/{model}"
    return LLM(
        model=model,
        base_url=settings.LLM_BASE_URL,
        api_key=settings.llm_key,
        temperature=0.3,
    )


# ----------------------------------------------------------------------
# Agent / Task / Crew
# ----------------------------------------------------------------------
def _build_agents(llm: Any, recorder: TurnRecorder) -> dict[str, Any]:
    from crewai import Agent

    frontline = Agent(
        role="一线IT客服代表",
        goal="快速理解用户问题（含截图与语音），给出准确的解决方案或正确路由",
        backstory=(
            "你是一名经验丰富的企业IT服务台客服，熟悉VPN、密码重置、软件安装、报销系统等常见问题。"
            "你态度友好，善于用简洁的语言解释技术问题。你能看懂用户上传的报错截图，"
            "并在多模态知识库中检索文本与图片方案。当问题超出你的能力范围时，"
            "你会准确地转交给工单处理专家或建议转人工。"
        ),
        tools=build_frontline_tools(recorder),
        llm=llm,
        verbose=settings.CREW_VERBOSE,
        allow_delegation=True,
        max_iter=settings.CREW_MAX_ITER,
    )

    ticket = Agent(
        role="IT工单处理专家",
        goal="根据用户需求创建、查询和更新IT服务工单",
        backstory=(
            "你是IT服务台的工单处理专家，负责将用户的问题转化为标准化工单。"
            "你熟悉工单的优先级判定、分类标准和SLA规则。你能判断什么情况下需要创建新工单、"
            "什么情况下更新已有工单。priority 取 high/medium/low，"
            "category 取 network/password/software/finance/email/general。"
        ),
        tools=build_ticket_tools(recorder),
        llm=llm,
        verbose=settings.CREW_VERBOSE,
        allow_delegation=False,
        max_iter=settings.CREW_MAX_ITER,
    )

    qa = Agent(
        role="服务质检专员",
        goal="审查AI回复的质量，确保准确、专业、无幻觉",
        backstory=(
            "你是客服质检专员，负责检查AI客服的回复是否准确、是否引用了正确的知识库内容、"
            "是否存在编造信息的情况。你的审查结果用于教学分析，只给意见，不修改回复。"
        ),
        tools=[],
        llm=llm,
        verbose=settings.CREW_VERBOSE,
        allow_delegation=False,
        max_iter=2,
    )

    return {"frontline": frontline, "ticket": ticket, "qa": qa}


def _build_tasks(agents: dict[str, Any], task_input: str) -> list[Any]:
    from crewai import Task

    handle_task = Task(
        description=(
            "你正在处理一位企业员工在IT服务台的咨询。\n"
            "【对话背景】\n"
            f"{task_input}\n\n"
            "【工作要求】\n"
            "1. 先用一句话复述你理解的问题；\n"
            "2. 截图分析：若【系统预分析（截图）】已给出结果，**直接采用，不要重复调用** "
            "analyze_screenshot；仅当没有预分析结果时才调用该工具；\n"
            "3. 知识库检索：multimodal_search 是排查问题的**首要手段**。【知识库预检索参考】"
            "只是系统预先给出的线索，**不得直接当作结论**；凡涉及具体报错、配置项或操作步骤，"
            "你**必须调用 multimodal_search 亲自核实并补充**，再据此作答；仅当预检索参考已完整"
            "覆盖该问题的排查步骤、且你已核对无误时，才可跳过；\n"
            "4. 若知识库能解决，给出简明可执行的排查步骤（可用编号列表）；\n"
            "5. 若知识库无法解决或用户要求线下跟进，明确说明将转为工单/人工处理；\n"
            "6. 不要编造知识库中不存在的内容。\n"
            "【输出】先输出给用户看的中文回复正文（不要输出思考过程、不要输出 Markdown "
            "代码块围栏），随后另起一行单独输出标记：需要线下跟进/建单时输出 [ESCALATE: yes]，"
            "否则输出 [ESCALATE: no]。"
        ),
        expected_output=(
            "面向用户的中文回复正文，正文之后另起一行输出 [ESCALATE: yes] 或 [ESCALATE: no]。"
        ),
        agent=agents["frontline"],
    )

    tasks: list[Any] = [handle_task]

    if settings.CREW_QA_ENABLED:
        qa_task = Task(
            description=(
                "审查上一环节给用户的回复：是否准确、是否引用了知识库、是否存在编造。"
                "【输出】不超过 80 字的质检意见（只给意见，不要改写回复）。"
            ),
            expected_output="不超过 80 字的中文质检意见。",
            agent=agents["qa"],
            context=[handle_task],
        )
        tasks.append(qa_task)

    return tasks


def _build_ticket_task(agent: Any, task_input: str, frontline_reply: str) -> Any:
    """按需触发的工单任务（见 4.2）：只在一线判定需线下跟进时才构造并执行。"""
    from crewai import Task

    return Task(
        description=(
            "一线客服本轮给用户的回复如下：\n"
            f"{frontline_reply}\n\n"
            "【本轮对话背景】\n"
            f"{task_input}\n\n"
            "【工作要求】结合以上内容判断是否需要操作工单：\n"
            "- 若问题无法即时解决、用户明确要求线下跟进、或需要密码重置/软件权限申请，"
            "调用相应工具（create_ticket 等）处理，注意按规则判定 priority 与 category；\n"
            "- 若问题已被知识库解决且用户未要求跟进，则不创建工单。\n"
            "【输出】用一句话说明你的操作结果（例如：已创建工单 TICKET-xxxx）。"
            "若无需操作，输出“无需建单”。"
        ),
        expected_output="工单操作结果说明，或“无需建单”。",
        agent=agent,
    )


def build_crew(recorder: TurnRecorder, task_input: str):
    from crewai import Crew, Process

    llm = build_llm()
    agents = _build_agents(llm, recorder)
    tasks = _build_tasks(agents, task_input)
    # 只挂载本轮真正有任务的 Agent；工单 Agent 由 _run_ticket_agent 按需单独执行
    used = [agents["frontline"]]
    if settings.CREW_QA_ENABLED:
        used.append(agents["qa"])

    return Crew(
        agents=used,
        tasks=tasks,
        process=Process.sequential,
        verbose=settings.CREW_VERBOSE,
        memory=False,  # 多轮上下文由中台注入任务描述（见模块 docstring）
        max_rpm=settings.CREW_MAX_RPM,
    )


_ESCALATE_RE = re.compile(r"\[\s*ESCALATE\s*:\s*(yes|no)\s*\]", re.IGNORECASE)


def _split_escalate(text: str) -> tuple[str, bool]:
    """拆出一线输出末尾的 ``[ESCALATE: yes|no]`` 标记，返回 ``(正文, 是否需线下跟进)``。

    标记缺失时按 **True** 处理（保守：宁可多跑一次工单 Agent，也不漏建单）。
    """
    raw = (text or "").strip()
    match = _ESCALATE_RE.search(raw)
    if not match:
        return raw, True
    body = (raw[: match.start()] + raw[match.end():]).strip()
    return body, match.group(1).lower() == "yes"


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def build_task_input(
    user_message: str,
    history_text: str = "",
    image_path: Optional[str] = None,
    agent_note: str = "",
    user_name: str = "",
    knowledge_hint: str = "",
    screenshot_hint: str = "",
) -> str:
    """拼装任务描述（避免使用花括号，防止 CrewAI 模板插值报错）。"""
    parts: list[str] = []
    if user_name:
        parts.append(f"用户：{user_name}")
    if agent_note:
        parts.append(f"【人工坐席备注（重要上下文）】{agent_note}")
    if history_text:
        parts.append(f"【最近对话记录】\n{history_text}")
    parts.append(f"【用户本次消息】{user_message}")
    if image_path:
        parts.append(f"【用户上传的截图文件路径】{image_path}")
    if screenshot_hint:
        parts.append(f"【系统预分析（截图，已由视觉模型完成，请勿重复分析）】\n{screenshot_hint}")
    if knowledge_hint:
        parts.append(
            "【知识库预检索参考（系统预先检索的线索，仅供参考；可调用 multimodal_search "
            f"核实或补充）】\n{knowledge_hint}"
        )
    return "\n\n".join(parts)


# 尾部缓冲长度：`[ESCALATE: yes]` 最长约 16 字符（含空白），留足余量确保该标记不会
# 在流式过程中被逐字推给用户。
_ESCALATE_HOLDBACK = 32


async def _consume_stream(
    streaming: Any, on_delta: Callable[[str], Awaitable[None]]
) -> str:
    """消费 CrewAI 流式输出：逐块回调一线 Agent 的可见正文，返回完整原文。

    仅推送 ``task_index == 0``（一线 Agent 的 handle_task）的 TEXT 块；质检任务
    （task_index 1）与工具调用块不外泄。尾部 ``_ESCALATE_HOLDBACK`` 个字符暂缓推送，
    待流结束后剥离 ``[ESCALATE: …]`` 标记再补推，避免标记出现在用户气泡里。
    """
    from crewai.types.streaming import StreamChunkType

    full = ""
    sent = 0
    async for chunk in streaming:
        if getattr(chunk, "chunk_type", None) != StreamChunkType.TEXT:
            continue
        if getattr(chunk, "task_index", 0) != 0:
            continue
        piece = getattr(chunk, "content", "") or ""
        if not piece:
            continue
        full += piece
        safe_len = len(full) - _ESCALATE_HOLDBACK
        if safe_len > sent:
            await on_delta(full[sent:safe_len])
            sent = safe_len
    visible, _ = _split_escalate(full)
    if len(visible) > sent:
        await on_delta(visible[sent:])
    return full


async def run_crew_turn(
    recorder: TurnRecorder,
    user_message: str,
    history_text: str = "",
    image_path: Optional[str] = None,
    agent_note: str = "",
    user_name: str = "",
    knowledge_hint: str = "",
    screenshot_hint: str = "",
    on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
) -> CrewTurnResult:
    """执行一轮 CrewAI 协作，返回一线客服的回复正文与质检意见。

    工单 Agent **按需触发**：一线 Agent 在输出末尾以 ``[ESCALATE: yes|no]`` 表态，
    仅 yes（或标记缺失）时才另起一轮工单 Agent，避免大量咨询白跑一次 LLM 往返。

    流式：传入 ``on_delta`` 时走**真流式**（``Crew(stream=True)`` + ``crew.kickoff_async``
    → ``CrewStreamingOutput``），一线 Agent 的正文逐块回调（见 ``_consume_stream``）。
    ``on_delta`` 省略时退化为一次性产出（保留给非流式调用方）。
    """
    task_input = build_task_input(
        user_message=user_message,
        history_text=history_text,
        image_path=image_path,
        agent_note=agent_note,
        user_name=user_name,
        knowledge_hint=knowledge_hint,
        screenshot_hint=screenshot_hint,
    )

    try:
        crew = build_crew(recorder, task_input)
    except CrewUnavailable as exc:
        logger.error("CrewAI 不可用：%s", exc)
        return CrewTurnResult(ok=False, error=str(exc))

    use_stream = on_delta is not None
    crew.stream = use_stream

    started = time.perf_counter()
    streamed = ""
    try:
        if use_stream:
            # kickoff_async 在后台线程跑同步 kickoff（不阻塞事件循环），返回可 async 迭代的
            # CrewStreamingOutput；正文 chunk 边产出边回调。
            streaming = await crew.kickoff_async()
            streamed = await _consume_stream(streaming, on_delta)
        else:
            # kickoff 为同步阻塞调用，放线程池执行，避免阻塞事件循环
            await asyncio.to_thread(crew.kickoff)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("CrewAI 执行失败")
        return CrewTurnResult(ok=False, error=f"CrewAI 执行失败：{exc}")

    outputs: list[str] = []
    for task in crew.tasks:
        output = getattr(task, "output", None)
        text = ""
        if output is not None:
            text = getattr(output, "raw", None) or str(output)
        outputs.append((text or "").strip())

    first = outputs[0] if outputs else ""
    if not first and streamed:
        # 流式过程中拿到的原文兜底（task.output 偶发未回填时）
        first = streamed
        if outputs:
            outputs[0] = first
    reply, escalate = _split_escalate(first)
    qa_note = outputs[1] if settings.CREW_QA_ENABLED and len(outputs) > 1 else ""

    if not reply:
        return CrewTurnResult(ok=False, error="CrewAI 未产出回复内容", raw_outputs=outputs)

    ticket_note = ""
    if escalate:
        ticket_note = await _run_ticket_agent(recorder, task_input, reply)

    logger.info(
        "CrewAI 回合完成：%.1fs（工单 Agent：%s）",
        time.perf_counter() - started,
        "执行" if escalate else "跳过",
    )
    if ticket_note:
        outputs.append(ticket_note)
    return CrewTurnResult(reply=reply, qa_note=qa_note, ok=True, raw_outputs=outputs)


async def _run_ticket_agent(recorder: TurnRecorder, task_input: str, frontline_reply: str) -> str:
    """按需执行的工单 Agent（见 4.2）；失败只记日志，不影响主回复。"""
    from crewai import Crew, Process

    try:
        llm = build_llm()
        agent = _build_agents(llm, recorder)["ticket"]
        task = _build_ticket_task(agent, task_input, frontline_reply)
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=settings.CREW_VERBOSE,
            memory=False,
            max_rpm=settings.CREW_MAX_RPM,
        )
        await asyncio.to_thread(crew.kickoff)
        output = getattr(task, "output", None)
        return (getattr(output, "raw", None) or str(output or "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("工单 Agent 执行失败：%s", exc)
        return ""
