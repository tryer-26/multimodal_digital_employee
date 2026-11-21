"""ChromaDB 向量库封装（单集合、单维度），见设计文档 7.2 / 7.3 / 7.6。

采用**文本优先（完全文本化）**方案：文本 / 表格 / 图片描述都以文本嵌入映射到同一
向量空间，因此只需一个集合、一次查询即可覆盖全部模态，跨模态相似度可直接排序比较。
图片原图不参与检索，存 `data/media` 并以 ``asset_id`` 关联，检索命中后作附件回传。

对外单例：``vector_store``。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

import chromadb

from core.config import settings

logger = logging.getLogger(__name__)


def _sanitize_metadata(metadata: dict) -> dict:
    """把 metadata 规整为「可序列化标量」字典。

    规则：None -> ""；dict/list -> JSON 字符串；其余标量原样保留。
    ChromaDB 仅接受 str/int/float/bool 类型的元数据值。
    """
    clean: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if value is None:
            clean[str(key)] = ""
        elif isinstance(value, (str, int, float, bool)):
            clean[str(key)] = value
        elif isinstance(value, (dict, list, tuple)):
            clean[str(key)] = json.dumps(value, ensure_ascii=False)
        else:
            clean[str(key)] = str(value)
    return clean


class VectorStore:
    """ChromaDB 单集合向量库。"""

    def __init__(
        self,
        path: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        self._path = str(path) if path is not None else str(settings.chroma_path())
        self._collection_name = collection_name or settings.CHROMA_COLLECTION
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection = None

    # ------------------------------------------------------------------
    # 内部：懒加载
    # ------------------------------------------------------------------
    def _get_collection(self):
        if self._collection is None:
            self._client = chromadb.PersistentClient(path=self._path)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(
        self,
        embedding: list[float],
        metadata: dict,
        chunk_id: Optional[str] = None,
    ) -> str:
        """写入单条向量，返回 chunk_id。"""
        ids = self.add_batch([embedding], [metadata], [chunk_id] if chunk_id else None)
        return ids[0]

    def add_batch(
        self,
        embeddings: list[list[float]],
        metadatas: list[dict],
        chunk_ids: Optional[list[str]] = None,
    ) -> list[str]:
        """批量写入向量，返回 chunk_id 列表。"""
        if not embeddings:
            return []
        if len(embeddings) != len(metadatas):
            raise ValueError("embeddings 与 metadatas 数量不一致。")
        if chunk_ids is None:
            chunk_ids = [f"chunk-{uuid.uuid4().hex[:16]}" for _ in embeddings]
        if len(chunk_ids) != len(embeddings):
            raise ValueError("chunk_ids 与 embeddings 数量不一致。")

        collection = self._get_collection()
        collection.add(
            ids=chunk_ids,
            embeddings=embeddings,
            metadatas=[_sanitize_metadata(m) for m in metadatas],
        )
        return chunk_ids

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self,
        embedding: list[float],
        top_k: int = 10,
        where: Optional[dict] = None,
    ) -> list[dict]:
        """向量检索，返回 ``[{"id","score","metadata"}]``。

        score = 1 - distance（cosine 距离），并 clamp 到 [0, 1]。
        """
        collection = self._get_collection()
        if collection.count() == 0:
            return []

        result = collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where=where or None,
            include=["metadatas", "distances"],
        )

        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

        hits: list[dict] = []
        for i, chunk_id in enumerate(ids):
            distance = float(distances[i]) if i < len(distances) else 1.0
            score = max(0.0, min(1.0, 1.0 - distance))
            metadata = metadatas[i] if i < len(metadatas) else {}
            hits.append({"id": chunk_id, "score": score, "metadata": metadata or {}})
        return hits

    def list_by_document(self, document_id: str) -> list[dict]:
        """列出某文档的全部分块，返回 ``[{"id","metadata"}]``，按 chunk_index 升序。

        用于知识库管理端「查看分块内容」（见 7.6）。
        """
        collection = self._get_collection()
        if collection.count() == 0:
            return []

        result = collection.get(
            where={"document_id": document_id},
            include=["metadatas"],
        )
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []

        chunks: list[dict] = []
        for i, chunk_id in enumerate(ids):
            metadata = metadatas[i] if i < len(metadatas) else {}
            chunks.append({"id": chunk_id, "metadata": metadata or {}})

        def _order(item: dict) -> int:
            try:
                return int(item["metadata"].get("chunk_index") or 0)
            except (TypeError, ValueError):
                return 0

        chunks.sort(key=_order)
        return chunks

    # ------------------------------------------------------------------
    # 删除 / 统计 / 重置
    # ------------------------------------------------------------------
    def delete_by_document(self, document_id: str) -> int:
        """按 ``metadata["document_id"]`` 删除某文档的全部向量，返回删除数量。"""
        collection = self._get_collection()
        before = collection.count()
        collection.delete(where={"document_id": document_id})
        after = collection.count()
        return max(0, before - after)

    def count(self) -> int:
        """集合内向量总数。"""
        return self._get_collection().count()

    def reset(self) -> None:
        """删除并重建集合（清空全部向量）。"""
        if self._client is None:
            self._client = chromadb.PersistentClient(path=self._path)
        try:
            self._client.delete_collection(name=self._collection_name)
        except Exception as exc:  # 集合不存在时忽略
            logger.info("重建索引前删除集合遇到提示（可忽略）：%s", exc)
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------
vector_store = VectorStore()
