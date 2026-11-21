"""文档解析：把各种格式统一解析为「页 → 文本 / 图片 / 表格」块，见设计文档 7.3 / 7.6。

支持格式：
  - ``.md`` / ``.txt``：纯文本，按标题切块（无图片、表格）；
  - ``.pdf``：PyMuPDF 逐页抽取文本 / 内嵌图片 / 表格；纯图页整页以 150dpi 渲染为图像块；
  - ``.docx``：python-docx 抽取段落文本 / 内嵌图片 / 表格（转 Markdown）；
  - ``.png`` / ``.jpg`` / ``.jpeg``：整图作为一页，一张图像块。

对外接口：:func:`parse_document`；工具函数：:func:`table_to_markdown`、:func:`downsample`。
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 图片格式后缀集合
_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "gif"}
# PDF 内嵌图片的最小边长（过滤装饰性小图 / 图标）
_MIN_IMAGE_SIDE = 50


class ParseError(RuntimeError):
    """文档解析失败（含中文可读信息）。"""


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class ParsedImage:
    """一张待入库的图片块。"""

    id: str
    data: bytes
    ext: str
    caption: str = ""


@dataclass
class ParsedTable:
    """一个已结构化为 Markdown 的表格块。"""

    markdown: str


@dataclass
class ParsedPage:
    """一页（或一个文本小节）的解析结果。"""

    no: int
    text: str
    images: list[ParsedImage] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    scanned: bool = False  # 扫描件 / 纯图页标记


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def table_to_markdown(rows: Any) -> str:
    """把二维表格（行列表）转为 Markdown 表格。

    处理 None 单元格、转义 ``|``、单元格内换行压平为空格。
    """
    if not rows:
        return ""

    normalized: list[list[str]] = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            value = "" if cell is None else str(cell)
            value = value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
            cells.append(value)
        normalized.append(cells)

    if not any(any(c for c in row) for row in normalized):
        return ""

    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]

    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _encode(image: Any, fmt: str, **kwargs: Any) -> bytes:
    """把 PIL 图像编码为指定格式的字节。"""
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def downsample(data: bytes, max_side: int, max_bytes: int = 0) -> tuple[bytes, str]:
    """图片降采样：既压长边，也压字节数。

    嵌入 / 视觉理解接口对图片有**双重上限**：长边像素与编码后字节数
    （实测硅基流动图片输入上限 32KB）。本函数保证输出同时满足两者：

    1. 长边压到 ``max_side`` 以内；
    2. 优先输出 PNG（无损，适合含文字的截图）；超出 ``max_bytes`` 时改用 JPEG
       并逐级降质量；
    3. 仍超限则逐级缩小像素，直至满足上限。

    输入本身已满足两项约束且为 PNG/JPEG 时直接透传，避免重复有损编码。
    返回 ``(bytes, ext)``；非法图片抛 :class:`ParseError`。
    """
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        src_format = (image.format or "").lower()
    except Exception as exc:
        raise ParseError(f"图片解码失败，无法降采样：{exc}") from exc

    try:
        oversize_pixels = bool(max_side) and max(image.size) > max_side
        oversize_bytes = bool(max_bytes) and len(data) > max_bytes
        if not oversize_pixels and not oversize_bytes and src_format in ("png", "jpeg"):
            # 已合规，直接透传，避免二次编码损失
            return data, "png" if src_format == "png" else "jpg"

        if oversize_pixels:
            image.thumbnail((max_side, max_side), Image.LANCZOS)

        if image.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in image.getbands()
            image = image.convert("RGBA" if has_alpha else "RGB")

        png = _encode(image, "PNG")
        if not max_bytes or len(png) <= max_bytes:
            return png, "png"

        # PNG 超限：转 JPEG 并逐级降质量
        rgb = image.convert("RGB")
        jpg = png
        for quality in (85, 70, 55, 40):
            jpg = _encode(rgb, "JPEG", quality=quality, optimize=True)
            if len(jpg) <= max_bytes:
                return jpg, "jpg"

        # 仍超限：逐级缩小像素
        while max(rgb.size) > 64:
            rgb = rgb.resize(
                (max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)), Image.LANCZOS
            )
            jpg = _encode(rgb, "JPEG", quality=40, optimize=True)
            if len(jpg) <= max_bytes:
                break
        return jpg, "jpg"
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"图片降采样失败：{exc}") from exc


# ----------------------------------------------------------------------
# 各格式解析实现
# ----------------------------------------------------------------------
def _split_text_sections(text: str) -> list[str]:
    """按 Markdown 标题切分文本；无标题时整篇为一节。"""
    lines = text.splitlines()
    sections: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip().startswith("#") and any(s.strip() for s in current):
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    sections = [s for s in sections if s]
    if not sections:
        stripped = text.strip()
        sections = [stripped] if stripped else []
    return sections


def _parse_text(path: Path) -> list[ParsedPage]:
    """解析 md / txt：按标题切块，无图片表格。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    sections = _split_text_sections(text)
    return [
        ParsedPage(no=i + 1, text=section, images=[], tables=[])
        for i, section in enumerate(sections)
    ]


def _parse_image(path: Path) -> list[ParsedPage]:
    """解析 png / jpg：整图作为一页，一张图像块。"""
    data = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    image = ParsedImage(id=path.stem, data=data, ext=ext, caption=path.name)
    return [ParsedPage(no=1, text="", images=[image], tables=[])]


