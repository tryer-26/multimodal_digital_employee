"""演示知识库种子数据（见设计文档 7.5、第十六章隐私合规）。

本模块职责：支撑管理端「一键加载演示知识库」。它把内置的示例文档写入本地
data/knowledge 目录，再交给多模态 RAG 解析、分块、嵌入并落入 ChromaDB 向量库，
从而让系统一启动就有一批可检索的知识（否则问答检索为空）。

全部为**合成演示数据**，禁止录入真实个人信息。
包含文本手册（含表格）与图像块（报错截图、网络拓扑图），
用于演示「纯文本 RAG vs 多模态 RAG」的召回差异。
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.infra.db import db
from core.rag.multimodal_rag import multimodal_rag

logger = logging.getLogger(__name__)

# 下列 *_MD 常量是文本型演示文档的原文（Markdown 格式）。
# 注意其中的 Markdown 表格：入库时会被解析成独立的「表格块」按文本嵌入，
# 这样表格的行列关系在检索里不会丢失（见 multimodal_rag 的分块逻辑）。
VPN_MD = """# VPN 连接失败排查手册

## 适用场景
员工在公司外网或家庭网络环境下，无法通过 VPN 客户端连接企业内网。

## 一、常见错误码与含义

| 错误码 | 含义 | 首选处理 |
|---|---|---|
| 619 | 无法建立到远程计算机的连接 | 检查账号是否过期 / 端口 1723 是否被占用 |
| 691 | 由于安全验证失败，拒绝访问 | 密码错误或账号被锁定，重置密码 |
| 720 | 无法建立连接（PPP 协商失败） | 删除并重建 VPN 拨号连接 |
| 809 | 无法建立到远程服务器的连接 | 防火墙拦截 IKEv2/443，切换协议 |

## 二、标准排查步骤（按顺序执行）
1. 确认账号在有效期内：登录 IT 自助门户查看 VPN 账号到期时间。
2. 检查本地网络：打开浏览器访问任意外网页面，确认基础网络畅通。
3. 检查客户端版本：VPN 客户端需为 v3.2 及以上，旧版本会因协议不兼容连接失败。
4. 检查拨号协议：优先使用 IKEv2，若被运营商拦截则切换为 SSTP。
5. 检查端口占用与防火墙：临时关闭第三方安全软件，确认 UDP 500/4500 未被拦截。
6. 重启客户端与网络适配器：`ipconfig /flushdns` 后重新拨号。

## 三、仍无法连接时
若完成上述步骤仍失败，请提交工单，附上错误截图与客户端日志，由网络工程师排查网关侧策略。
"""

PASSWORD_MD = """# 密码重置与账号解锁流程

## 适用场景
企业办公账号忘记密码、密码过期、账号被锁定导致无法登录。

## 一、自助重置步骤
1. 打开 IT 自助门户，点击「忘记密码」。
2. 输入工号与预留手机号，获取短信验证码。
3. 设置新密码：长度不少于 12 位，需包含大小写字母、数字与特殊符号。
4. 密码历史限制：最近 5 次使用过的密码不可重复使用。

## 二、账号被锁定
连续输错密码 5 次会锁定账号 30 分钟。可等待 30 分钟自动解锁，或提交工单由服务台手工解锁。

## 三、密码周期策略

| 账号类型 | 有效期 | 到期提前提醒 |
|---|---|---|
| 普通员工 | 180 天 | 提前 7 天 |
| 管理员 | 90 天 | 提前 14 天 |
| 服务账号 | 365 天 | 提前 30 天 |
"""

SOFTWARE_MD = """# 软件安装与权限申请

## 适用场景
员工需要安装办公软件、开发工具，或申请业务系统访问权限。

## 一、申请流程
1. 登录 IT 自助门户 →「软件与权限申请」。
2. 选择软件名称与版本，填写申请理由与使用期限。
3. 直属主管审批通过后，由 IT 服务台统一分发安装包或开通权限。

