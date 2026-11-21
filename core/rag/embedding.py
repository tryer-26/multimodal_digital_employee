"""嵌入与精排 API 封装（OpenAI 兼容协议），见设计文档 7.2 / 7.4 / 第十七章。

设计要点：
  - 文本优先嵌入：文本、表格 Markdown、图片配图描述统一走 ``encode_text`` 进同一文本空间
    （Qwen3-VL-Embedding-8B）；图片本身不直接嵌入；
  - 精排走 OpenAI 兼容的 ``POST {base_url}/rerank``（Qwen3-VL-Reranker-8B）；
  - 所有同步 SDK / HTTP 调用一律用 :func:`asyncio.to_thread` 包装，避免阻塞事件循环。

对外单例：``embedder``、``reranker``。
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Optional

import httpx
from openai import OpenAI

from core.config import settings

logger = logging.getLogger(__name__)

# 单批文本 embedding 的最大条数（避免单次请求过大）
_EMBED_BATCH_SIZE = 32
# 网络调用超时（秒）
_REQUEST_TIMEOUT = 30.0


class EmbeddingError(RuntimeError):
    """嵌入调用失败（含中文可读信息）。"""


def _is_unsupported_param_error(exc: Exception) -> bool:
    """判断异常是否为「供应商不认 dimensions 参数」。

    必须与超时/网络/鉴权等**瞬时**故障区分开：瞬时故障若被误当成「参数不支持」，
    客户端会退回模型原生维度（4096）并与按 EMBEDDING_DIM 建好的索引不匹配，
    且 `_use_dimensions` 被永久置 False，导致进程内所有检索静默失效。

    因此**只认异常文本**，要求同时命中 "dimension" 与「不支持/未知/非法」语义；
    不能按异常类型（如 BadRequestError）放宽——图片超限、非法输入等同样返回 400，
    一旦被误判就会永久关闭 dimensions 参数，使进程内嵌入全线失败。
    """
    text = str(exc).lower()
    return "dimension" in text and any(
        token in text for token in ("unsupported", "not support", "unknown", "invalid")
    )


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _detect_image_mime(data: bytes) -> str:
    """按文件头判断图片 MIME 类型，无法识别时默认 image/png。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/png"


def build_data_url(image_bytes: bytes) -> str:
    """把图片字节编码为 data URL（供多模态嵌入 / 视觉理解接口使用）。"""
    mime = _detect_image_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


