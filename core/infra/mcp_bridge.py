"""MCP 常驻连接桥。

问题：MCP 官方 SDK 的 stdio_client 基于 anyio 任务组，必须在**同一个 asyncio 任务**
内进入与退出。FastAPI 每个请求是不同任务，若在请求里 connect、在另一请求里
disconnect，会触发 cancel scope 跨任务错误。

方案：用一条常驻后台任务持有连接，所有工具调用通过队列投递给它执行。
CrewAI 的同步工具运行在线程中，可用 call_tool_sync() 通过
asyncio.run_coroutine_threadsafe 投递到主事件循环。

【这个文件在系统里的角色】
"工单 MCP Server"是一个独立进程，通过标准输入输出（stdio）与本进程通信、对外提供
工单查询/创建等工具。本桥负责"长期维持这一条连接"，并把上层（含 CrewAI 工具、路由）
的调用请求安全地转发过去、把结果带回来，是业务代码与 MCP 之间的唯一通道。

【关键概念扫盲】
1) 常驻后台任务：start() 只创建一条后台协程 _run，它独占持有 MCP 连接；
   其他协程想调用工具，不是自己连，而是把请求"投递"给它执行。
2) 队列通信：调用方把 (操作, 参数, future) 放进 asyncio.Queue，_run 逐条取出执行，
   再把结果写回 future；调用方 await 这个 future 即可拿到结果。这样所有对连接的
   读写都发生在 _run 这一个任务里，规避了 anyio 的跨任务限制。
3) 线程安全桥：CrewAI 的同步工具运行在工作线程（非事件循环），工作线程里没法直接
   await 协程，于是用 call_tool_sync 通过 run_coroutine_threadsafe 把协程"投"回
   主事件循环执行，再同步等待结果。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from core.infra.mcp_client import MCPClientError, get_mcp_client

logger = logging.getLogger(__name__)

# 哨兵对象：放进队列表示"该停机了"。用独一无二的对象而非 None，避免与正常载荷混淆。
_STOP = object()


class MCPBridge:
    """工单 MCP Server 的常驻连接桥。"""

    def __init__(self) -> None:
        """初始化桥（此时尚未建立连接，真正连接在 start() 里发起）。

        属性说明：
            _queue: 任务队列，调用方通过它把请求投递给后台任务。
            _task: 持有连接的后台任务 _run。
            _loop: 主事件循环引用，call_tool_sync 需要用它把协程投递回主循环。
            _ready: 事件标志，连接成功或失败后置位，用于 wait_ready 等待就绪。
            _schemas: 缓存的工具清单（MCP Server 提供哪些工具）。
            _error: 连接失败时的错误信息；为 None 表示无错误。
        """
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = asyncio.Event()
        self._schemas: list[dict] = []
        self._error: Optional[str] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动桥：记录事件循环、建队列、拉起后台任务。

        说明：
            get_running_loop() 拿到当前的事件循环并保存，供后续线程安全的同步调用使用。
            create_task 会把 _run() 作为独立后台协程调度起来（不阻塞当前启动流程）。
        """
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="mcp-bridge")

    async def _run(self) -> None:
        """后台常驻任务：建立连接后循环处理队列里的请求，直到收到停机信号。

        说明：
            本协程是"唯一与 MCP 连接打交道的任务"，连接、调用、断开都在这里完成，
            从而满足 MCP SDK"同一任务内进入/退出"的约束。
            连接失败时不会退出进程，而是记录错误并置位 _ready（让等待方知道"已就绪但不可用"），
            之后 available 会返回 False，工单能力优雅降级。
        """
        client = get_mcp_client()
        try:
            await client.connect()
            self._schemas = await client.list_tool_schemas()
            logger.info("工单 MCP Server 已连接，工具：%s", [s["name"] for s in self._schemas])
        except MCPClientError as exc:
            # 预期的连接错误：记录后置位就绪标志，交由上层判断能力不可用。
            self._error = str(exc)
            logger.error("工单 MCP Server 连接失败，工单能力将不可用：%s", exc)
            self._ready.set()
            return
        except Exception as exc:  # noqa: BLE001
            # 兜底捕获其它异常，保证后台任务不会静默死掉、等待方也不会一直卡住。
            self._error = str(exc)
            logger.error("工单 MCP Server 连接异常：%s", exc)
            self._ready.set()
            return

        # 连接成功，通知等待方可以开始调用。
        self._ready.set()
        assert self._queue is not None
        try:
            while True:
                # 阻塞等待下一个请求；item 是 (op, payload, fut) 三元组或停机哨兵。
                item = await self._queue.get()
                if item is _STOP:
                    break
                op, payload, fut = item
                # 调用方可能已超时放弃，此时 future 已完成，直接跳过不必执行。
                if fut.done():
                    continue
                try:
                    if op == "call":
                        name, args = payload
                        # 直接 await 底层 client，结果写回 future 交给调用方。
                        fut.set_result(await client.call_tool(name, args))
                    elif op == "schemas":
                        fut.set_result(await client.list_tool_schemas())
                    else:  # pragma: no cover - 防御
                        fut.set_exception(ValueError(f"未知操作：{op}"))
                except Exception as exc:  # noqa: BLE001
                    # 单个请求失败只反馈给它的 future，不影响后续请求处理。
                    if not fut.done():
                        fut.set_exception(exc)
        finally:
            # 无论因停机哨兵还是异常退出，都要断开连接，避免留下僵尸子进程。
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭工单 MCP 连接时出错：%s", exc)

    async def stop(self) -> None:
        """停止桥：投递停机哨兵并等待后台任务收尾。

        说明：
            先入队 _STOP 让 _run 跳出循环、正常断开连接；最多等 5 秒，超时或任务被取消时
            强制 cancel，保证应用能及时退出。
        """
        if self._queue is not None:
            await self._queue.put(_STOP)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    async def wait_ready(self, timeout: float = 20.0) -> None:
        """等待桥就绪（连接成功或失败都会置位），超时则抛 TimeoutError。

        参数：
            timeout: 最长等待秒数。
        说明：
            应用启动时调用它，确保在对外提供能力前连接已尝试完成。
        """
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    # ------------------------------------------------------------------
    # 能力查询
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """桥是否可用：无连接错误、已就绪、且后台任务存在。

        @property 让它像属性一样访问（bridge.available），不必写括号。
        """
        return self._error is None and self._ready.is_set() and self._task is not None

    @property
    def error(self) -> Optional[str]:
        """连接错误信息；None 表示无错误。"""
        return self._error

    def tool_schemas(self) -> list[dict]:
        """返回已缓存的工具清单副本。

        说明：
            返回副本（list(...)）而非内部列表本身，防止调用方误改内部状态。
        """
        return list(self._schemas)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 30.0) -> str:
        """异步调用某个 MCP 工具并返回其结果。

        参数：
            name: 工具名。
            arguments: 工具入参 dict。
            timeout: 等待结果的超时秒数。
        返回：
            工具执行结果（字符串）。
        异常：
            桥不可用/未启动/调用超时/工具报错时抛 MCPClientError 或 TimeoutError。
        说明：
            本方法只负责"投递"：创建一个 future，把 ("call", (name, arguments), future)
            放进队列后 await 该 future；真正的执行在后台任务 _run 里。
        """
        if self._error:
            raise MCPClientError(f"工单 MCP Server 不可用：{self._error}")
        if self._queue is None or self._loop is None:
            raise MCPClientError("MCP 桥尚未启动。")
        # future 是"将来会有结果的占位符"：后台任务执行完会把结果 set_result 到它上面。
        fut: asyncio.Future = self._loop.create_future()
        await self._queue.put(("call", (name, arguments), fut))
        return await asyncio.wait_for(fut, timeout=timeout)

    def call_tool_sync(self, name: str, arguments: dict[str, Any], timeout: float = 30.0) -> str:
        """同步调用（供 CrewAI 同步 Tool 在 worker 线程内使用）。

        参数：
            name: 工具名。
            arguments: 工具入参 dict。
            timeout: 等待结果的超时秒数。
        返回：
            工具执行结果（字符串）。
        异常：
            桥未启动、超时或工具报错时抛出相应异常。
        说明：
            本方法可能运行在普通线程（非事件循环）里，不能直接 await 协程，
            因此用 run_coroutine_threadsafe 把 call_tool 协程"跨线程"投递回主事件循环，
            再通过 future.result() 同步阻塞等待结果。这是把"同步世界"接入"异步世界"的标准做法。
        """
        if self._loop is None:
            raise MCPClientError("MCP 桥尚未启动。")
        future = asyncio.run_coroutine_threadsafe(self.call_tool(name, arguments, timeout), self._loop)
        # 多给 5 秒缓冲，避免因调度延迟比协程自身的 timeout 先触发。
        return future.result(timeout=timeout + 5)


# 全局单例：应用共享同一个桥。
bridge = MCPBridge()
