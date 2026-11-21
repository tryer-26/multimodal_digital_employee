"""多模态 RAG 单元测试（不依赖外部 API：注入假嵌入器）。"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from core.config import settings
from core.rag.multimodal_rag import MultimodalRAGService
from core.rag.parsers import downsample, parse_document, table_to_markdown
from core.service.turn_context import TurnRecorder
from core.rag.vector_store import VectorStore

DIM = 32
# 素材目录（docs/ 下）：三种 PDF + 两种 Word + 六张图片，见设计文档 7.3
ASSET_DIR = Path(__file__).resolve().parents[1] / "docs" / "素材"
PNG_ASSETS = [
    "finance_error.png",
    "install_permission.png",
    "mail_config.png",
    "network_topology.png",
    "vpn_config_page.png",
    "vpn_error_691.png",
]


class FakeEmbedder:
    """确定性假嵌入器：文本按字符哈希散点，图片按字节哈希散点。"""

    def available(self) -> bool:
        return True

    def _vec(self, seed: str) -> list[float]:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        raw = [b / 255.0 for b in digest]
        vec = (raw * math.ceil(DIM / len(raw)))[:DIM]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def encode_text(self, text: str) -> list[float]:
        return self._vec(text)

    async def encode_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class DisabledReranker:
    enabled = False

    async def rerank(self, query: str, candidates: list[dict], top_n: int) -> list[dict]:
        return candidates[:top_n]


class FakeDB:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def upsert_document(self, doc_id, filename, category, modalities, chunk_count, size_bytes, status="indexed"):
        self.docs[doc_id] = {"id": doc_id, "filename": filename, "chunk_count": chunk_count}

    async def delete_document(self, doc_id: str) -> None:
        self.docs.pop(doc_id, None)


@pytest.fixture(autouse=True)
def no_vlm_caption(monkeypatch):
    """默认屏蔽入库 / 检索期的 VLM 配图描述，避免测试触网；需要时由用例自行覆盖。"""

    async def _empty(image_bytes: bytes) -> str:
        return ""

    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _empty)


@pytest.fixture()
def rag(tmp_path, monkeypatch):
    store = VectorStore(path=str(tmp_path / "chroma"), collection_name="test_kb")
    fake_db = FakeDB()
    monkeypatch.setattr("core.rag.multimodal_rag.db", fake_db)
    service = MultimodalRAGService(
        embedder=FakeEmbedder(),
        reranker=DisabledReranker(),
        vector_store=store,
    )
    return service, store, fake_db


def test_table_to_markdown_handles_none_and_pipe():
    md = table_to_markdown([["列A", "列B"], [None, "x|y"]])
    assert "| 列A | 列B |" in md
    assert "x\\|y" in md


def test_downsample_limits_long_side():
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (2000, 1000), "white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    data, ext = downsample(buf.getvalue(), max_side=512)
    assert ext in ("png", "PNG") or ext.lstrip(".") == "png"
    out = Image.open(BytesIO(data))
    assert max(out.size) <= 512


@pytest.mark.asyncio
async def test_parse_markdown_splits_blocks(tmp_path):
    path = tmp_path / "vpn.md"
    path.write_text("# 标题\n\n第一段内容。\n\n## 小节\n\n第二段内容。", encoding="utf-8")
    pages = await parse_document(str(path))
    assert pages and pages[0].text


@pytest.mark.asyncio
async def test_add_search_and_delete(rag, tmp_path):
    service, store, fake_db = rag
    path = tmp_path / "vpn.md"
    path.write_text("# VPN 连接失败\n\n错误 619 表示无法建立到远程计算机的连接。", encoding="utf-8")

    result = await service.add_document(str(path), category="network", document_id="doc-1")
    assert result["chunk_count"] >= 1
    assert "doc-1" in fake_db.docs
    assert store.count() == result["chunk_count"]

    hits = await service.search(query="VPN 错误 619")
    assert hits, "应至少召回一条"
    assert hits[0]["metadata"]["modality"] in ("text", "table")

    removed = await service.delete_document("doc-1")
    assert removed >= 1
    assert store.count() == 0


@pytest.mark.asyncio
async def test_result_assembly(rag, tmp_path):
    service, _store, _db = rag
    results = [
        {"id": "a", "score": 0.9, "metadata": {"modality": "text", "source": "vpn.md", "page": 2, "content": "正文"}},
        {"id": "b", "score": 0.8, "metadata": {"modality": "table", "source": "err.md", "page": 1, "content": "| a | b |"}},
        {"id": "c", "score": 0.7, "metadata": {"modality": "image", "source": "s.png", "page": 1, "asset_id": "kb-1", "context": "上下文"}},
    ]
    context = service.format_results(results)
    assert "来源：vpn.md（第2页·text）" in context

    attachments = service.build_attachments(results)
    assert len(attachments) == 1 and attachments[0]["url"] == "/api/media/kb-1"

    citations = service.build_citations(results)
    assert len(citations) == 3


# ======================================================================
# 素材驱动的多模态用例（基于 docs/素材/ 下的真实文件，见设计文档 7.3）
# ======================================================================
@pytest.fixture()
def media_stub(monkeypatch):
    """替掉 save_media：素材用例只验证「分块模态」，不落盘污染 data/media。"""
    saved: list[str] = []

    def _save(data: bytes, ext: str, prefix: str = "asset") -> str:
        asset_id = f"{prefix}-test{len(saved) + 1}"
        saved.append(asset_id)
        return asset_id

    monkeypatch.setattr("core.rag.multimodal_rag.save_media", _save)
    return saved


class RecordingReranker:
    """会反转候选顺序且记录调用次数的精排器，用于锁定「图片查询不做文本精排」。"""

    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query: str, candidates: list[dict], top_n: int) -> list[dict]:
        self.calls += 1
        return list(reversed(candidates))[:top_n]


class StubVectorStore:
    """固定返回给定候选，隔离 Chroma 与嵌入，专测检索编排逻辑。"""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits

    def search(self, vector, top_k: int, where=None) -> list[dict]:  # noqa: ANN001
        return self.hits[:top_k]

    def count(self) -> int:
        return len(self.hits)


class StubRAG:
    """替掉 crew_tools 里的 multimodal_rag，用于验证 recorder 的「保留最优命中」。"""

    def __init__(self, results: list[dict]) -> None:
        self.results = results

    async def search(self, query=None, query_image=None, **kwargs) -> list[dict]:  # noqa: ANN001
        return self.results

    def format_results(self, results: list[dict]) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_asset_pdf_table_extracts_table_chunk(rag, media_stub):
    """表格型 PDF：错误码对照表必须落成 table 块（保住行列关系）。"""
    service, _store, _db = rag
    path = ASSET_DIR / "pdf" / "表格_IT常见错误码对照表.pdf"
    result = await service.add_document(str(path), category="network", document_id="doc-err")

    assert result["modalities"]["table"] >= 1
    assert result["modalities"]["text"] >= 1
    assert result["chunk_count"] == sum(result["modalities"].values())


@pytest.mark.asyncio
async def test_asset_pdf_mixed_has_text_image_table(rag, media_stub):
    """图文混合 PDF：文本、内嵌图、表格三类块都要有。"""
    service, _store, _db = rag
    path = ASSET_DIR / "pdf" / "图文混合_软件安装与权限申请指南.pdf"
    result = await service.add_document(str(path), category="software", document_id="doc-mixed")

    assert result["modalities"]["text"] >= 1
    assert result["modalities"]["image"] >= 1
    assert result["modalities"]["table"] >= 1
    assert media_stub, "图片块应当落盘并分配 asset_id"


@pytest.mark.asyncio
async def test_asset_pdf_scanned_falls_back_to_page_image(rag, media_stub):
    """扫描型 PDF：无文本层，整页渲染为 image 块（text 必须为 0）。"""
    service, _store, _db = rag
    path = ASSET_DIR / "pdf" / "扫描型_VPN连接故障排查手册.pdf"
    result = await service.add_document(str(path), category="network", document_id="doc-scan")

    assert result["modalities"]["text"] == 0
    assert result["modalities"]["image"] >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PNG_ASSETS)
async def test_asset_png_is_single_image_chunk(rag, media_stub, name):
    """单图素材：一张图 = 一个 image 块，不产生 text / table。"""
    service, _store, _db = rag
    result = await service.add_document(
        str(ASSET_DIR / "images" / name), category="general", document_id=f"doc-{name}"
    )

    assert result["modalities"] == {"text": 0, "image": 1, "table": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["密码重置与账号解锁SOP.docx", "报销系统操作与常见问题.docx"])
async def test_asset_docx_extracts_text_and_table(rag, media_stub, name):
    """Word 素材：段落 → text 块，表内数据 → table 块，内嵌图 → image 块。"""
    service, _store, _db = rag
    result = await service.add_document(
        str(ASSET_DIR / "word" / name), category="general", document_id=f"doc-{name}"
    )

    assert result["modalities"]["text"] >= 1
    assert result["modalities"]["table"] >= 1
    assert result["modalities"]["image"] >= 1


@pytest.mark.asyncio
async def test_image_query_self_matches_image_chunk(rag, media_stub, monkeypatch):
    """图查图（文本优先）：查询图先转描述，再按描述做文本召回，命中其 image 块。"""
    service, _store, _db = rag
    caption = "企业远程办公网络拓扑图，含 VPN 网关与分支节点"

    async def _caption(image_bytes: bytes) -> str:
        return caption

    # 入库与查询共用同一描述，保证查询向量与入库向量同源
    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _caption)
    path = ASSET_DIR / "images" / "network_topology.png"
    await service.add_document(str(path), category="network", document_id="doc-topo")

    data, _ext = downsample(
        path.read_bytes(), settings.EMBEDDING_MAX_IMAGE_SIDE, settings.EMBEDDING_MAX_IMAGE_BYTES
    )
    hits = await service.search(query_image=data)

    assert hits
    assert hits[0]["metadata"]["modality"] == "image"
    assert hits[0]["metadata"]["source"] == "network_topology.png"


@pytest.mark.asyncio
async def test_image_query_no_caption_returns_empty(rag, media_stub):
    """VLM 未配置（描述为空）时图片查询返回空，不误召回（② 的代价）。"""
    service, _store, _db = rag
    await service.add_document(
        str(ASSET_DIR / "images" / "network_topology.png"), document_id="doc-nocap-q"
    )

    hits = await service.search(query_image=b"topology-bytes")

    assert hits == []


@pytest.mark.asyncio
async def test_text_only_search_excludes_image_chunks(rag, media_stub):
    """text_only 模式（纯文本对比）：图像块与表格块均不得参与召回。"""
    service, _store, _db = rag
    await service.add_document(
        str(ASSET_DIR / "pdf" / "图文混合_软件安装与权限申请指南.pdf"), document_id="doc-mixed"
    )

    hits = await service.search(query="软件安装与权限申请", text_only=True)

    assert hits
    assert all(h["metadata"]["modality"] == "text" for h in hits)


@pytest.mark.asyncio
async def test_image_query_uses_description_and_reranks(monkeypatch):
    """图片查询（文本优先）：先转描述，用描述走文本检索并按描述精排。"""
    img_hit = {
        "id": "img", "score": 0.70,
        "metadata": {"modality": "image", "asset_id": "kb-1", "source": "network_topology.png", "page": 1},
    }
    txt_hit = {
        "id": "txt", "score": 0.60,
        "metadata": {"modality": "text", "source": "error_code_table.md", "page": 1},
    }
    reranker = RecordingReranker()

    async def _caption(image_bytes: bytes) -> str:
        return "网络拓扑图，VPN 网关与分支节点"

    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _caption)
    service = MultimodalRAGService(
        embedder=FakeEmbedder(),
        reranker=reranker,
        vector_store=StubVectorStore([img_hit, txt_hit]),
    )

    hits = await service.search(query_image=b"topology-bytes")
    assert reranker.calls == 1, "图片查询已转为描述文本，需经精排"
    assert hits[0]["id"] == "txt", "RecordingReranker 反转顺序，证明精排确实生效"

    # 纯文本查询同样走精排
    await service.search(query="VPN错误码619处理建议")
    assert reranker.calls == 2


def test_search_tool_keeps_best_hit(monkeypatch):
    """回归｜修复 A：低分检索不得覆盖此前的高分命中。

    复现「上传拓扑图 → Agent 凭空搜『错误码619』」：低分文本结果不能顶掉图片自匹配。
    """
    import core.ai.crew_tools as crew_tools
    from core.ai.crew_tools import MultimodalSearchTool

    recorder = TurnRecorder(conversation_id="c-1")
    tool = MultimodalSearchTool(recorder=recorder)
    high = [{"id": "img", "score": 0.88, "metadata": {"modality": "image"}}]
    low = [{"id": "txt", "score": 0.06, "metadata": {"modality": "text"}}]

    monkeypatch.setattr(crew_tools, "multimodal_rag", StubRAG(high))
    tool._run(query="看这张拓扑图")
    assert recorder.top_score == 0.88

    monkeypatch.setattr(crew_tools, "multimodal_rag", StubRAG(low))
    tool._run(query="VPN连接错误码619处理建议")
    assert recorder.top_score == 0.88, "低分检索不得覆盖高分命中"
    assert recorder.retrieved[0]["id"] == "img"


def test_search_tool_overwrites_on_higher_score(monkeypatch):
    """更高分检索仍应正常覆盖，避免「保留最优」把后续有效检索挡掉。"""
    import core.ai.crew_tools as crew_tools
    from core.ai.crew_tools import MultimodalSearchTool

    recorder = TurnRecorder(conversation_id="c-1")
    tool = MultimodalSearchTool(recorder=recorder)

    monkeypatch.setattr(
        crew_tools, "multimodal_rag", StubRAG([{"id": "a", "score": 0.30, "metadata": {}}])
    )
    tool._run(query="报销系统打不开")

    monkeypatch.setattr(
        crew_tools, "multimodal_rag", StubRAG([{"id": "b", "score": 0.95, "metadata": {}}])
    )
    tool._run(query="报销系统发票上传失败")

    assert recorder.top_score == 0.95
    assert recorder.retrieved[0]["id"] == "b"


# ======================================================================
# 文本优先索引（《多模态 RAG 文本优先改造方案》）：描述进单一集合作主召回
# ======================================================================
@pytest.mark.asyncio
async def test_add_writes_image_chunk_once_as_text(rag, media_stub):
    """文本优先：一张图只写一条（描述向量），落到单一集合，asset_id 有效。"""
    service, store, _db = rag
    await service.add_document(
        str(ASSET_DIR / "images" / "network_topology.png"), document_id="doc-single"
    )

    assert store.count() == 1
    meta = store.list_by_document("doc-single")[0]["metadata"]
    assert meta["modality"] == "image"
    assert meta["asset_id"]
    assert not hasattr(service, "image_store"), "文本优先方案已无图像空间"


@pytest.mark.asyncio
async def test_caption_stored_in_metadata(rag, media_stub, monkeypatch):
    """入库期生成的配图描述写入 metadata["caption"]。"""
    service, store, _db = rag
    caption = "一张企业远程办公网络拓扑图，含 VPN 网关与分支节点"

    async def _caption(image_bytes: bytes) -> str:
        return caption

    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _caption)
    await service.add_document(
        str(ASSET_DIR / "images" / "network_topology.png"), document_id="doc-cap"
    )

    meta = store.list_by_document("doc-cap")[0]["metadata"]
    assert meta["caption"] == caption


@pytest.mark.asyncio
async def test_caption_fallback_keeps_ingestion(rag, media_stub):
    """描述为空（VLM 未配置 / 失败）时回退页正文，入库不中断。"""
    service, store, _db = rag
    await service.add_document(
        str(ASSET_DIR / "images" / "vpn_error_691.png"), document_id="doc-nocap"
    )

    assert store.count() == 1
    meta = store.list_by_document("doc-nocap")[0]["metadata"]
    assert meta["caption"] == ""


@pytest.mark.asyncio
async def test_text_query_hits_image_via_caption(rag, media_stub, monkeypatch):
    """缺陷 1 回归：文本查询能命中 image 块（配图描述已进文本空间）。"""
    service, _store, _db = rag
    caption = "企业远程办公网络拓扑图，展示 VPN 网关与分支机构链路"

    async def _caption(image_bytes: bytes) -> str:
        return caption

    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _caption)
    await service.add_document(
        str(ASSET_DIR / "images" / "network_topology.png"), document_id="doc-hit"
    )

    hits = await service.search(query=caption)

    assert hits
    assert hits[0]["metadata"]["modality"] == "image"
    assert hits[0]["metadata"]["source"] == "network_topology.png"


@pytest.mark.asyncio
async def test_image_query_returns_text_space_hits(monkeypatch):
    """图片查询：候选全部来自单一文本集合，按描述召回后取前 N，无跨空间合并。"""
    img_hit = {
        "id": "a1", "score": 0.88,
        "metadata": {"modality": "image", "asset_id": "kb-1", "source": "t.png", "page": 1},
    }
    txt_hit = {
        "id": "c3", "score": 0.70,
        "metadata": {"modality": "text", "source": "vpn.md", "page": 3},
    }

    async def _caption(image_bytes: bytes) -> str:
        return "网络拓扑图"

    monkeypatch.setattr("core.rag.multimodal_rag.describe_knowledge_image", _caption)
    service = MultimodalRAGService(
        embedder=FakeEmbedder(),
        reranker=DisabledReranker(),
        vector_store=StubVectorStore([img_hit, txt_hit]),
    )

    hits = await service.search(query_image=b"topology")

    assert [h["id"] for h in hits] == ["a1", "c3"]


@pytest.mark.asyncio
async def test_delete_removes_image_chunk(rag, media_stub):
    """删除文档时，图片块（其描述向量）一并清理。"""
    service, store, _db = rag
    await service.add_document(
        str(ASSET_DIR / "images" / "mail_config.png"), document_id="doc-del"
    )
    assert store.count() == 1

    await service.delete_document("doc-del")

    assert store.count() == 0


def test_reranker_candidate_text_prefers_caption():
    """精排候选文本对 image 块优先取配图描述，无描述时回退页正文。"""
    from core.rag.embedding import RerankerClient

    with_caption = {"metadata": {"modality": "image", "caption": "配图描述", "context": "页正文"}}
    without = {"metadata": {"modality": "image", "context": "页正文"}}

    assert RerankerClient._candidate_text(with_caption) == "配图描述"
    assert RerankerClient._candidate_text(without) == "页正文"


def test_format_results_prefers_caption(rag):
    """拼装上下文时，image 块优先展示配图描述而非页正文。"""
    service, _store, _db = rag
    results = [
        {
            "id": "c", "score": 0.7,
            "metadata": {
                "modality": "image", "source": "s.png", "page": 1,
                "asset_id": "kb-1", "caption": "拓扑图描述", "context": "页正文",
            },
        }
    ]
    assert "拓扑图描述" in service.format_results(results)
