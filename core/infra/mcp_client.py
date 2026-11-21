"""中台侧 MCP Client：以 stdio 方式连接工单系统 MCP Server。

中台（MCP Host）通过本客户端与**唯一的外部系统**（工单 MCP Server）通信：
- 拉起 / 关闭 Server 子进程（stdio 传输）；
- 发现工具（list_tools）并标准化调用（call_tool）；
- 将连接失败、调用失败、Server 未启动等异常统一包装为 ``MCPClientError``，
  并打印清晰日志，保证中台不会因外部系统故障而崩溃。

设计依据：设计文档 3.2、5.1 ~ 5.6。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool

from core.config import settings

logger = logging.getLogger(__name__)

# 项目根目录：core/infra/ 的上两级（即仓库根）。
# 注意：本文件原位于 core/ 下（当时 parent.parent 即仓库根），
# 重构到 core/infra/ 后目录层级多了一层，必须用 parents[2]，否则 cwd 会错成 core/。
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# Server 脚本默认相对路径（相对项目根）
DEFAULT_SERVER_SCRIPT: str = "mcp_servers/it_service/server.py"


class MCPClientError(RuntimeError):
    """MCP 客户端异常。

    在连接失败、工具发现失败、工具调用失败或 Server 未启动时抛出，
    消息中带有中文说明，便于中台捕获后降级处理而非崩溃。
    """


class MCPClient:
    """工单系统 MCP Server 的 stdio 客户端。

    Args:
        server_command: 启动 Server 的命令，缺省从 ``settings.MCP_SERVER_COMMAND``
            读取，未配置时回退为当前解释器 ``sys.executable``。
        server_args: 传给命令的参数列表，缺省为 ``[server_script]``。
        cwd: 子进程工作目录，缺省为项目根目录。
    """

    def __init__(
        self,
        server_command: Optional[str] = None,
        server_args: Optional[list[str]] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        # 命令与脚本均可通过 core.config.settings 覆盖，缺省给出兜底值
        command: str = (
            server_command
            or getattr(settings, "MCP_SERVER_COMMAND", "")
            or sys.executable
        )
        script: str = getattr(settings, "MCP_SERVER_SCRIPT", "") or DEFAULT_SERVER_SCRIPT
        args: list[str] = server_args if server_args is not None else [script]
        self.cwd: Path = Path(cwd) if cwd is not None else PROJECT_ROOT

        # 用属性保存 StdioServerParameters，供连接与重连复用。
        # 必须显式传 env：mcp SDK 默认只继承一份环境变量白名单
        # （mcp.client.stdio.get_default_environment），不含 PYTHONIOENCODING/PYTHONUTF8。
        # 子进程的 stdout 是被中台用管道接管的，此时 Python 不再走控制台 API，
        # 会退回系统默认编码（Windows 中文即 GBK），导致 FastMCP banner 的框线/emoji
        # 无法编码而被转义成 \u2580 / \U0001f5a5。这里显式声明 UTF-8 即可根治。
        child_env: dict[str, str] = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        self.server_params: StdioServerParameters = StdioServerParameters(
            command=command,
            args=args,
            cwd=str(self.cwd),
            env=child_env,
        )

        self.session: Optional[ClientSession] = None
        # 保存底层上下文管理器，便于显式释放（子进程生命周期）
        self._stdio_ctx: Any = None
        self._session_ctx: Any = None

    # ------------------------------------------------------------------
    # 连接 / 断开
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """启动 Server 子进程并完成 MCP 握手初始化。

        Raises:
            MCPClientError: 无法启动 Server 或初始化失败时抛出。
        """
        if self.session is not None:
            # 已连接，幂等返回
            return

        try:
            self._stdio_ctx = stdio_client(self.server_params)
            read_stream, write_stream = await self._stdio_ctx.__aenter__()

            self._session_ctx = ClientSession(read_stream, write_stream)
            self.session = await self._session_ctx.__aenter__()
            await self.session.initialize()

            logger.info(
                "已连接工单 MCP Server：command=%s args=%s cwd=%s",
                self.server_params.command,
                self.server_params.args,
                self.cwd,
            )
        except Exception as exc:  # noqa: BLE001 - 统一兜底并回滚资源
            await self._cleanup()
            logger.error("连接工单 MCP Server 失败：%s", exc)
            raise MCPClientError(
                f"连接工单 MCP Server 失败：{exc}；"
                f"请确认命令 {self.server_params.command} 与脚本 "
                f"{self.server_params.args} 可用。"
            ) from exc

    async def disconnect(self) -> None:
        """断开会话并回收 Server 子进程（幂等）。"""
        await self._cleanup()
        logger.info("已断开工单 MCP Server 连接。")

    async def _cleanup(self) -> None:
        """按相反顺序释放会话与 stdio 上下文，避免子进程泄漏。"""
        if self._session_ctx is not None:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 MCP 会话时出错：%s", exc)
            finally:
                self._session_ctx = None
                self.session = None

        if self._stdio_ctx is not None:
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 MCP 子进程时出错：%s", exc)
            finally:
                self._stdio_ctx = None

    # ------------------------------------------------------------------
    # 上下文管理：支持 async with
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # 工具发现 / 调用
    # ------------------------------------------------------------------
    async def get_tools(self) -> list[Tool]:
        """获取 Server 暴露的全部工具。

        Returns:
            工具列表（``mcp.types.Tool``）。

        Raises:
            MCPClientError: 未连接或获取失败时抛出。
        """
        if self.session is None:
            raise MCPClientError("MCP Server 尚未连接，请先调用 connect()。")
        try:
            result = await self.session.list_tools()
        except Exception as exc:  # noqa: BLE001
            logger.error("获取工单工具列表失败：%s", exc)
            raise MCPClientError(f"获取工单工具列表失败：{exc}") from exc
        return list(result.tools)

    async def list_tool_schemas(self) -> list[dict[str, Any]]:
        """获取工具的结构化描述（name/description/inputSchema）。

        Returns:
            形如 [{"name": ..., "description": ..., "inputSchema": {...}}, ...]。

        Raises:
            MCPClientError: 未连接或获取失败时抛出。
        """
        tools = await self.get_tools()
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            schemas.append(
                {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", ""),
                    "inputSchema": getattr(tool, "inputSchema", {}),
                }
            )
        return schemas

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用指定工具并返回其文本结果。

        Args:
            name: 工具名称。
            arguments: 工具入参字典。

        Returns:
            工具返回的文本内容（``result.content[0].text``）；
            当 content 为空时回退到结构化内容或空字符串。

        Raises:
            MCPClientError: 未连接或调用失败时抛出。
        """
        if self.session is None:
            raise MCPClientError("MCP Server 尚未连接，请先调用 connect()。")
        try:
            result = await self.session.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            logger.error("调用工单工具 '%s' 失败：%s", name, exc)
            raise MCPClientError(
                f"调用工单工具 '{name}' 失败：{exc}"
            ) from exc

        # 容错：content 可能为空或缺失
        content = getattr(result, "content", None)
        if not content:
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                return json.dumps(structured, ensure_ascii=False)
            logger.warning("工具 '%s' 返回内容为空。", name)
            return ""

        first = content[0]
        text = getattr(first, "text", None)
        if text is None:
            return str(first)
        return text


# ----------------------------------------------------------------------
# 模块级单例工厂：进程内复用同一个 MCPClient（注意 stdio 子进程生命周期）
# ----------------------------------------------------------------------
_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    """获取进程内复用的 MCPClient 单例（懒加载）。

    Returns:
        全局唯一的 ``MCPClient`` 实例。
    """
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClient()
    return _mcp_client
