"""文本优先的多模态 RAG 服务，见设计文档 7.2 / 7.3 / 7.4 / 7.7。

采用**文本优先（完全文本化）**方案：入库时为每张配图生成文本描述，文本 / 表格 /
图片描述统一走 ``encode_text`` 映射到同一向量空间，只需一个集合；检索时文本查询直接
嵌入，图片查询先转成描述再嵌入，召回后用 Qwen3-VL-Reranker-8B 精排。图片原图不参与
检索，存 ``data/media`` 并以 ``asset_id`` 关联，命中后作附件回传。

对外单例：``multimodal_rag``。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from core.config import settings
from core.infra.db import db
from core.rag.embedding import (
    EmbeddingClient,
    RerankerClient,
    embedder as _default_embedder,
    reranker as _default_reranker,
)
from core.infra.media import save_media
from core.rag.multimodal import describe_knowledge_image
from core.rag.parsers import ParsedImage, downsample, parse_document
from core.rag.vector_store import VectorStore, vector_store as _default_vector_store

logger = logging.getLogger(__name__)

# 知识库文档目录（见项目结构：data/knowledge）
KNOWLEDGE_DIR = Path("data/knowledge")
# 支持重建索引的文件类型
_SUPPORTED_EXTS = {"md", "markdown", "txt", "pdf", "docx", "png", "jpg", "jpeg", "webp", "bmp"}
# 默认文本分块参数
_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 50


class MultimodalRAGService:
    """文本优先的多模态 RAG 服务：入库 / 检索 / 结果装配 / 索引管理。"""

    def __init__(
        self,
        embedder: Optional[EmbeddingClient] = None,
        reranker: Optional[RerankerClient] = None,
        vector_store: Optional[VectorStore] = None,
    ) -> None:
        self.embedder = embedder or _default_embedder
        self.reranker = reranker or _default_reranker
        self.vector_store = vector_store or _default_vector_store

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------
    async def add_document(
        self,
        file_path: str,
        category: str = "general",
        document_id: Optional[str] = None,
    ) -> dict:
        """文档入库：解析 → 分块（文本/图片/表格）→ 统一嵌入 → 落库（向量 + 元数据）。

        返回 ``{"document_id","modalities":{"text":n,"image":n,"table":n},"chunk_count":n}``。
        """
        pages = await parse_document(file_path)
        source_name = Path(file_path).name
        doc_id = document_id or f"doc-{uuid.uuid4().hex[:8]}"

        counts = {"text": 0, "image": 0, "table": 0}
        chunk_index = 0

        for page in pages:
            # 1. 文本块
            for chunk in self._chunk_text(page.text):
                vector = await self.embedder.encode_text(chunk)
                metadata = {
                    "modality": "text",
                    "content": chunk,
                    "source": source_name,
                    "page": page.no,
                    "document_id": doc_id,
                    "category": category,
                    "chunk_index": chunk_index,
                }
                self.vector_store.add(vector, metadata)
                counts["text"] += 1
                chunk_index += 1

            # 2. 图片块：降采样落盘取 asset_id，生成**配图描述**并按文本嵌入入库；
            #    原图不参与检索，命中后由 build_attachments 作附件回传
            for image in page.images:
                data, ext = downsample(
                    image.data,
                    settings.EMBEDDING_MAX_IMAGE_SIDE,
                    settings.EMBEDDING_MAX_IMAGE_BYTES,
                )
                asset_id = save_media(data, ext, prefix="kb")
                caption = ""
                if settings.KB_IMAGE_CAPTION_ENABLED:
                    caption = await describe_knowledge_image(data)
                metadata = {
                    "modality": "image",
                    "asset_id": asset_id,
                    "caption": caption,
                    "context": self._find_context(image, page.text),
                    "source": source_name,
                    "page": page.no,
                    "document_id": doc_id,
                    "category": category,
                    "chunk_index": chunk_index,
                }
                # 载体：描述优先，回退页正文，再回退文件名，避免空文本无法嵌入
                vector = await self.embedder.encode_text(
                    caption or metadata["context"] or source_name
                )
                self.vector_store.add(vector, metadata)
                counts["image"] += 1
                chunk_index += 1

            # 3. 表格块：结构化为 Markdown 后按文本嵌入，保住行列关系
            for table in page.tables:
                markdown = (table.markdown or "").strip()
                if not markdown:
                    continue
                vector = await self.embedder.encode_text(markdown)
                metadata = {
                    "modality": "table",
                    "content": markdown,
                    "source": source_name,
                    "page": page.no,
                    "document_id": doc_id,
                    "category": category,
                    "chunk_index": chunk_index,
                }
                self.vector_store.add(vector, metadata)
                counts["table"] += 1
                chunk_index += 1

        size_bytes = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        await db.upsert_document(
            doc_id, source_name, category, counts, chunk_index, size_bytes, "indexed"
        )
        logger.info(
            "入库完成：%s（document_id=%s，text=%d，image=%d，table=%d）",
            source_name, doc_id, counts["text"], counts["image"], counts["table"],
        )
        return {"document_id": doc_id, "modalities": counts, "chunk_count": chunk_index}

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    async def search(
        self,
        query: Optional[str] = None,
        query_image: Optional[bytes] = None,
        top_k: Optional[int] = None,
        text_only: bool = False,
        category: Optional[str] = None,
    ) -> list[dict]:
        """多模态检索（单集合、文本优先）：召回 → 精排。

        - 纯文本 ``query``：直接嵌入召回（image 块以其**配图描述**参与），精排后取
          ``RERANKER_TOP_N`` 条 —— 这是「文查图」的主路径；
        - ``query_image``：先由 VLM 生成一段描述，再用该描述做一次文本召回并精排 —— 这是
          「图查文 / 图查图」的路径；VLM 未配置或调用失败时图片查询返回空结果；
        - ``text_only=True`` 时限定只召回 text 块（image / table 块均不参与，
          纯文本模式对比；见 7.6）。
        """
        if not query and not query_image:
            raise ValueError("检索失败：query 与 query_image 至少需要提供一个。")

        limit = top_k or settings.RETRIEVAL_TOP_K
        where = self._build_where(text_only=text_only, category=category)

        if query_image:
            query = await describe_knowledge_image(query_image)
            if not query:
                logger.warning("图片查询无可用描述（VLM 未配置或调用失败），返回空结果。")
                return []
            label = f"<图片查询·描述>{query[:40]}"
        else:
            label = repr(query)

        vector = await self.embedder.encode_text(query or "")
        candidates = self.vector_store.search(vector, top_k=limit, where=where)
        self._log_candidates(label, limit, candidates)

        if self.reranker.enabled:
            results = await self.reranker.rerank(
                query or "", candidates, top_n=settings.RERANKER_TOP_N
            )
            logger.info("精排完成：%d 条候选 → 取前 %d 条", len(candidates), len(results))
            return results

        return candidates[: settings.RERANKER_TOP_N]

    @staticmethod
    def _log_candidates(query_desc: str, limit: int, candidates: list[dict]) -> None:
        logger.info(
            "多模态检索：query=%s top_k=%d 命中=%d",
            query_desc, limit, len(candidates),
        )
        for rank, hit in enumerate(candidates, 1):
            metadata = hit.get("metadata") or {}
            logger.info(
                "  召回[%d] modality=%s score=%.4f source=%s page=%s",
                rank, metadata.get("modality", "?"), hit.get("score", 0.0),
                metadata.get("source", "?"), metadata.get("page", "-"),
            )

    def _build_where(self, text_only: bool, category: Optional[str]) -> Optional[dict]:
        """构造 ChromaDB 过滤条件。"""
        conditions: list[dict] = []
        if text_only:
            conditions.append({"modality": {"$in": ["text"]}})
        if category:
            conditions.append({"category": category})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    # ------------------------------------------------------------------
    # 结果装配（见 7.7 / 8.4）
    # ------------------------------------------------------------------
    def format_results(self, results: list[dict]) -> str:
        """把检索结果拼成带来源标注的上下文。

        text / table 取 ``content``；image / page 优先取配图描述 ``caption``、回退 ``context``；
        每条前缀 ``[序号] 来源：文件（第 N 页·模态）``。
        """
        blocks: list[str] = []
        for index, result in enumerate(results, 1):
            metadata = result.get("metadata") or {}
            body = (
                metadata.get("content")
                or metadata.get("caption")
                or metadata.get("context", "")
            )
            blocks.append(
                f"[{index}] 来源：{metadata.get('source', '未知')}"
                f"（第{metadata.get('page', '-')}页·{metadata.get('modality', '?')}）\n{body}"
            )
        return "\n\n".join(blocks)

    def build_attachments(self, results: list[dict]) -> list[dict]:
        """把检索命中的图像块转为消息附件（见 8.4）。

        仅 ``modality == "image"`` 的输出，``url`` 走鉴权媒体接口，并附带配图描述 ``caption``。
        """
        attachments: list[dict] = []
        for result in results:
            metadata = result.get("metadata") or {}
            if metadata.get("modality") != "image":
                continue
            asset_id = metadata.get("asset_id")
            if not asset_id:
                continue
            attachments.append(
                {
                    "type": "image",
                    "url": f"/api/media/{asset_id}",
                    "source": metadata.get("source"),
                    "page": metadata.get("page"),
                    "caption": metadata.get("caption") or "",
                }
            )
        return attachments

    def build_citations(self, results: list[dict]) -> list[dict]:
        """生成「参考来源」列表（见 7.7 ③）：text / table / image 都列出，page 可为 None。"""
        citations: list[dict] = []
        for result in results:
            metadata = result.get("metadata") or {}
            citations.append(
                {
                    "source": metadata.get("source"),
                    "page": metadata.get("page"),
                    "modality": metadata.get("modality"),
                }
            )
        return citations

    # ------------------------------------------------------------------
    # 索引管理
    # ------------------------------------------------------------------
    async def delete_document(self, document_id: str) -> int:
        """删除文档：级联删除其全部向量（并清理元数据），返回删除的向量数。"""
        removed = self.vector_store.delete_by_document(document_id)
        await db.delete_document(document_id)
        logger.info("删除文档 %s：清理向量 %d 条", document_id, removed)
        return removed

    async def rebuild_index(self, force: bool = False) -> dict:
        """全量重建索引：清空集合后重跑 ``data/knowledge`` 下所有文件。

        注意：换嵌入模型、改维度或改索引语义后必须执行（见 7.6）。
        返回 ``{"documents": n, "chunks": n}``。
        """
        self.vector_store.reset()

        # 读出旧元数据用于继承分类，并清空文档表——否则重建会用新 id 重新落库，
        # 导致知识库文档列表出现重复行（向量已清空，旧行不再对应任何分块）。
        category_by_name: dict[str, str] = {}
        try:
            for doc in await db.list_documents():
                filename = doc.get("filename") or ""
                if filename:
                    category_by_name.setdefault(
                        Path(filename).name, doc.get("category") or "general"
                    )
                await db.delete_document(doc["id"])
        except Exception as exc:
            logger.warning("重置文档元数据失败，分类将默认 general：%s", exc)

        files: list[Path] = []
        if KNOWLEDGE_DIR.exists():
            for path in sorted(KNOWLEDGE_DIR.rglob("*")):
                if path.is_file() and path.suffix.lower().lstrip(".") in _SUPPORTED_EXTS:
                    files.append(path)

        documents = 0
        chunks = 0
        for path in files:
            category = category_by_name.get(path.name, "general")
            try:
                result = await self.add_document(str(path), category=category)
            except Exception as exc:
                logger.error("重建索引跳过 %s：%s", path.name, exc)
                continue
            documents += 1
            chunks += int(result.get("chunk_count", 0))

        logger.info("重建索引完成：documents=%d chunks=%d（force=%s）", documents, chunks, force)
        return {"documents": documents, "chunks": chunks}

    def stats(self) -> dict:
        """向量库统计（单集合）。"""
        return {
            "chunks": self.vector_store.count(),
            "collection": settings.CHROMA_COLLECTION,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_text(
        text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP
    ) -> list[str]:
        """按固定窗口滑动切分文本（默认 500 字、重叠 50 字）。"""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        step = max(1, chunk_size - overlap)
        chunks: list[str] = []
        for start in range(0, len(text), step):
            piece = text[start : start + chunk_size].strip()
            if piece:
                chunks.append(piece)
            if start + chunk_size >= len(text):
                break
        return chunks

    @staticmethod
    def _find_context(image: ParsedImage, page_text: str) -> str:
        """取图片所在页的文本摘要（前 200 字）作为图像块的上下文。"""
        if image.caption:
            return image.caption[:200]
        return (page_text or "")[:200]


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------
multimodal_rag = MultimodalRAGService()