class EmbeddingClient:
    """多模态嵌入客户端（OpenAI 兼容 ``/v1/embeddings``）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
    ) -> None:
        self._base_url = base_url if base_url is not None else settings.EMBEDDING_BASE_URL
        self._api_key = api_key if api_key is not None else settings.embedding_key
        self._model = model if model is not None else settings.EMBEDDING_MODEL
        # Qwen3-VL-Embedding-8B 原生 4096 维，通过 dimensions 参数做 Matryoshka 截断，
        # 与 EMBEDDING_DIM 对齐以控制存储与成本（见 7.2 / 第十六章）。
        self._dimensions = dimensions if dimensions is not None else settings.EMBEDDING_DIM
        # 供应商不支持 dimensions 参数时置 False，后续调用不再携带（保证换供应商只改配置）
        self._use_dimensions = True
        self._client: Optional[OpenAI] = None

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    def available(self) -> bool:
        """嵌入能力是否可用（三项配置齐全）。"""
        return bool(self._base_url and self._model and self._api_key)

    # ------------------------------------------------------------------
    # 内部：懒加载同步客户端
    # ------------------------------------------------------------------
    def _client_or_raise(self) -> OpenAI:
        if not self.available():
            raise EmbeddingError(
                "嵌入模型未配置：请设置 EMBEDDING_BASE_URL / EMBEDDING_MODEL / "
                "EMBEDDING_API_KEY（未单独配置 Key 时回退使用 SILICONFLOW_API_KEY）。"
            )
        if self._client is None:
            self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    def _embed_sync(self, inputs: list[str]) -> list[list[float]]:
        """同步批量嵌入（文本或 data URL 图片）。"""
        client = self._client_or_raise()
        kwargs: dict = {"model": self._model, "input": inputs, "timeout": _REQUEST_TIMEOUT}
        if self._dimensions and self._use_dimensions:
            kwargs["dimensions"] = self._dimensions

        try:
            resp = client.embeddings.create(**kwargs)
        except Exception as exc:  # 网络 / 鉴权 / 模型等一切异常统一包装
            # 只有「供应商不认 dimensions 参数」才降级为原生维度重试；超时/网络/鉴权等
            # 瞬时故障必须原样抛出——否则会静默退回 4096 维，与按 EMBEDDING_DIM 建好的
            # 索引不兼容，且此后进程内每次检索都失败（实测一次 Request timed out 即触发）
            if "dimensions" not in kwargs or not _is_unsupported_param_error(exc):
                raise EmbeddingError(f"嵌入调用失败：{exc}") from exc
            # 供应商不支持 dimensions 参数：降级为模型原生维度重试一次，并记住该能力
            logger.warning("嵌入接口不支持 dimensions 参数，降级为模型原生维度：%s", exc)
            self._use_dimensions = False
            kwargs.pop("dimensions")
            try:
                resp = client.embeddings.create(**kwargs)
            except Exception as retry_exc:
                raise EmbeddingError(f"嵌入调用失败：{retry_exc}") from retry_exc

        # 按 index 排序，保证返回顺序与输入一致
        items = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        return [[float(x) for x in item.embedding] for item in items]

    def probe_dim(self) -> int:
        """同步探测一次嵌入维度（供启动自检使用，走与业务完全相同的调用路径）。"""
        return len(self._embed_sync(["维度自检"])[0])

    def _ensure_dim(self, vectors: list[list[float]]) -> None:
        """校验返回维度与 ``EMBEDDING_DIM`` 一致。

        维度不一致必须在入库/检索之前失败：把不同维的向量交给 ChromaDB 只会得到
        「Collection expecting embedding with dimension of X, got Y」，并且此后每次
        检索都失败。启动自检走 ``probe_dim``（不经过本校验），仍按原口径给出明确的
        配置错误提示。
        """
        if not self._dimensions or not vectors:
            return
        got = len(vectors[0])
        if got != self._dimensions:
            raise EmbeddingError(
                f"嵌入维度不一致：配置 EMBEDDING_DIM={self._dimensions}，模型实际返回 {got}。"
                "请对齐 EMBEDDING_DIM 后重建索引（见设计文档 7.2 / 17.3）。"
            )

    # ------------------------------------------------------------------
    # 对外异步接口
    # ------------------------------------------------------------------
    async def encode_text(self, text: str) -> list[float]:
        """编码单条文本为向量。"""
        vectors = await self.encode_texts([text])
        return vectors[0]

    async def encode_texts(self, texts: list[str]) -> list[list[float]]:
        """批量编码文本为向量；内部按 32 条一批分片。"""
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            vectors = await asyncio.to_thread(self._embed_sync, batch)
            self._ensure_dim(vectors)
            results.extend(vectors)
        return results


class RerankerClient:
    """精排客户端（OpenAI 兼容 ``POST {base_url}/rerank``），可关闭。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self._base_url = base_url if base_url is not None else settings.RERANKER_BASE_URL
        self._api_key = api_key if api_key is not None else settings.reranker_key
        self._model = model if model is not None else settings.RERANKER_MODEL

    @property
    def enabled(self) -> bool:
        """精排是否可用：开关打开且 base_url / model 非空。"""
        return bool(settings.RERANKER_ENABLED and self._base_url and self._model)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _candidate_text(candidate: dict) -> str:
        """把候选块转成参与精排的可检索文本。

        text / table 取 ``metadata["content"]``；image / page 优先取配图描述 ``caption``、
        无描述时回退 ``context``（页正文）。
        """
        metadata = candidate.get("metadata") or {}
        modality = metadata.get("modality", "text")
        if modality in ("image", "page"):
            return str(
                metadata.get("caption")
                or metadata.get("context")
                or metadata.get("content")
                or ""
            )
        return str(metadata.get("content") or metadata.get("context") or "")

    def _rerank_sync(self, query: str, documents: list[str], top_n: int) -> list[dict]:
        """同步调用精排接口，返回 [{"index","score"}]。"""
        url = self._base_url.rstrip("/") + "/rerank"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        resp = httpx.post(url, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        raw = data.get("results") if isinstance(data, dict) else data
        if raw is None and isinstance(data, dict):
            raw = data.get("data")
        if not isinstance(raw, list):
            raise RuntimeError(f"精排返回结构无法识别：{data!r}")

        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", item.get("corpus_id"))
            score = item.get("relevance_score", item.get("score"))
            if idx is None or score is None:
                continue
            out.append({"index": int(idx), "score": float(score)})
        return out

    # ------------------------------------------------------------------
    # 对外异步接口
    # ------------------------------------------------------------------
    async def rerank(
        self, query: str, candidates: list[dict], top_n: int
    ) -> list[dict]:
        """对候选结果精排。

        输入 ``candidates`` 元素形如 ``{"id","score","metadata"}``，返回同结构列表，
        并把精排分数写回 ``score``，按分数降序取前 ``top_n``。
        接口不可用时记录日志并回退到原顺序（不抛异常）。
        """
        if not candidates:
            return []
        if not self.enabled:
            return candidates[:top_n]

        documents = [self._candidate_text(c) for c in candidates]
        try:
            ranked = await asyncio.to_thread(
                self._rerank_sync, query, documents, top_n
            )
        except Exception as exc:  # 精排失败不应阻断主链路
            logger.warning("精排调用失败，回退向量召回顺序：%s", exc)
            return candidates[:top_n]

        if not ranked:
            logger.warning("精排返回为空，回退向量召回顺序。")
            return candidates[:top_n]

        merged: list[dict] = []
        for entry in ranked:
            idx = entry["index"]
            if idx < 0 or idx >= len(candidates):
                continue
            candidate = dict(candidates[idx])
            candidate["score"] = entry["score"]
            merged.append(candidate)

        merged.sort(key=lambda c: c.get("score", 0.0), reverse=True)
        return merged[:top_n]


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------
embedder = EmbeddingClient()
reranker = RerankerClient()
