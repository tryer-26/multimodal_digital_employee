"""单轮对话记录器：供进程内 Tool 写入、由会话路由读取后装配回复。

对应设计文档 4.5「中台装配 citations 与 attachments」与 7.7 的生成链路：
- Tool 在 Agent 执行期间把检索结果/截图分析/工单调用写入 recorder；
- Agent 结束后，路由从 recorder 读出命中来源，装配 citations / attachments
  并发给前端，同时用于低置信度护栏判定（见 6.3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TurnRecorder:
    """一轮对话的旁路记录（不进入 Agent 上下文）。"""

    conversation_id: str
    user_id: str = ""
    # 最近一次多模态检索结果（[{id, score, metadata}]）
    retrieved: list[dict] = field(default_factory=list)
    # 最近一次检索的 top1 分数（置信度护栏用，见 6.3）
    top_score: float = 0.0
    # 截图分析结果（process_image 的返回值）
    screenshot: Optional[dict] = None
    # 工单相关
    tickets: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)

    def record_tool_call(self, tool_name: str, params: Any, result: Any, latency_ms: int, status: str = "success") -> None:
        self.tool_calls.append(
            {
                "tool_name": tool_name,
                "input_params": params,
                "output_result": result,
                "latency_ms": latency_ms,
                "status": status,
            }
        )
