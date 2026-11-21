"""初始演示账号（见设计文档 11.6）。

演示账号统一弱口令 123456，禁止部署到公网（见 11.4 安全警示）。

【这个文件在系统里的角色】
应用启动时"种"一批开箱即用的演示账号，让学员不用先注册就能登录体验不同角色
（员工/坐席/管理员）。它是"差量补种"：只创建还不存在的账号，已存在的一律跳过，
因此反复重启也不会覆盖或报错。
"""
from __future__ import annotations

from core.infra.auth import hash_password
from core.infra.db import db

# 演示账号清单：覆盖三种角色，便于演示"员工提问 → 坐席接管 → 管理员看数据"的完整链路。
# 注意：这里存的是明文口令，仅用于起始种子；真正落库时会在下方用 hash_password 转换。
INIT_USERS = [
    {"username": "user01",  "password": "123456", "role": "user",  "display_name": "张三（员工）", "department": "技术部"},
    {"username": "user02",  "password": "123456", "role": "user",  "display_name": "王五（员工）", "department": "财务部"},
    {"username": "agent01", "password": "123456", "role": "agent", "display_name": "李四（坐席）", "department": "IT服务台"},
    {"username": "admin",   "password": "123456", "role": "admin", "display_name": "管理员",       "department": "IT服务台"},
]


async def init_users() -> dict:
    """差量补种：仅创建不存在的账号。

    返回：
        {"created": [...新建的用户名...], "skipped": [...已存在而跳过的用户名...]}。

    说明：
        逐个先查是否存在：存在则记入 skipped 并跳过（保证不覆盖已有账号）；
        不存在则创建。坐席账号额外置为 online，方便单坐席场景下直接演示转人工派单。
    """
    created, skipped = [], []
    for item in INIT_USERS:
        existing = await db.get_user_by_username(item["username"])
        if existing:
            skipped.append(item["username"])
            continue
        await db.create_user(
            username=item["username"],
            # 落库前先哈希口令，数据库里绝不存明文。
            password_hash=hash_password(item["password"]),
            role=item["role"],
            display_name=item["display_name"],
            department=item.get("department", ""),
        )
        # 坐席默认置为在线，便于单坐席演示（见 6.8）
        if item["role"] == "agent":
            user = await db.get_user_by_username(item["username"])
            await db.set_agent_status(user["id"], "online")
        created.append(item["username"])
    return {"created": created, "skipped": skipped}