## 二、常见问题
- **安装时提示无管理员权限**：普通员工账号不具备本机管理员权限，需走申请流程。
- **软件激活失败**：确认已连接内网并已获取授权码；授权码有效期 7 天。
- **申请被驳回**：常见原因是缺少主管审批，或该软件不在企业许可清单内。

## 三、企业许可清单（节选）

| 软件 | 是否可直接安装 | 备注 |
|---|---|---|
| 办公套件 | 是 | 自助门户直接下载 |
| 代码编辑器 | 是 | 免申请 |
| 数据库客户端 | 否 | 需主管审批 |
| 设计类软件 | 否 | 需主管审批且限量授权 |
"""

EMAIL_MD = """# 企业邮箱配置指南

## 适用场景
员工在 Outlook / 手机客户端配置企业邮箱失败，或出现收发异常。

## 一、标准配置参数（IMAP/SMTP）

| 项目 | 服务器地址 | 端口 | 加密方式 |
|---|---|---|---|
| IMAP（收） | imap.corp.example.com | 993 | SSL/TLS |
| SMTP（发） | smtp.corp.example.com | 465 | SSL/TLS |
| 发件服务器认证 | 需勾选「要求身份验证」 | - | - |

## 二、常见故障
1. **能收不能发**：多为发件服务器未勾选身份验证，或 465 端口被拦截。
2. **提示密码错误**：企业邮箱启用了「应用专用密码」，需在门户生成专用密码再配置。
3. **邮件延迟**：检查是否触发了反垃圾策略，大附件（>25MB）会被拒收。

## 三、手机客户端
推荐使用 IMAP 协议；若配置 Exchange 模式失败，改用 IMAP 手动配置即可。
"""

ERROR_CODE_TABLE_MD = """# IT 常见错误码对照表

