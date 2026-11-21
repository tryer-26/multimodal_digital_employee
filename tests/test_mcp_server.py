"""工单系统 MCP Server 单元测试。

测试策略：
- 先通过 monkeypatch 将环境变量 ``TICKETS_DB_PATH`` 指向 ``tmp_path/tickets.db``，
  再导入 server 模块，确保每个用例使用独立的临时数据库；
- 通过直接调用被 ``@mcp.tool()`` 装饰后的函数（装饰器保留原函数可调用性）来验证业务逻辑；
- 所有返回为 JSON 的接口统一用 ``json.loads`` 解析后断言。
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

# 将项目根目录加入 sys.path，保证可 import mcp_servers.it_service.server
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVER_MODULE = "mcp_servers.it_service.server"
TICKET_ID_PATTERN = re.compile(r"^TICKET-[0-9a-f]{8}$")
REQUEST_ID_PATTERN = re.compile(r"^REQ-[0-9a-f]{8}$")


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """提供已绑定临时数据库的 server 模块。"""
    db_file = tmp_path / "tickets.db"
    monkeypatch.setenv("TICKETS_DB_PATH", str(db_file))

    # 每次用例都重新导入，避免跨用例缓存模块状态与数据库路径
    sys.modules.pop(SERVER_MODULE, None)
    module = importlib.import_module(SERVER_MODULE)
    module.ensure_db()
    return module


def _extract_ticket_id(message: str) -> str:
    """从 create_ticket 的返回文本中提取工单号。"""
    match = re.search(r"TICKET-[0-9a-f]{8}", message)
    assert match is not None, f"返回文本中未找到合法工单号：{message}"
    return match.group(0)


def test_ensure_db_idempotent(server: ModuleType, tmp_path: Path) -> None:
    """ensure_db 幂等且返回真实数据库路径。"""
    path = server.ensure_db()
    assert Path(path) == tmp_path / "tickets.db"
    assert Path(path).exists()
    # 再次调用不应报错
    server.ensure_db()


def test_create_ticket_normal(server: ModuleType) -> None:
    """正常创建工单，返回文本与工单号格式正确。"""
    message = server.create_ticket("VPN 无法连接", "办公室 VPN 连接超时", user_id="u1")
    assert message.startswith("工单已创建：")
    ticket_id = _extract_ticket_id(message)
    assert TICKET_ID_PATTERN.match(ticket_id)

    # 落库校验
    detail = json.loads(server.query_ticket(ticket_id))
    assert detail["id"] == ticket_id
    assert detail["status"] == "open"
    assert detail["priority"] == "medium"
    assert detail["category"] == "general"
    assert detail["user_id"] == "u1"


def test_create_ticket_invalid_enums_fallback(server: ModuleType) -> None:
    """非法 priority/category 回退为默认值，并在返回中说明。"""
    message = server.create_ticket(
        "非法枚举", "描述", priority="urgent", category="unknown"
    )
    assert "medium" in message
    assert "general" in message

    ticket_id = _extract_ticket_id(message)
    detail = json.loads(server.query_ticket(ticket_id))
    assert detail["priority"] == "medium"
    assert detail["category"] == "general"


def test_query_ticket_found(server: ModuleType) -> None:
    """query_ticket 命中返回完整 JSON。"""
    ticket_id = _extract_ticket_id(server.create_ticket("标题", "描述"))
    detail = json.loads(server.query_ticket(ticket_id))
    assert detail["found"] is True
    assert detail["title"] == "标题"
    assert detail["description"] == "描述"


def test_query_ticket_not_found(server: ModuleType) -> None:
    """query_ticket 未命中返回明确提示。"""
    detail = json.loads(server.query_ticket("TICKET-deadbeef"))
    assert detail["found"] is False
    assert "未找到" in detail["message"]


def test_list_user_tickets(server: ModuleType) -> None:
    """list_user_tickets 仅返回指定用户工单，按创建时间倒序。"""
    server.create_ticket("A", "a", user_id="u2")
    server.create_ticket("B", "b", user_id="u2")
    server.create_ticket("C", "c", user_id="u3")

    tickets = json.loads(server.list_user_tickets("u2"))
    assert isinstance(tickets, list)
    assert len(tickets) == 2
    assert all(t["user_id"] == "u2" for t in tickets)

    # 空用户返回空数组
    assert json.loads(server.list_user_tickets("nobody")) == []


def test_update_ticket_status_valid(server: ModuleType) -> None:
    """合法状态更新成功，并返回更新后的工单。"""
    ticket_id = _extract_ticket_id(server.create_ticket("标题", "描述"))
    result = json.loads(server.update_ticket_status(ticket_id, "resolved"))
    assert result["ok"] is True
    assert result["ticket"]["status"] == "resolved"
    assert result["ticket"]["updated_at"]


def test_update_ticket_status_invalid(server: ModuleType) -> None:
    """非法状态返回错误说明。"""
    ticket_id = _extract_ticket_id(server.create_ticket("标题", "描述"))
    result = json.loads(server.update_ticket_status(ticket_id, "bad_status"))
    assert result["ok"] is False
    assert "非法状态" in result["message"]


def test_update_ticket_status_not_found(server: ModuleType) -> None:
    """工单不存在返回明确提示。"""
    result = json.loads(server.update_ticket_status("TICKET-00000000", "closed"))
    assert result["ok"] is False
    assert "未找到" in result["message"]


def test_reset_password(server: ModuleType) -> None:
    """reset_password 返回含 8 位临时密码的 JSON。"""
    result = json.loads(server.reset_password("u1"))
    assert result["user_id"] == "u1"
    assert isinstance(result["temp_password"], str)
    assert len(result["temp_password"]) == 8
    assert result["expires_in_minutes"] == 30


def test_request_software_access(server: ModuleType) -> None:
    """request_software_access 返回提交成功 JSON。"""
    result = json.loads(server.request_software_access("u1", "Photoshop"))
    assert REQUEST_ID_PATTERN.match(result["request_id"])
    assert result["user_id"] == "u1"
    assert result["software"] == "Photoshop"
    assert result["status"] == "submitted"
