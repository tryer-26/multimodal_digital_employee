"""部署一致性测试：deploy/ 下的镜像与编排必须与运行时导入保持一致。

回归背景：
1. ``core.main`` / ``core.api.admin_api`` 在模块顶层导入 ``eval``，但
   Dockerfile 曾漏拷 ``eval``，导致容器内 ``uvicorn core.main:app`` 启动即
   ``ModuleNotFoundError: No module named 'eval'``。
2. 容器部署文件统一收敛到 ``deploy/``，编排里引用的 Dockerfile 必须真实存在。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_eval_package():
    text = (ROOT / "deploy" / "Dockerfile.core").read_text(encoding="utf-8")
    assert "COPY eval ./eval" in text, "Dockerfile.core 必须拷贝 eval 包（core.main 顶层导入）"


def test_core_main_imports_eval():
    """固化耦合关系：若将来移除该导入，可同步去掉 Dockerfile.core 的 eval 拷贝。"""
    text = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    assert "from eval.store import recover_stale_runs" in text


def test_compose_references_existing_dockerfiles():
    """compose 中引用的 Dockerfile 路径必须存在（相对仓库根目录）。"""
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("Dockerfile.core", "Dockerfile.frontend"):
        assert f"dockerfile: deploy/{name}" in compose, f"编排未引用 deploy/{name}"
        assert (ROOT / "deploy" / name).is_file(), f"deploy/{name} 不存在"


def test_frontend_dockerfile_uses_root_context_paths():
    """前端镜像与 core 共用仓库根上下文，源码路径需带 frontend/ 前缀。"""
    text = (ROOT / "deploy" / "Dockerfile.frontend").read_text(encoding="utf-8")
    assert "COPY frontend/package.json frontend/package-lock.json ./" in text
    assert "RUN npm ci" in text, "带 lockfile 时应用 npm ci 保证构建可复现"
    assert "COPY frontend/ ./" in text


def test_core_dockerfile_omits_tests():
    """运行镜像不拷贝 tests/pytest.ini：容器内既不需要、又因 docs/素材被忽略而跑不通。"""
    text = (ROOT / "deploy" / "Dockerfile.core").read_text(encoding="utf-8")
    assert "COPY tests" not in text
    assert "pytest.ini" not in text


def test_dockerignore_excludes_nested_db():
    """`*.db` 不跨目录，须用 `**/*.db` 才能排除 mcp_servers/.../tickets.db 等嵌套库文件。"""
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
    assert "**/*.db" in patterns
    assert "*.db" not in patterns