| 错误码 | 所属系统 | 含义 | 处理建议 |
|---|---|---|---|
| 502 | Nginx 网关 | Bad Gateway，上游服务不可达 | 检查上游服务状态与 proxy_read_timeout |
| 504 | Nginx 网关 | Gateway Timeout，上游响应超时 | 调大超时时间并排查上游慢查询 |
| 619 | VPN | 无法建立到远程计算机的连接 | 检查账号有效期与端口占用 |
| 691 | VPN | 安全验证失败，拒绝访问 | 重置密码或解除账号锁定 |
| 0x80070005 | Windows | 访问被拒绝 | 以管理员身份运行或申请权限 |
| 0xC000006A | Windows 登录 | 用户名正确但密码错误 | 重置密码 |
"""


def _load_font(size: int):
    """尽量加载中文字体，失败则回退默认字体。

    参数：
        size: 字号（像素）。

    返回：
        PIL 的 ImageFont 对象，可直接传给 ImageDraw.text。

    作用：
        合成演示图片时需要渲染中文，而 PIL 的默认字体不含中文字形，会显示成方框；
        因此在多个常见系统字体路径里逐个探测。

    边界说明：
        - 候选路径同时覆盖 Windows（msyh/simhei）与 Linux（文泉驿/Noto），
          找不到任何可用字体时退回 ImageFont.load_default()；
        - 即使文件存在也可能因损坏抛 OSError，此时 continue 试下一个候选。
    """
    from PIL import ImageFont

    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _make_error_screenshot() -> bytes:
    """合成「VPN 错误 619」报错截图（合成演示数据）。

    返回：
        PNG 图片的字节内容（bytes），可直接写盘或入库。

    作用：
        为「多模态 RAG」演示准备一张图片型知识。图片入库时会走「配图描述 +
        文本嵌入」，检索命中后原图作为附件回传，用来对比纯文本 RAG 的差异。

    实现说明：
        - 用 PIL 现画一张对话框样式图片，而不是依赖外部素材文件；
        - import 放在函数内部，是为了不把 Pillow 变成模块导入的强依赖（只有
          真正生成图片时才需要它）；
        - 图上出现的 zhangsan@corp.example.com 等均为编造的演示信息，不含真实
          个人数据（见第十六章隐私合规）。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw

    # 新建画布，再铺一层白底对话框（带浅灰描边）
    img = Image.new("RGB", (720, 360), "#f3f4f6")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 700, 340], fill="#ffffff", outline="#d1d5db", width=2)
    # 对话框顶部的红色标题栏 + 标题文字
    draw.rectangle([20, 20, 700, 70], fill="#dc2626")
    draw.text((40, 34), "连接失败 - VPN 客户端", fill="#ffffff", font=_load_font(22))
    # 主体错误信息（刻意做成「版本过旧 + 端口被关」的典型 619 场景，供检索区分）
    draw.text((40, 110), "错误 619：无法建立到远程计算机的连接，", fill="#111827", font=_load_font(20))
    draw.text((40, 145), "因为用于此连接的网络端口已被关闭。", fill="#111827", font=_load_font(20))
    # 账号、协议端口、客户端版本等上下文信息
    draw.text((40, 200), "账号：zhangsan@corp.example.com", fill="#374151", font=_load_font(18))
    draw.text((40, 232), "协议：PPTP    端口：1723", fill="#374151", font=_load_font(18))
    draw.text((40, 264), "客户端版本：v2.8（低于建议的 v3.2）", fill="#b45309", font=_load_font(18))
    # 右下角蓝色「确定」按钮
    draw.rectangle([540, 285, 680, 325], fill="#2563eb")
    draw.text((575, 295), "确定", fill="#ffffff", font=_load_font(20))
    # 保存到内存缓冲区并取字节，避免产生临时文件
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_network_diagram() -> bytes:
    """合成企业网络拓扑图（合成演示数据）。

    返回：
        PNG 图片的字节内容（bytes）。

    作用：
        再准备一张「图优于文」的知识（接入路径、端口、协议），用于演示多模态
        检索：用文字描述的网络问题也能召回这张拓扑图作为参考附件。

    实现说明：
        先画四个圆角矩形表示节点，再用直线把它们连成数据流方向，最后加简短
        标注（端口 / 协议 / 风险提示）；所有坐标都是写死的画布像素位置。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw

    # 画布 + 标题
    img = Image.new("RGB", (860, 420), "#f8fafc")
    draw = ImageDraw.Draw(img)
    font = _load_font(18)
    small = _load_font(15)
    draw.text((30, 20), "企业网络架构与 VPN 接入路径", fill="#0f172a", font=_load_font(22))

    # 各节点：坐标 (x1,y1,x2,y2) + 文字 + 底色
    boxes = [
        (40, 90, 220, 150, "员工终端", "#dbeafe"),
        (330, 90, 510, 150, "公网 / 运营商", "#e0e7ff"),
        (620, 90, 800, 150, "VPN 网关", "#fee2e2"),
        (620, 260, 800, 320, "内网应用服务器", "#dcfce7"),
    ]
    for x1, y1, x2, y2, label, color in boxes:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=10, fill=color, outline="#94a3b8", width=2)
        draw.text((x1 + 22, y1 + 20), label, fill="#0f172a", font=font)

    # 连接线：终端→公网→网关，再向下连到内网服务器，表示接入路径
    draw.line([220, 120, 330, 120], fill="#64748b", width=3)
    draw.line([510, 120, 620, 120], fill="#64748b", width=3)
    draw.line([710, 150, 710, 260], fill="#64748b", width=3)
    # 线上标注：使用的端口与协议，以及网关策略这一常见失败原因
    draw.text((240, 96), "1723/443", fill="#475569", font=small)
    draw.text((530, 96), "IKEv2 / SSTP", fill="#475569", font=small)
    draw.text((470, 235), "网关策略与 NAT 影响连接成功率", fill="#b91c1c", font=small)
    # 同截图：保存到内存缓冲区，返回字节
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# 文本文档清单：每项是 (文件名, 知识分类 category, 文档正文)。
# category 用英文枚举（network/password/software/email/general），与检索时的
# 分类过滤、以及知识库表 knowledge_documents.category 保持同一套口径。
TEXT_DOCS: list[tuple[str, str, str]] = [
    ("vpn.md", "network", VPN_MD),
    ("password.md", "password", PASSWORD_MD),
    ("software.md", "software", SOFTWARE_MD),
    ("email.md", "email", EMAIL_MD),
    ("error_code_table.md", "general", ERROR_CODE_TABLE_MD),
]

# 图片文档清单：每项是 (文件名, 知识分类, 图片字节)。
# 注意：这里的 _make_error_screenshot() / _make_network_diagram() 是在「模块被
# 导入时」就执行并生成 PNG 字节，之后复用这份字节，避免每次播种都重画图片。
IMAGE_DOCS: list[tuple[str, str, bytes]] = [
    ("vpn_error_619.png", "network", _make_error_screenshot()),
    ("network_diagram.png", "network", _make_network_diagram()),
]


def _knowledge_dir() -> Path:
    """返回存放演示文档的本地目录，不存在则先创建。

    返回：
        Path 对象，指向相对项目根目录的 ./data/knowledge。

    说明：
        parents=True 会连同缺失的上级目录一起创建；exist_ok=True 表示目录已存在
        时不报错，因此本函数可以安全地重复调用。
    """
    directory = Path("./data/knowledge")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def seed_knowledge_base(force: bool = False) -> dict:
    """写入并索引演示知识库。已存在且非 force 时跳过（差量补种）。

    参数：
        force: 是否强制重种。False（默认）时，对已入库的文件跳过；True 时对
            所有文件都重新写盘并调用入库。

    返回：
        dict，形如 {"created": [...], "skipped": [...], "chunks_added": int}，
        created/skipped 是文件名列表，chunks_added 是本次新增的分块（chunk）总数。

    边界说明：
        - 去重依据是「文件名」：先读 knowledge_documents 表里已有的文件名做集合，
          逐个比对；因此重复点击「一键加载」不会把同一份文档反复入库；
        - 判定用的是文件名而非内容哈希，若两个不同路径同名会被视为同一份文档；
        - force=True 会重新入库，注意它不会先删除旧记录/旧向量。

    实现说明（文件从哪来）：
        文本内容来自本模块内置的 *_MD 常量，图片来自 _make_* 函数生成的字节；
        两者都先写到本地 ./data/knowledge 目录，再把「磁盘路径」交给
        multimodal_rag.add_document 去解析、分块、嵌入并落 ChromaDB。
        之所以要真正落盘，是因为解析器按文件路径读取（见 parse_document）。
    """
    directory = _knowledge_dir()
    created: list[str] = []  # 本次实际入库的文件名
    skipped: list[str] = []  # 因已存在而跳过的文件名
    chunks = 0               # 本次入库产生的分块总数

    # 取「已入库文件名」集合：比对时用 set，查找复杂度 O(1)，比逐个查库快
    existing_docs = {doc["filename"] for doc in await db.list_documents()}

    # 第一轮：处理文本型文档（文件名, 分类, 正文）
    for filename, category, content in TEXT_DOCS:
        if filename in existing_docs and not force:
            # 已存在且未强制：跳过，保证幂等（重复调用不会产生重复知识）
            skipped.append(filename)
            continue
        path = directory / filename
        # 写盘：指定 utf-8 编码，避免中文内容在默认编码下乱码
        path.write_text(content, encoding="utf-8")
        # add_document 完成「解析→分块→嵌入→落向量库」，返回 chunk_count 供统计
        result = await multimodal_rag.add_document(str(path), category=category)
        chunks += int(result.get("chunk_count", 0))
        created.append(filename)

    # 第二轮：处理图片型文档（文件名, 分类, 图片字节）；逻辑同上，只是用二进制写盘
    for filename, category, data in IMAGE_DOCS:
        if filename in existing_docs and not force:
            skipped.append(filename)
            continue
        path = directory / filename
        path.write_bytes(data)
        result = await multimodal_rag.add_document(str(path), category=category)
        chunks += int(result.get("chunk_count", 0))
        created.append(filename)

    logger.info("知识库种子完成：新建 %s，跳过 %s，新增分块 %d", created, skipped, chunks)
    return {"created": created, "skipped": skipped, "chunks_added": chunks}
