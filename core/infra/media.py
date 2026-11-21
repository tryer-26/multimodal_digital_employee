"""媒体文件存储：图片/音频附件，经 /api/media/{id} 鉴权访问（见 8.4）。

存本地 data/media/，不裸露静态路径。

【这个文件在系统里的角色】
统一管理"用户上传的图片/语音"这类二进制文件：负责把字节写进本地目录、生成一个安全的
asset_id，并在需要时按 id 找回文件。文件不直接对外做静态路由，而是必须经过 API 鉴权后
再流式返回，避免任何人猜到路径就能下载别人上传的内容。

【关键概念扫盲】
1) asset_id 与文件名：保存时用 "前缀-随机串.扩展名" 作为磁盘文件名；asset_id 只是
   "前缀-随机串" 这段（不含扩展名）。因为扩展名由上传时的 MIME 决定、事先不一定知道，
   所以查找时用 `asset_id.*` 做前缀匹配（见 media_file）。
2) 路径穿越防护：如果把用户传进来的字符串直接拼进文件路径，攻击者可能用 "../../etc/passwd"
   之类跳出目录、读到系统文件。因此本文件对 asset_id 做了严格校验，杜绝 `/`、`\\`、`..`。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from core.config import settings

# MIME 类型 -> 文件扩展名 的映射表：上传时浏览器只给 MIME，落盘需要扩展名，故做此转换。
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
}


def ext_from_mime(mime: str, default: str = "bin") -> str:
    """把 MIME 类型转换成文件扩展名。

    参数：
        mime: 如 "image/png"、"audio/mpeg"。
        default: 未收录的 MIME 时返回的兜底扩展名。
    返回：
        扩展名字符串（不含点），如 "png"。
    """
    return _MIME_EXT.get((mime or "").lower(), default)


def save_media(data: bytes, ext: str, prefix: str = "asset") -> str:
    """保存二进制并返回 asset_id。

    参数：
        data: 文件字节内容。
        ext: 扩展名（可带或不带前导点，如 "png" 或 ".png"）。
        prefix: 文件名前缀，用于区分用途（如 image/audio，默认 asset）。
    返回：
        asset_id（形如 "asset-xxxxxxxxxxxx"，不含扩展名）。
    说明：
        只返回 asset_id 而不返回完整文件名，是为了让"对外标识"与"具体扩展名"解耦。
        磁盘上真实文件名为 "{asset_id}.{ext}"。
    """
    # 归一化扩展名：转小写并去掉可能的前导点，避免出现 "png." 之类的怪名字。
    ext = (ext or "bin").lower().lstrip(".")
    asset_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    directory = settings.media_dir()
    # 目录不存在就先建（mkdir -p），保证本地开发首次上传也能成功。
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{asset_id}.{ext}").write_bytes(data)
    return asset_id


def media_file(asset_id: str) -> Optional[Path]:
    """按 asset_id 查找媒体文件（扩展名不定，做前缀匹配）。

    参数：
        asset_id: 保存时返回的媒体标识。
    返回：
        找到则返回文件 Path；参数非法或找不到返回 None。
    说明：
        保存时扩展名不定，所以用 glob("{asset_id}.*") 做前缀匹配。
        安全：进入 glob 之前先做路径穿越校验——只要 asset_id 里含 "/"、"\\" 或 ".."
        就直接拒绝，防止构造出跳出 media 目录的路径。
    """
    if not asset_id or "/" in asset_id or "\\" in asset_id or ".." in asset_id:
        return None
    directory = settings.media_dir()
    if not directory.exists():
        return None
    for path in directory.glob(f"{asset_id}.*"):
        if path.is_file():
            return path
    return None


def media_url(asset_id: str) -> str:
    """拼出该媒体的对外访问地址（实际下载仍会走鉴权接口）。"""
    return f"/api/media/{asset_id}"


def guess_ext_from_filename(filename: str) -> str:
    """从文件名猜扩展名（如 "a.PNG" -> "png"）。

    参数：
        filename: 原始文件名。
    返回：
        小写扩展名；无扩展名或传入为空时返回 "bin"。
    说明：
        rsplit(".", 1) 只按最后一个点切分，避免把 "my.file.txt" 误切成 "file"。
    """
    if not filename or "." not in filename:
        return "bin"
    return filename.rsplit(".", 1)[1].lower()
