"""进程内 CrewAI Tool（见设计文档 4.4）。

- 多模态检索与截图分析是**中台内部能力**，不额外起 MCP Server，以进程内 Tool 接入；
- 工单相关 Tool 由中台通过 MCP Client 动态注册（见 4.3）；
- 每次调用都写入 tool_calls（满足第十五章「全程可旁观」），并由 recorder 旁路
  记录命中结果，供会话路由装配 citations / attachments。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from core.infra.async_bridge import run_sync
from core.config import settings
from core.infra.mcp_bridge import bridge
from core.rag.multimodal import process_image
from core.rag.multimodal_rag import multimodal_rag
from core.rag.parsers import downsample
from core.service.turn_context import TurnRecorder

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 工具入参模型
# ----------------------------------------------------------------------
class MultimodalSearchArgs(BaseModel):
    query: Optional[str] = Field(default=None, description="文字检索词；用户用文字描述问题时填写")
    image_path: Optional[str] = Field(default=None, description="用户上传截图的本地文件路径；做图查文时填写")


class ScreenshotAnalyzeArgs(BaseModel):
    image_path: str = Field(description="用户上传的错误截图的本地文件路径")


# ----------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------
class MultimodalSearchTool(BaseTool):
    """多模态知识库检索：文本与图片共用统一向量空间。"""

    name: str = "multimodal_search"
    description: str = (
        "在IT知识库中检索问题解决方案，支持文字与图片两种查询方式。"
        "用户用文字描述问题时填写 query（文查文/文查图）；"
        "用户提供报错截图时填写 image_path（图查文，找相关文本与相似截图）。"
        "返回带来源标注（文件+页码+模态）的知识片段。"
    )
    args_schema: Type[BaseModel] = MultimodalSearchArgs
    recorder: Any = Field(default=None, exclude=True)

    def _run(self, query: Optional[str] = None, image_path: Optional[str] = None) -> str:
        started = time.perf_counter()
        image_bytes = _load_image(image_path) if image_path else None
        if not query and not image_bytes:
            return "检索失败：请至少提供 query 或 image_path 之一。"
        try:
            results = run_sync(multimodal_rag.search(query=query, query_image=image_bytes))
        except Exception as exc:  # noqa: BLE001
            _log_tool_call(self.recorder, self.name, {"query": query, "image_path": image_path},
                           str(exc), started, status="failed")
            return f"知识库检索失败：{exc}"

        if self.recorder is not None and results:
            new_top = float(results[0].get("score", 0.0))
            # 仅在「更优」时覆盖：Agent 可能自行编词发起低分检索（如凭空搜「错误码619」），
            # 若无条件覆盖，会把前置检索/图片自匹配得到的高分命中冲掉，导致最终答案与
            # 参考来源答非所问（见 7.4 修复说明）。
            if not self.recorder.retrieved or new_top > self.recorder.top_score:
                self.recorder.retrieved = results
                self.recorder.top_score = new_top
        _log_tool_call(self.recorder, self.name, {"query": query, "image_path": image_path},
                       f"命中 {len(results)} 条", started)

        if not results:
            return "知识库中未检索到相关内容。"
        return multimodal_rag.format_results(results)


class ScreenshotAnalyzeTool(BaseTool):
    """截图分析：OCR + 视觉理解（复用 core/rag/multimodal.py，见 8.2）。"""

    name: str = "analyze_screenshot"
    description: str = (
        "分析用户上传的错误截图，提取错误码、错误信息与截图中的配置参数，"
        "并判断错误类型（网络/权限/软件/系统）。传入截图的本地文件路径。"
    )
    args_schema: Type[BaseModel] = ScreenshotAnalyzeArgs
    recorder: Any = Field(default=None, exclude=True)

    def _run(self, image_path: str) -> str:
        started = time.perf_counter()
        image_bytes = _load_image(image_path)
        if image_bytes is None:
            return f"截图分析失败：无法读取文件 {image_path}"
        try:
            result = run_sync(process_image(image_bytes))
        except Exception as exc:  # noqa: BLE001
            _log_tool_call(self.recorder, self.name, {"image_path": image_path},
                           str(exc), started, status="failed")
            return f"截图分析失败：{exc}"
        if self.recorder is not None:
            self.recorder.screenshot = result
        _log_tool_call(self.recorder, self.name, {"image_path": image_path},
                       result.get("error_code") or "已完成", started)
        return json.dumps(result, ensure_ascii=False)


class MCPTool(BaseTool):
    """把工单 MCP Server 的一个 Tool 包装成 CrewAI Tool（见 4.3）。"""

    name: str = "mcp_tool"
    description: str = ""
    args_schema: Type[BaseModel] = BaseModel
    tool_name: str = ""
    recorder: Any = Field(default=None, exclude=True)

    def _run(self, **kwargs: Any) -> str:
        started = time.perf_counter()
        try:
            text = bridge.call_tool_sync(self.tool_name, kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("MCP 工具 %s 调用失败：%s", self.tool_name, exc)
            _log_tool_call(self.recorder, self.tool_name, kwargs, str(exc), started, status="failed")
            return f"工单系统调用失败：{exc}"
        if self.recorder is not None and self.tool_name in (
            "create_ticket", "query_ticket", "list_user_tickets", "update_ticket_status"
        ):
            self.recorder.tickets.append(text)
        _log_tool_call(self.recorder, self.tool_name, kwargs, text, started)
        return text


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def _load_image(image_path: Optional[str]) -> Optional[bytes]:
    """读取图片并降采样（控制视觉 token 成本，见 7.7 ②）。"""
    if not image_path:
        return None
    path = Path(image_path)
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("读取图片失败 %s：%s", image_path, exc)
        return None
    try:
        data, _ = downsample(
            data,
            settings.EMBEDDING_MAX_IMAGE_SIDE,
            settings.EMBEDDING_MAX_IMAGE_BYTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("图片降采样失败 %s：%s", image_path, exc)
    return data


def _log_tool_call(recorder: Optional[TurnRecorder], tool_name: str, params: Any,
                   result: Any, started: float, status: str = "success") -> None:
    latency = int((time.perf_counter() - started) * 1000)
    logger.info("工具调用 %s status=%s %dms", tool_name, status, latency)
    if recorder is not None:
        recorder.record_tool_call(tool_name, params, str(result)[:2000], latency, status)


def _args_model_from_schema(tool_name: str, input_schema: dict) -> Type[BaseModel]:
    """由 MCP 的 inputSchema 动态生成 pydantic 入参模型。"""
    props: dict = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    type_map = {"string": str, "integer": int, "number": float, "boolean": bool}
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        py_type = type_map.get((spec or {}).get("type", "string"), str)
        desc = (spec or {}).get("description", "")
        if key in required:
            fields[key] = (py_type, Field(description=desc))
        else:
            fields[key] = (Optional[py_type], Field(default=None, description=desc))
    if not fields:
        fields["_noop"] = (Optional[str], Field(default=None, description="无参数"))
    return create_model(f"{tool_name}_Args", **fields)


def build_mcp_tools(recorder: TurnRecorder, names: Optional[list[str]] = None) -> list[BaseTool]:
    """按 MCP Server 暴露的 schema 动态注册工单工具。

    Args:
        recorder: 本轮记录器。
        names: 只注册这些工具；None 表示全部。
    """
    tools: list[BaseTool] = []
    for schema in bridge.tool_schemas():
        tool_name = schema.get("name", "")
        if not tool_name:
            continue
        if names is not None and tool_name not in names:
            continue
        tools.append(
            MCPTool(
                name=tool_name,
                description=schema.get("description", "") or f"调用工单系统工具 {tool_name}",
                args_schema=_args_model_from_schema(tool_name, schema.get("inputSchema", {})),
                tool_name=tool_name,
                recorder=recorder,
            )
        )
    return tools


def build_frontline_tools(recorder: TurnRecorder) -> list[BaseTool]:
    """一线客服 Agent 的工具（见 4.1）：多模态检索 + 截图分析 + 工单查询。"""
    tools: list[BaseTool] = [
        MultimodalSearchTool(recorder=recorder),
        ScreenshotAnalyzeTool(recorder=recorder),
    ]
    tools.extend(build_mcp_tools(recorder, names=["query_ticket", "list_user_tickets"]))
    return tools


def build_ticket_tools(recorder: TurnRecorder) -> list[BaseTool]:
    """工单处理 Agent 的工具（见 4.1）：工单全生命周期 + 密码重置 + 软件权限。"""
    return build_mcp_tools(
        recorder,
        names=[
            "create_ticket", "query_ticket", "list_user_tickets",
            "update_ticket_status", "reset_password", "request_software_access",
        ],
    )