def _extract_pdf_tables(page: Any) -> list[ParsedTable]:
    """抽取 PDF 页内表格（find_tables 不可用时跳过）。"""
    tables: list[ParsedTable] = []
    find_tables = getattr(page, "find_tables", None)
    if find_tables is None:
        return tables
    try:
        finder = find_tables()
    except Exception as exc:
        logger.info("PDF 表格检测跳过：%s", exc)
        return tables

    for table in getattr(finder, "tables", []):
        try:
            rows = table.extract()
        except Exception as exc:
            logger.info("PDF 表格抽取跳过：%s", exc)
            continue
        if not rows:
            continue
        markdown = table_to_markdown(rows)
        if markdown.strip():
            tables.append(ParsedTable(markdown=markdown))
    return tables


def _extract_pdf_images(doc: Any, page: Any) -> list[ParsedImage]:
    """抽取 PDF 页内嵌图片（按 xref 去重，过滤装饰小图）。"""
    images: list[ParsedImage] = []
    seen: set[int] = set()
    for image_info in page.get_images(full=True):
        xref = image_info[0]
        if xref in seen:
            continue
        seen.add(xref)
        # image_info: (xref, smask, width, height, bpc, colorspace, ...)
        width = image_info[2] if len(image_info) > 2 else 0
        height = image_info[3] if len(image_info) > 3 else 0
        if width and height and min(width, height) < _MIN_IMAGE_SIDE:
            continue
        try:
            base = doc.extract_image(xref)
        except Exception as exc:
            logger.info("PDF 图片抽取跳过（xref=%s）：%s", xref, exc)
            continue
        data = base.get("image")
        if not data:
            continue
        ext = base.get("ext", "png")
        images.append(
            ParsedImage(id=f"p{page.number + 1}-x{xref}", data=data, ext=ext, caption="")
        )
    return images


def _parse_pdf(path: Path) -> list[ParsedPage]:
    """解析 PDF：逐页文本 + 内嵌图片 + 表格；纯图页整页渲染。"""
    try:
        import pymupdf as fitz  # PyMuPDF（1.24+ 推荐入口，避免 fitz 别名弃用告警）
    except ImportError:
        try:
            import fitz  # 兼容旧版本 PyMuPDF
        except Exception as exc:
            raise ParseError(f"PDF 解析依赖 PyMuPDF 未安装：{exc}") from exc

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ParseError(f"PDF 打开失败：{exc}") from exc

    pages: list[ParsedPage] = []
    try:
        for page in doc:
            text = page.get_text("text") or ""
            tables = _extract_pdf_tables(page)

            if not text.strip() and not tables:
                # 纯图页 / 扫描件：整页渲染为图像块
                try:
                    pixmap = page.get_pixmap(dpi=150)
                    data = pixmap.tobytes("png")
                except Exception as exc:
                    raise ParseError(
                        f"PDF 第 {page.number + 1} 页渲染失败：{exc}"
                    ) from exc
                page_image = ParsedImage(
                    id=f"p{page.number + 1}-render", data=data, ext="png", caption=""
                )
                pages.append(
                    ParsedPage(
                        no=page.number + 1,
                        text="",
                        images=[page_image],
                        tables=[],
                        scanned=True,
                    )
                )
                continue

            images = _extract_pdf_images(doc, page)
            pages.append(
                ParsedPage(
                    no=page.number + 1,
                    text=text,
                    images=images,
                    tables=tables,
                    scanned=False,
                )
            )
    finally:
        doc.close()
    return pages


def _parse_docx(path: Path) -> list[ParsedPage]:
    """解析 docx：段落文本 + 内嵌图片 + 表格转 Markdown。"""
    try:
        from docx import Document
    except Exception as exc:
        raise ParseError(f"docx 解析依赖 python-docx 未安装：{exc}") from exc

    try:
        document = Document(str(path))
    except Exception as exc:
        raise ParseError(f"docx 打开失败：{exc}") from exc

    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs)

    images: list[ParsedImage] = []
    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        part = rel.target_part
        data = getattr(part, "blob", None)
        if not data:
            continue
        ext = str(getattr(part, "partname", "")).rsplit(".", 1)[-1].lower() or "png"
        if ext not in _IMAGE_EXTS:
            ext = "png"
        images.append(
            ParsedImage(id=f"img-{len(images) + 1}", data=data, ext=ext, caption="")
        )

    tables: list[ParsedTable] = []
    for table in getattr(document, "tables", []):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        markdown = table_to_markdown(rows)
        if markdown.strip():
            tables.append(ParsedTable(markdown=markdown))

    return [ParsedPage(no=1, text=text, images=images, tables=tables)]


_DISPATCH = {
    "md": _parse_text,
    "markdown": _parse_text,
    "txt": _parse_text,
    "pdf": _parse_pdf,
    "docx": _parse_docx,
    "png": _parse_image,
    "jpg": _parse_image,
    "jpeg": _parse_image,
    "webp": _parse_image,
    "bmp": _parse_image,
}


def _parse_sync(file_path: str) -> list[ParsedPage]:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise ParseError(f"文件不存在：{file_path}")
    ext = path.suffix.lower().lstrip(".")
    parser = _DISPATCH.get(ext)
    if parser is None:
        raise ParseError(
            f"不支持的文件类型：.{ext}（支持 md/txt/pdf/docx/png/jpg/jpeg）。"
        )
    try:
        return parser(path)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"解析文件失败（{path.name}）：{exc}") from exc


async def parse_document(file_path: str) -> list[ParsedPage]:
    """解析文档，返回统一的分页块结构（在子线程中执行，避免阻塞事件循环）。"""
    return await asyncio.to_thread(_parse_sync, file_path)


__all__ = [
    "ParsedImage",
    "ParsedTable",
    "ParsedPage",
    "ParseError",
    "parse_document",
    "table_to_markdown",
    "downsample",
]
