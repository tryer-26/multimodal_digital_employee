"""SQLite 业务库（中台主库），见设计文档第九章。

表：users / agent_profiles / sessions / conversations / messages /
    tool_calls / knowledge_documents
工单库 tickets.db 属于工单 MCP Server，不在本库（见 5.3）。

【这个文件在系统里的角色】
这是整个平台唯一的"业务数据仓库"封装层。上层（路由、服务、Agent）不直接写 SQL，
一律通过这里暴露的 `Database` 方法读写，好处是：SQL 集中、字段变更方便、并发安全统一处理。

【关键概念扫盲】
1) sqlite3 是"同步阻塞"库：它的 execute/commit 会卡住当前线程。而 FastAPI 的请求跑在
   asyncio 事件循环里，一旦在协程里直接调用同步阻塞代码，整个服务器都会被卡住（其他请求
   全部等待）。所以这里用 `asyncio.to_thread(...)` 把同步的 sqlite3 调用丢到线程池执行，
   让事件循环能继续处理别的请求——这正是本文件大量出现 `await asyncio.to_thread(...)` 的原因。
2) 写操作需要串行：SQLite 对同一文件的并发写会互相干扰（报 "database is locked"）。
   这里用一把进程级线程锁 `_write_lock`，保证同一时刻只有一个线程在写，读则不加锁。
3) WAL 模式：让"读"和"写"可以并行，是 SQLite 推荐的日志模式。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.config import settings

# 建表脚本（幂等）：所有表都用 IF NOT EXISTS，保证启动时重复执行也不会报错。
# 下面按表说明关键字段与状态枚举含义，方便对照业务：
#   users.role    : user=普通员工 / agent=人工坐席 / admin=管理员
#   users.status  : active=正常 / disabled=被禁用（登录时会被拦截）
#   agent_profiles.status : offline=离线 / online=在线（决定能否被派单）
#   conversations.mode    : bot=机器人接待 / waiting_human=已转人工等待接单 /
#                           human=坐席服务中（见 6.8 会话状态机）
#   conversations.status  : active=进行中 / closed=已结束
#   conversations.end_reason : resolved=已解决 / unresolved=超时未解决等结束原因
#   messages.role         : user/assistant/agent/system，标识这条消息由谁发出
#   messages.modality     : text/audio/image，多模态消息的载体类型
#   messages.intent       : 意图识别结果；confidence 为该意图的置信度（0~1）
#   tool_calls.status     : success/error，记录一次工具调用的成败
#   knowledge_documents.status : indexed=已入向量库等索引状态

# 索引意图：给高频查询的字段建索引，避免全表扫描。
#   idx_messages_conv    : 按会话查消息（(conversation_id, id) 组合，兼顾过滤与排序）
#   idx_conversations_user : 按用户列会话并按最近活跃时间排序
#   idx_tool_calls_conv  : 按会话查工具调用记录
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    department TEXT,
    role TEXT DEFAULT 'user',
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    user_id TEXT PRIMARY KEY,
    status TEXT DEFAULT 'offline',
    max_concurrent INTEGER DEFAULT 3,
    current_count INTEGER DEFAULT 0,
    skills TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT,
    role TEXT,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    channel TEXT DEFAULT 'web',
    title TEXT,
    mode TEXT DEFAULT 'bot',
    assigned_agent_id TEXT,
    handoff_reason TEXT,
    status TEXT DEFAULT 'active',
    end_reason TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP,
    ended_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    role TEXT,
    content TEXT,
    modality TEXT DEFAULT 'text',
    audio_url TEXT,
    image_url TEXT,
    emotion TEXT,
    intent TEXT,
    confidence REAL,
    attachments TEXT,
    citations TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    tool_name TEXT,
    input_params TEXT,
    output_result TEXT,
    status TEXT DEFAULT 'success',
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    modalities TEXT,
    chunk_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'indexed',
    size_bytes INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, last_message_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_conv ON tool_calls(conversation_id);
"""


def now_iso() -> str:
    """返回当前本地时间字符串（精确到秒，ISO 8601 格式）。

    返回：
        形如 "2026-10-04T15:30:00" 的字符串。
    说明：
        全库的 created_at / last_message_at 等时间字段统一用本函数写入，
        保证格式一致、可直接按字符串比较大小排序（ISO 格式天然支持字典序比较）。
    """
    return datetime.now().isoformat(timespec="seconds")


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """把单行查询结果转成普通 dict；无结果时返回 None。

    参数：
        row: sqlite3.Row 对象（可像 dict 一样取值），或 None。
    返回：
        dict，或 None。
    说明：
        因为开启了 row_factory=Row，查询结果不是原生 dict，这里统一转换，
        让上层拿到的就是普通字典。
    """
    return dict(row) if row is not None else None


