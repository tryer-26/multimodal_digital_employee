"""评测用合成图片（确定性生成）。

与演示知识库同一思路：用 PIL 画出「报错截图 / 拓扑图」这类**合成演示数据**，
评测因此不依赖任何外部素材，也不含真实个人信息（见设计文档第十六章）。
生成结果按文件名缓存到 ``data/eval/images/``，金标写在 ``datasets/*.jsonl``。
"""
from __future__ import annotations

from pathlib import Path

IMAGES_DIR = Path("./data/eval/images")


def _load_font(size: int):
    """尽量加载中文字体，失败回退默认字体。

    参数：
        size：字号（像素）。
    返回：
        PIL 的 ImageFont 对象。默认字体不含中文字形，若不加载系统字体，
        画出来的汉字会变成方块，故按候选清单择优加载。
    """
    from PIL import ImageFont

    # 依次尝试常见中文字体（Windows / Linux），命中即用
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
                continue  # 字体文件损坏则继续试下一个
    return ImageFont.load_default()


def _png(image) -> bytes:
    """把 PIL 图像编码为 PNG 字节（内存中完成，不落盘）。

    参数：
        image：PIL.Image 对象。
    返回：
        PNG 格式字节串，供后续直接喂给 VLM，省去中间文件读写。
    """
    from io import BytesIO

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def vpn_619() -> bytes:
    """VPN 错误 619 报错截图。金标：error_code=619 / service=VPN / error_type=网络。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 360), "#f3f4f6")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 740, 340], fill="#ffffff", outline="#d1d5db", width=2)
    draw.rectangle([20, 20, 740, 72], fill="#dc2626")
    draw.text((40, 34), "连接失败 - VPN 客户端", fill="#ffffff", font=_load_font(22))
    draw.text((40, 112), "错误 619：无法建立到远程计算机的连接，", fill="#111827", font=_load_font(20))
    draw.text((40, 148), "因为用于此连接的网络端口已被关闭。", fill="#111827", font=_load_font(20))
    draw.text((40, 205), "协议：PPTP    端口：1723", fill="#374151", font=_load_font(18))
    draw.text((40, 240), "客户端版本：v2.8（低于建议的 v3.2）", fill="#b45309", font=_load_font(18))
    draw.rectangle([560, 285, 700, 325], fill="#2563eb")
    draw.text((600, 295), "确定", fill="#ffffff", font=_load_font(20))
    return _png(img)


def nginx_502() -> bytes:
    """Nginx 502 Bad Gateway 报错页。金标：error_code=502 / service=Nginx / error_type=网络。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 300), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 740, 280], fill="#ffffff", outline="#cbd5e1", width=2)
    draw.text((40, 44), "502 Bad Gateway", fill="#b91c1c", font=_load_font(30))
    draw.text((40, 110), "nginx/1.24.0", fill="#475569", font=_load_font(18))
    draw.text((40, 150), "上游服务不可达，请检查后端服务状态与 proxy_read_timeout。", fill="#111827", font=_load_font(18))
    draw.text((40, 200), "网关：Nginx    上游：app-server:8080", fill="#374151", font=_load_font(18))
    return _png(img)


def vpn_691() -> bytes:
    """VPN 错误 691（安全验证失败）报错截图。金标：error_code=691 / service=VPN / error_type=权限。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (760, 320), "#f3f4f6")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 740, 300], fill="#ffffff", outline="#d1d5db", width=2)
    draw.rectangle([20, 20, 740, 72], fill="#d97706")
    draw.text((40, 34), "VPN 连接被拒绝", fill="#ffffff", font=_load_font(22))
    draw.text((40, 112), "错误 691：由于安全验证失败，拒绝访问。", fill="#111827", font=_load_font(20))
    draw.text((40, 150), "用户名或密码错误，或该账号已被锁定。", fill="#111827", font=_load_font(20))
    draw.text((40, 205), "账号：lisi@corp.example.com", fill="#374151", font=_load_font(18))
    draw.text((40, 240), "提示：连续输错 5 次将锁定 30 分钟", fill="#b45309", font=_load_font(18))
    return _png(img)


def network_topology() -> bytes:
    """企业网络与 VPN 接入路径拓扑图（用于配图描述评测）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (880, 430), "#f8fafc")
    draw = ImageDraw.Draw(img)
    font = _load_font(18)
    small = _load_font(15)
    draw.text((30, 20), "企业网络架构与 VPN 接入路径", fill="#0f172a", font=_load_font(22))

    boxes = [
        (40, 100, 230, 160, "员工终端", "#dbeafe"),
        (350, 100, 540, 160, "公网 / 运营商", "#e0e7ff"),
        (640, 100, 830, 160, "VPN 网关", "#fee2e2"),
        (640, 280, 830, 340, "内网应用服务器", "#dcfce7"),
    ]
    for x1, y1, x2, y2, label, color in boxes:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=10, fill=color, outline="#94a3b8", width=2)
        draw.text((x1 + 24, y1 + 22), label, fill="#0f172a", font=font)

    draw.line([230, 130, 350, 130], fill="#64748b", width=3)
    draw.line([540, 130, 640, 130], fill="#64748b", width=3)
    draw.line([735, 160, 735, 280], fill="#64748b", width=3)
    draw.text((255, 104), "1723/443", fill="#475569", font=small)
    draw.text((552, 104), "IKEv2 / SSTP", fill="#475569", font=small)
    draw.text((470, 250), "网关策略与 NAT 影响连接成功率", fill="#b91c1c", font=small)
    return _png(img)


# 文件名 → 生成函数（数据集 jsonl 通过文件名引用）
BUILDERS = {
    "vpn_619.png": vpn_619,
    "nginx_502.png": nginx_502,
    "vpn_691.png": vpn_691,
    "network_topology.png": network_topology,
}


def materialize(filename: str) -> bytes:
    """确保图片存在并返回其字节（首次生成后写入 ``data/eval/images/``）。

    参数：
        filename：图片文件名，须在 BUILDERS 中登记。
    返回：
        图片 PNG 字节。已存在则直接读缓存，否则调用对应生成函数并落盘。
    异常：
        KeyError：文件名未登记时抛出，避免静默返回空图。
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / filename
    if path.exists():  # 命中缓存，避免重复绘制
        return path.read_bytes()
    builder = BUILDERS.get(filename)
    if builder is None:
        raise KeyError(f"未知评测图片：{filename}")
    data = builder()
    path.write_bytes(data)
    return data