def _rows(rows) -> list[dict]:
    """把多行查询结果批量转成 dict 列表。

    参数：
        rows: 可迭代的 sqlite3.Row 集合。
    返回：
        dict 列表（即使无数据也返回空列表）。
    """
    return [dict(r) for r in rows]


class Database:
    """业务库访问器：把同步 sqlite3 调用包装成异步、并保证写入串行。

    上层拿到的是本类的全局单例 `db`（文件末尾创建），直接用 `await db.xxx()` 即可，
    不需要关心 sqlite3 的阻塞与并发问题。
    """

    def __init__(self, path: Optional[str] = None):
        """初始化数据库访问器。

        参数：
            path: 数据库文件路径；不传则用配置里的 settings.DB_PATH。
        说明：
            构造时会顺手把数据库文件所在目录建出来（mkdir(parents=True) 相当于 mkdir -p），
            避免首次运行时因目录不存在而连不上库。
            同时创建一把写锁 `_write_lock`：进程内所有写操作都要先抢到它才能执行。
        """
        self.path = path or settings.DB_PATH
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # 线程锁：保证同一进程里同一时刻只有一个线程在写 SQLite，避免并发写冲突。
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接与通用执行
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """新建并返回一个 sqlite3 连接。

        返回：
            已配置好的 sqlite3.Connection。
        说明：
            每次操作都新建连接、用完即关（见 _write/_read），这样天然规避了连接被
            跨线程复用的风险；代价是略微多了些建连开销，对教学/中小规模场景可接受。
            - check_same_thread=False：允许这个连接被 to_thread 的线程池线程使用；
            - row_factory=Row：让查询结果支持按列名取值（如 row["id"]）；
            - WAL：读写可并行的日志模式；
            - foreign_keys=ON：SQLite 默认不强制外键，这里显式打开。
        """
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _write(self, fn):
        """在写锁保护下执行一次写事务，成功后提交。

        参数：
            fn: 接收一个连接、执行若干 SQL 的函数（回调）。
        返回：
            fn 的返回值。
        说明：
            这是所有"写"的唯一入口：先抢写锁串行化，再执行、提交，最后无论成败都关连接
            （finally 保证不泄漏连接）。若 fn 抛异常则不会 commit，事务自动回滚。
        """
        with self._write_lock:
            conn = self._connect()
            try:
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()

    def _read(self, fn):
        """执行一次只读查询（不加写锁，用完关连接）。

        参数：
            fn: 接收一个连接、执行查询的函数（回调）。
        返回：
            fn 的返回值。
        说明：
            读操作不需要串行（WAL 下读可与写并行），因此不加锁以提升并发。
        """
        conn = self._connect()
        try:
            return fn(conn)
        finally:
            conn.close()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """执行一条写 SQL（INSERT/UPDATE/DELETE）。

        参数：
            sql: 带 `?` 占位符的 SQL 语句。
            params: 与占位符一一对应的参数元组（用参数化可防 SQL 注入）。
        返回：
            None。
        说明：
            `asyncio.to_thread` 把同步阻塞的 `_write` 丢到线程池执行，避免卡死事件循环；
            lambda 只是把"用哪个连接、执行哪条 SQL"打包成回调传给 _write。
        """
        await asyncio.to_thread(self._write, lambda c: c.execute(sql, params))

    async def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """查询单行，返回 dict；查不到返回 None。

        参数：
            sql: SELECT 语句（带 `?` 占位符）。
            params: 参数元组。
        返回：
            dict 或 None。
        """
        return await asyncio.to_thread(
            self._read, lambda c: _row(c.execute(sql, params).fetchone())
        )

    async def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """查询多行，返回 dict 列表（无结果为空列表）。

        参数：
            sql: SELECT 语句（带 `?` 占位符）。
            params: 参数元组。
        返回：
            dict 列表。
        """
        return await asyncio.to_thread(
            self._read, lambda c: _rows(c.execute(sql, params).fetchall())
        )

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    async def init(self) -> None:
        """初始化数据库：建表（幂等）并补齐老库缺失的列。

        返回：
            None。
        说明：
            应用启动时调用一次。executescript 会一次性执行整个 SCHEMA。
        """
        await asyncio.to_thread(self._write, lambda c: c.executescript(SCHEMA))
        await asyncio.to_thread(self._write, self._ensure_columns)

    # 差量补列：CREATE TABLE IF NOT EXISTS 不会改动已存在的表，
    # 因此新增列必须在这里对老库补齐（见 17.3 要点 4）
    # 结构：{表名: {新增列名: 列定义}}，需求新增列时往这里加即可。
    _NEW_COLUMNS: dict[str, dict[str, str]] = {"messages": {"intent": "TEXT"}}

    @classmethod
    def _ensure_columns(cls, conn: sqlite3.Connection) -> None:
        """为已存在的老表补上 SCHEMA 里新增、但老库没有的列。

        参数：
            conn: 数据库连接（由 _write 传入）。
        返回：
            None。
        说明：
            PRAGMA table_info(表) 会列出该表当前的列，用列名集合判断缺哪列，
            缺了才 ALTER TABLE ADD COLUMN（该操作在 SQLite 里安全、可重复执行）。
        """
        for table, columns in cls._NEW_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    async def create_user(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        display_name: str = "",
        email: str = "",
        department: str = "",
    ) -> str:
        """新建用户，返回新用户 id。

        参数：
            username: 登录名（唯一）。
            password_hash: 已哈希的密码（调用方先经 auth.hash_password 处理，严禁存明文）。
            role: 角色，user/agent/admin，默认 user。
            display_name/email/department: 展示名/邮箱/部门，均可不填。
        返回：
            新生成的用户 id，形如 "u-xxxxxxxx"。
        说明：
            若角色是坐席（agent），顺手建一条坐席档案，让坐席能立即被派单。
        """
        # 用 uuid 截取前 8 位拼 id，短且基本不会碰撞，便于阅读。
        user_id = f"u-{uuid.uuid4().hex[:8]}"
        await self.execute(
            """INSERT INTO users (id, username, password_hash, display_name, email,
               department, role, status, created_at)
               VALUES (?,?,?,?,?,?,?, 'active', ?)""",
            (user_id, username, password_hash, display_name, email, department, role, now_iso()),
        )
        if role == "agent":
            await self.upsert_agent_profile(user_id)
        return user_id

    async def get_user(self, user_id: str) -> Optional[dict]:
        """按 id 查用户，查不到返回 None。"""
        return await self.query_one("SELECT * FROM users WHERE id=?", (user_id,))

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        """按登录名查用户（登录流程用），查不到返回 None。"""
        return await self.query_one("SELECT * FROM users WHERE username=?", (username,))

    async def list_users(self) -> list[dict]:
        """列出全部用户，并附带坐席状态（非坐席则为空串）。

        返回：
            用户 dict 列表，坐席额外带 agent_status 字段。
        说明：
            用 LEFT JOIN 关联 agent_profiles：普通用户没有坐席档案也要能查出来，
            所以用左连接而非内连接。
        """
        return await self.query_all(
            """SELECT u.*, COALESCE(a.status, '') AS agent_status
               FROM users u LEFT JOIN agent_profiles a ON a.user_id = u.id
               ORDER BY u.created_at ASC"""
        )

    async def update_user(self, user_id: str, **fields) -> None:
        """按需更新用户字段（只更新传进来的、且在白名单内的字段）。

        参数：
            user_id: 目标用户 id。
            **fields: 要更新的字段，如 display_name="张三"。
        返回：
            None。
        说明：
            allowed 白名单防止误改 id/username 等关键字段；值为 None 的字段会被跳过，
            若过滤后没有任何可更新字段则直接返回（避免拼出非法 SQL）。
        """
        allowed = {"display_name", "email", "department", "role", "status", "password_hash"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        cols = ", ".join(f"{k}=?" for k in updates)
        await self.execute(
            f"UPDATE users SET {cols} WHERE id=?", (*updates.values(), user_id)
        )

    async def delete_user(self, user_id: str) -> None:
        """删除用户，并级联清理其会话、消息、工具调用、登录会话与坐席档案。

        参数：
            user_id: 要删除的用户 id。
        返回：
            None。

        工单存于 tickets.db（经 MCP 管理），与会话解耦，不做级联（见 6.x）；
        该用户作为坐席被指派的会话仅解除指派（置空），避免留下悬空 ID。
        说明：
            这些删除必须在同一个事务（同一个 _do）里完成，保证要么全成功要么全回滚，
            不会出现"消息删了、会话还在"的半删状态。
        """

        def _do(conn: sqlite3.Connection) -> None:
            # 先找出该用户的所有会话 id，再据此删除其名下的消息与工具调用，
            # 最后才删会话本身（子表先删，避免残留孤儿数据）。
            conv_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM conversations WHERE user_id=?", (user_id,)
                ).fetchall()
            ]
            for conv_id in conv_ids:
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
                conn.execute("DELETE FROM tool_calls WHERE conversation_id=?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
            # 若该用户是坐席、正被指派服务某些会话，则把这些会话的指派置空（不删会话）。
            conn.execute(
                "UPDATE conversations SET assigned_agent_id=NULL WHERE assigned_agent_id=?",
                (user_id,),
            )
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM agent_profiles WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))

        await asyncio.to_thread(self._write, _do)

    async def set_last_login(self, user_id: str) -> None:
        """记录用户本次登录时间（登录成功时调用）。"""
        await self.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user_id))

    # ------------------------------------------------------------------
    # 坐席档案
    # ------------------------------------------------------------------
    async def upsert_agent_profile(self, user_id: str) -> None:
        """确保该用户有坐席档案（有则不动，无则新建）。

        说明：
            ON CONFLICT(user_id) DO NOTHING 是"插入或忽略"：已存在就什么都不做，
            因此本方法可安全重复调用。新建时默认离线、最多 3 路并发、技能为空。
        """
        await self.execute(
            """INSERT INTO agent_profiles (user_id, status, max_concurrent, current_count, skills)
               VALUES (?, 'offline', 3, 0, '[]')
               ON CONFLICT(user_id) DO NOTHING""",
            (user_id,),
        )

    async def get_agent_profile(self, user_id: str) -> Optional[dict]:
        """按用户 id 查坐席档案，查不到返回 None。"""
        return await self.query_one("SELECT * FROM agent_profiles WHERE user_id=?", (user_id,))

    async def set_agent_status(self, user_id: str, status: str) -> None:
        """设置坐席在线状态（online/offline），首次调用会自动补建档案。"""
        await self.upsert_agent_profile(user_id)
        await self.execute(
            "UPDATE agent_profiles SET status=? WHERE user_id=?", (status, user_id)
        )

    async def list_agents(self) -> list[dict]:
        """列出全部坐席及其容量（含离线坐席），供管理端展示。"""
        return await self.query_all(
            """SELECT u.id, u.username, u.display_name, a.status, a.max_concurrent, a.current_count
               FROM users u JOIN agent_profiles a ON a.user_id=u.id
               WHERE u.role='agent' ORDER BY u.username"""
        )

    async def list_available_agents(self) -> list[dict]:
        """列出"可接单"的坐席：在线且当前接待数未达上限。

        说明：
            派单逻辑据此挑选目标坐席；current_count < max_concurrent 表示还有余量。
        """
        return await self.query_all(
            """SELECT u.id, u.display_name, a.current_count, a.max_concurrent
               FROM users u JOIN agent_profiles a ON a.user_id=u.id
               WHERE u.role='agent' AND a.status='online' AND a.current_count < a.max_concurrent"""
        )

    async def sync_agent_current_count(self, agent_id: str) -> int:
        """把 current_count 重算为「该坐席名下服务中的活跃会话数」。

        参数：
            agent_id: 坐席用户 id。
        返回：
            重算后的接待数。

        计数不再用 +1/-1 增量维护：坐席掉线、服务重启、会话被用户关闭或超时关闭
        等路径都不会再造成「只增不减」的漂移（见 6.8）。
        说明：
            这里用一条"子查询"直接把计数改成真实值，属于"以事实为准"的自愈式重算。
        """
        await self.execute(
            """UPDATE agent_profiles SET current_count = (
                   SELECT COUNT(*) FROM conversations
                   WHERE assigned_agent_id = ? AND mode='human' AND status='active'
               ) WHERE user_id=?""",
            (agent_id, agent_id),
        )
        profile = await self.get_agent_profile(agent_id) or {}
        return int(profile.get("current_count") or 0)

    # ------------------------------------------------------------------
    # 登录会话
    # ------------------------------------------------------------------
    async def create_session(self, token: str, user_id: str, role: str, expires_at: datetime) -> None:
        """写入一条登录会话（token 与用户、角色、过期时间的映射）。

        参数：
            token: 登录令牌（由 auth.login 生成，主键）。
            user_id: 归属用户 id。
            role: 冗余存储角色，鉴权时可直接读，少一次查用户表。
            expires_at: 过期时间（datetime，写入时转成 ISO 字符串）。
        """
        await self.execute(
            "INSERT INTO sessions (token, user_id, role, expires_at, created_at) VALUES (?,?,?,?,?)",
            (token, user_id, role, expires_at.isoformat(timespec="seconds"), now_iso()),
        )

    async def get_session(self, token: str) -> Optional[dict]:
        """按 token 查会话，查不到返回 None。"""
        return await self.query_one("SELECT * FROM sessions WHERE token=?", (token,))

    async def delete_session(self, token: str) -> None:
        """删除会话（登出，或 token 过期时清理）。"""
        await self.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def create_conversation(self, user_id: str, title: str = "新会话") -> str:
        """新建会话，返回会话 id。

        参数：
            user_id: 会话所属用户。
            title: 会话标题，默认"新会话"。
        返回：
            新生成的会话 id，形如 "conv-xxxxxxxx"。
        说明：
            新会话一律初始化为 channel=web、mode=bot（机器人接待）、status=active。
        """
        conv_id = f"conv-{uuid.uuid4().hex[:8]}"
        ts = now_iso()
        await self.execute(
            """INSERT INTO conversations (id, user_id, channel, title, mode, status, started_at, last_message_at)
               VALUES (?,?, 'web', ?, 'bot', 'active', ?, ?)""",
            (conv_id, user_id, title, ts, ts),
        )
        return conv_id

    async def get_conversation(self, conv_id: str) -> Optional[dict]:
        """按 id 查会话，查不到返回 None。"""
        return await self.query_one("SELECT * FROM conversations WHERE id=?", (conv_id,))

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """列出某用户的会话，按最近活跃时间倒序。

        说明：
            COALESCE(last_message_at, started_at) 保证从未发过消息的新会话也能用开始时间排序。
        """
        return await self.query_all(
            """SELECT * FROM conversations WHERE user_id=?
               ORDER BY COALESCE(last_message_at, started_at) DESC LIMIT ?""",
            (user_id, limit),
        )

    @staticmethod
    def _conversation_filters(mode: str = "", keyword: str = "") -> tuple[str, list]:
        """构造会话查询的 WHERE 片段与参数（供列表页/计数复用）。

        参数：
            mode: 按会话模式过滤（bot/waiting_human/human），空则不筛。
            keyword: 关键词，模糊匹配会话 id/标题/用户名/展示名，空则不筛。
        返回：
            (where_sql, params)：where_sql 形如 "WHERE c.mode = ?"；无过滤条件时返回 ("", [])。
        说明：
            抽成静态方法是为了让"列表查询"与"总数统计"共用同一套过滤条件，避免两处逻辑不一致。
        """
        clauses: list[str] = []
        params: list = []
        if mode:
            clauses.append("c.mode = ?")
            params.append(mode)
        if keyword:
            like = f"%{keyword}%"
            clauses.append(
                "(c.id LIKE ? OR c.title LIKE ? OR u.display_name LIKE ? OR u.username LIKE ?)"
            )
            params.extend([like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    async def list_all_conversations(
        self, limit: int = 100, offset: int = 0, mode: str = "", keyword: str = ""
    ) -> list[dict]:
        """分页列出全部会话（管理端用），可按模式与关键词过滤。

        参数：
            limit/offset: 分页参数（每页条数、偏移量）。
            mode/keyword: 过滤条件，含义见 _conversation_filters。
        返回：
            会话 dict 列表，附带 user_name 展示名。
        """
        where, params = self._conversation_filters(mode, keyword)
        return await self.query_all(
            f"""SELECT c.*, u.display_name AS user_name FROM conversations c
               LEFT JOIN users u ON u.id = c.user_id
               {where}
               ORDER BY COALESCE(c.last_message_at, c.started_at) DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )

    async def count_conversations(self, mode: str = "", keyword: str = "") -> int:
        """统计满足过滤条件的会话总数（与列表页配套，用于分页）。"""
        where, params = self._conversation_filters(mode, keyword)
        row = await self.query_one(
            f"""SELECT COUNT(*) AS n FROM conversations c
               LEFT JOIN users u ON u.id = c.user_id {where}""",
            tuple(params),
        )
        return int(row["n"]) if row else 0

    async def list_waiting_human(self) -> list[dict]:
        """列出"等待人工接单"的会话（mode=waiting_human），按等待时长正序。

        说明：
            按 last_message_at 升序 = 等得越久排越前，坐席/管理端可优先处理最早转来的会话。
        """
        return await self.query_all(
            """SELECT c.*, u.display_name AS user_name FROM conversations c
               LEFT JOIN users u ON u.id = c.user_id
               WHERE c.status='active' AND c.mode='waiting_human'
               ORDER BY c.last_message_at ASC"""
        )

    async def list_agent_active_sessions(self, agent_id: str) -> list[dict]:
        """坐席「服务中」的会话（mode=human 且已分配给自己）。

        参数：
            agent_id: 坐席用户 id。
        返回：
            该坐席正在服务的会话列表。

        用于应用重启 / 页面刷新后恢复工作台（见 6.8「服务中会话保留」）。
        """
        return await self.query_all(
            """SELECT c.*, u.display_name AS user_name FROM conversations c
               LEFT JOIN users u ON u.id = c.user_id
               WHERE c.status='active' AND c.mode='human' AND c.assigned_agent_id=?
               ORDER BY COALESCE(c.last_message_at, c.started_at) DESC""",
            (agent_id,),
        )

    async def update_conversation(self, conv_id: str, **fields) -> None:
        """按需更新会话字段（白名单内、非空才更新），用法同 update_user。"""
        allowed = {
            "title", "mode", "assigned_agent_id", "handoff_reason",
            "status", "end_reason", "last_message_at", "ended_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        cols = ", ".join(f"{k}=?" for k in updates)
        await self.execute(f"UPDATE conversations SET {cols} WHERE id=?", (*updates.values(), conv_id))

    async def touch_conversation(self, conv_id: str) -> None:
        """把会话的 last_message_at 刷新为当前时间（有新消息时调用）。

        说明：
            该字段是"会话是否超时"和"会话排序"的依据，因此在每次收发消息后更新。
        """
        await self.execute("UPDATE conversations SET last_message_at=? WHERE id=?", (now_iso(), conv_id))

    async def delete_conversation(self, conv_id: str) -> None:
        """删除会话及其消息、工具调用记录。

        参数：
            conv_id: 要删除的会话 id。
        返回：
            None。

        工单（tickets.db，经 MCP 管理）与会话解耦，不做级联删除，仅通过
        conversation_id / user_id 关联（见 6.x）。
        """

        def _do(conn: sqlite3.Connection) -> None:
            # 先读出会话当前状态，用于删除后判断是否需要重算坐席接待数。
            row = conn.execute(
                "SELECT assigned_agent_id, mode, status FROM conversations WHERE id=?",
                (conv_id,),
            ).fetchone()
            # 子表先删，再删主表，避免残留孤儿记录。
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
            conn.execute("DELETE FROM tool_calls WHERE conversation_id=?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            # 若删掉的是坐席「服务中」的会话，同步重算接待数（见 6.8）
            if row and row["mode"] == "human" and row["status"] == "active" and row["assigned_agent_id"]:
                conn.execute(
                    """UPDATE agent_profiles SET current_count = (
                           SELECT COUNT(*) FROM conversations c
                           WHERE c.assigned_agent_id = agent_profiles.user_id
                             AND c.mode='human' AND c.status='active'
                       )"""
                )

        await asyncio.to_thread(self._write, _do)

    async def close_expired_conversations(self, minutes: int = 30) -> int:
        """超时自动关闭（见 6.9）：last_message_at 为空以 started_at 兜底。

        参数：
            minutes: 空闲超过该分钟数即视为超时。
        返回：
            被关闭的会话条数。
        说明：
            COALESCE(last_message_at, started_at) 表示"最后活跃时间"，从未发过消息的新会话
            用开始时间兜底，避免因 last_message_at 为 NULL 而永远不超时。
        """
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")

        def _do(conn: sqlite3.Connection) -> int:
            # 一条 UPDATE 批量关闭所有超时的 active 会话，并标记结束原因为 unresolved。
            cur = conn.execute(
                """UPDATE conversations
                   SET status='closed', end_reason='unresolved', ended_at=?
                   WHERE status='active'
                     AND COALESCE(last_message_at, started_at) < ?""",
                (now_iso(), cutoff),
            )
            if cur.rowcount:
                # 被关闭的可能是坐席「服务中」的会话，同步重算各坐席接待数（见 6.8）
                conn.execute(
                    """UPDATE agent_profiles SET current_count = (
                           SELECT COUNT(*) FROM conversations c
                           WHERE c.assigned_agent_id = agent_profiles.user_id
                             AND c.mode='human' AND c.status='active'
                       )"""
                )
            # rowcount 即本次关闭的条数（无命中时为 0）。
            return cur.rowcount

        return await asyncio.to_thread(self._write, _do)

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    async def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        modality: str = "text",
        audio_url: Optional[str] = None,
        image_url: Optional[str] = None,
        emotion: Optional[str] = None,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
        attachments: Optional[list] = None,
        citations: Optional[list] = None,
    ) -> int:
        """新增一条消息，并同步刷新会话的最后活跃时间。

        参数：
            conv_id: 所属会话 id。
            role: 消息发送方，user/assistant/agent/system。
            content: 文本内容。
            modality: 模态，text/audio/image，默认 text。
            audio_url/image_url: 语音/图片附件的访问地址（多模态消息才传）。
            emotion: 情绪识别结果（可选）。
            intent: 意图识别结果（可选）。
            confidence: 意图置信度 0~1（可选）。
            attachments: 附件列表，落库前会序列化为 JSON 字符串。
            citations: 引用的知识来源列表，落库前会序列化为 JSON 字符串。
        返回：
            新消息的自增主键 id（lastrowid）。
        说明：
            插入消息与更新会话 last_message_at 放在同一个事务里，保证"有新消息但时间没更新"
            这类不一致不会出现。
        """
        ts = now_iso()

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """INSERT INTO messages (conversation_id, role, content, modality, audio_url,
                   image_url, emotion, intent, confidence, attachments, citations, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    conv_id, role, content, modality, audio_url, image_url, emotion, intent,
                    confidence,
                    # 列表类型字段转成 JSON 文本存（SQLite 无数组类型），空值则存 NULL。
                    json.dumps(attachments, ensure_ascii=False) if attachments else None,
                    json.dumps(citations, ensure_ascii=False) if citations else None,
                    ts,
                ),
            )
            conn.execute("UPDATE conversations SET last_message_at=? WHERE id=?", (ts, conv_id))
            return cur.lastrowid

        return await asyncio.to_thread(self._write, _do)

    async def get_messages(self, conv_id: str, limit: int = 200) -> list[dict]:
        """取「最近 limit 条」，按时间正序返回。

        参数：
            conv_id: 会话 id。
            limit: 最多返回的条数（取最近的）。
        返回：
            消息 dict 列表，已按 id 正序（时间从旧到新），且 attachments/citations 已解析为列表。

        原先直接 ORDER BY id ASC LIMIT 会在超过 limit 条后永远只返回最旧的记录，
        新消息查不到；且 _history_text 取 messages[-N:] 实际会拿到最旧的历史。
        说明：
            现在的写法是"先按 id 倒序取最近 limit 条，再在外层翻回正序"，
            既保证拿到的是最新消息，又保证给上层的历史记录是从旧到新。
        """
        rows = await self.query_all(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (conv_id, limit),
        )
        return [_decode_message(r) for r in rows]

    async def count_messages(self, conv_id: str) -> int:
        """统计某会话的消息条数。"""
        row = await self.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conv_id,)
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------
    async def add_tool_call(
        self,
        conv_id: Optional[str],
        tool_name: str,
        input_params: Any,
        output_result: Any,
        status: str = "success",
        latency_ms: int = 0,
    ) -> None:
        """记录一次工具调用（用于审计与可观测）。

        参数：
            conv_id: 关联会话 id，可为 None（脱离会话的调用）。
            tool_name: 工具名，如工单查询/创建。
            input_params: 入参，非字符串时会被序列化为 JSON 文本。
            output_result: 出参，非字符串时会被序列化为 JSON 文本。
            status: 调用结果，success/error。
            latency_ms: 调用耗时（毫秒）。
        """
        await self.execute(
            """INSERT INTO tool_calls (conversation_id, tool_name, input_params, output_result,
               status, latency_ms, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                conv_id, tool_name,
                # 已是字符串就直接存（避免二次 JSON 编码变成带引号的串），否则序列化。
                json.dumps(input_params, ensure_ascii=False) if not isinstance(input_params, str) else input_params,
                json.dumps(output_result, ensure_ascii=False) if not isinstance(output_result, str) else output_result,
                status, latency_ms, now_iso(),
            ),
        )

    async def list_tool_calls(
        self, limit: int = 100, conv_id: Optional[str] = None, offset: int = 0
    ) -> list[dict]:
        """分页列出工具调用记录，按时间倒序（最新在前）。

        参数：
            limit/offset: 分页参数。
            conv_id: 指定则只返回该会话的调用记录，否则返回全部。
        返回：
            工具调用 dict 列表。
        """
        if conv_id:
            return await self.query_all(
                "SELECT * FROM tool_calls WHERE conversation_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (conv_id, limit, offset),
            )
        return await self.query_all(
            "SELECT * FROM tool_calls ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    async def count_tool_calls(self) -> int:
        """统计工具调用总条数。"""
        row = await self.query_one("SELECT COUNT(*) AS n FROM tool_calls")
        return int(row["n"]) if row else 0

    async def delete_tool_call(self, tool_call_id: int) -> bool:
        """删除单条工具调用记录，返回是否命中。

        参数：
            tool_call_id: 工具调用记录的自增 id。
        返回：
            True=删掉了 1 条；False=没有该记录。
        说明：
            execute 返回的是 cursor，用 cursor.rowcount 判断是否真的删到了数据。
        """
        cursor = await asyncio.to_thread(
            self._write,
            lambda c: c.execute("DELETE FROM tool_calls WHERE id=?", (tool_call_id,)),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 知识库文档元数据
    # ------------------------------------------------------------------
    async def upsert_document(
        self,
        doc_id: str,
        filename: str,
        category: str,
        modalities: dict,
        chunk_count: int,
        size_bytes: int,
        status: str = "indexed",
    ) -> None:
        """写入或更新一条知识库文档的元数据。

        参数：
            doc_id: 文档 id（主键）。
            filename: 文件名。
            category: 分类，默认 general。
            modalities: 该文档包含的模态信息（dict，落库前序列化为 JSON）。
            chunk_count: 切分后的分块数。
            size_bytes: 文件字节数。
            status: 索引状态，默认 indexed。
        说明：
            "upsert"：id 已存在则更新（DO UPDATE），否则插入。excluded.xxx 指"本次本要插入的值"，
            用它来覆盖旧值，实现"有则更新、无则新建"。
        """
        await self.execute(
            """INSERT INTO knowledge_documents
               (id, filename, category, modalities, chunk_count, status, size_bytes, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 filename=excluded.filename, category=excluded.category,
                 modalities=excluded.modalities, chunk_count=excluded.chunk_count,
                 status=excluded.status, size_bytes=excluded.size_bytes""",
            (
                doc_id, filename, category,
                json.dumps(modalities, ensure_ascii=False),
                chunk_count, status, size_bytes, now_iso(),
            ),
        )

    async def list_documents(self) -> list[dict]:
        """列出全部知识库文档，按入库时间倒序。"""
        return await self.query_all(
            "SELECT * FROM knowledge_documents ORDER BY created_at DESC"
        )

    async def get_document(self, doc_id: str) -> Optional[dict]:
        """按 id 查文档元数据，查不到返回 None。"""
        return await self.query_one("SELECT * FROM knowledge_documents WHERE id=?", (doc_id,))

    async def delete_document(self, doc_id: str) -> None:
        """按 id 删除文档元数据（注意：仅删本库记录，不动向量库内容）。"""
        await self.execute("DELETE FROM knowledge_documents WHERE id=?", (doc_id,))

    async def count_documents(self) -> int:
        """统计知识库文档总数。"""
        row = await self.query_one("SELECT COUNT(*) AS n FROM knowledge_documents")
        return int(row["n"]) if row else 0


def _decode_message(row: dict) -> dict:
    """把消息行里存成 JSON 文本的 attachments/citations 还原成列表。

    参数：
        row: 单条消息 dict。
    返回：
        处理后的同一 dict（字段已被就地替换为 list）。
    说明：
        落库时列表被 json.dumps 成字符串，读取时要 json.loads 还原；解析失败（脏数据）时
        退化为空列表，保证上层拿到的永远是可用列表，不会因单条脏数据而报错。
    """
    for field in ("attachments", "citations"):
        raw = row.get(field)
        if raw:
            try:
                row[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                row[field] = []
        else:
            row[field] = []
    return row


# 全局单例：整个应用共享同一个 Database 实例（内含同一把写锁）。
db = Database()
