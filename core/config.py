"""统一配置加载（能力四元组），见设计文档第十七章。

命名规范：{能力}_PROVIDER / {能力}_BASE_URL / {能力}_MODEL / {能力}_API_KEY
全部走 OpenAI 兼容协议，替换供应商只改变量、不改代码。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 固定用「仓库根」的绝对路径加载。
# 若写成相对路径 ".env"，会随进程工作目录（CWD）变化：在非仓库根目录启动的
# Jupyter 内核 / 服务可能读不到本项目 .env（配置静默退回默认值），甚至误读其它
# 目录下的同名文件（例如带行内注释的旧副本 → 解析报 ValidationError）。
# 用绝对路径可让「读的是哪个 .env」不再依赖 CWD。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ============ 大模型（Agent 对话 / 回复生成；生成输入为文本）============
    LLM_PROVIDER: str = "openai_compatible"
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""
    LLM_API_KEY: str = ""

    # ============ 多模态嵌入（文本优先：文本/表格/图片配图描述统一进文本空间）============
    EMBEDDING_PROVIDER: str = "openai_compatible"
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_MODEL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_DIM: int = 1024
    EMBEDDING_MAX_IMAGE_SIDE: int = 1024
    # 图片编码后字节上限：硅基流动视觉接口约 32KB 上限，超出直接 400
    EMBEDDING_MAX_IMAGE_BYTES: int = 32000

    # ============ 检索精排（可关闭）============
    RERANKER_ENABLED: bool = True
    RERANKER_BASE_URL: str = ""
    RERANKER_MODEL: str = ""
    RERANKER_API_KEY: str = ""
    RERANKER_TOP_N: int = 3

    # ============ 视觉理解（截图语义）============
    VLM_BASE_URL: str = ""
    VLM_MODEL: str = ""
    VLM_API_KEY: str = ""

    # ============ 语音识别 / 合成 ============
    ASR_PROVIDER: str = "api"
    ASR_BASE_URL: str = ""
    ASR_MODEL: str = ""
    ASR_API_KEY: str = ""
    TTS_PROVIDER: str = "edge"
    TTS_VOICE: str = "zh-CN-XiaoxiaoNeural"

    # ============ OCR ============
    OCR_PROVIDER: str = "local"
    OCR_BASE_URL: str = ""
    OCR_MODEL: str = ""
    OCR_API_KEY: str = ""

    # ============ 供应商默认 Key ============
    SILICONFLOW_API_KEY: str = ""

    # ============ 检索参数 ============
    RETRIEVAL_TOP_K: int = 10
    # 检索准入阈值：**当前未接入检索流程**（search() 不据此过滤），仅作预留配置。
    # 若日后启用，须作用于**精排分**并按其实测分布校准（Qwen3-VL-Reranker-8B 下
    # 无关提问 top1≈0.001~0.004、相关提问 top1≈0.023~0.887）。详见设计文档 7.4。
    RETRIEVAL_MIN_SCORE: float = 0.0

    # ============ 向量库（嵌入式）============
    CHROMA_PATH: str = "./data/chroma"
    CHROMA_COLLECTION: str = "mde_knowledge"

    # ============ 关系数据库 ============
    DB_PATH: str = "./data/app.db"

    # ============ 媒体存储 ============
    MEDIA_DIR: str = "./data/media"

    # ============ 认证 ============
    TOKEN_EXPIRE_HOURS: int = 8

    # ============ 转人工护栏阈值（见 6.3）============
    # 按所用重排模型的实测分数分布校准（本机 Qwen3-VL-Reranker-8B：命中问答 0.20~0.70、
    # 知识库未覆盖的 IT 问题 0.03~0.06、寒暄/闲聊 <=0.019，取 0.15）
    HANDOFF_MIN_CONFIDENCE: float = 0.15
    HANDOFF_NEGATIVE_ROUNDS: int = 2
    HANDOFF_UNRESOLVED_ROUNDS: int = 3

    # ============ 知识库约束（见 7.6）============
    KB_MAX_FILE_MB: int = 10
    KB_MAX_DOCUMENTS: int = 50
    # 入库时为每张配图生成文本描述（写入文本空间作主召回）；VLM 未配置时自动跳过、回退页正文
    KB_IMAGE_CAPTION_ENABLED: bool = True

    # ============ CrewAI（见 4.1 / 4.2）============
    # 多 Agent 编排总开关：
    #   false（默认）→ 中台直连 LLM **流式生成**，逐字输出、首字快（见 7.7 / 10.1）；
    #   true         → 走 CrewAI 一线/工单/质检编排，Agent 自主调用检索/截图/工单工具，
    #                 回复同样**逐字流式**（`Crew(stream=True)` → `CrewStreamingOutput`）。
    # 注：CrewAI 本身支持流式；本项目额外打一个 provider 补丁，是供应商侧不兼容所致——
    # 硅基流动在流式响应中给**每个 chunk** 都回传 usage，命中 CrewAI 流式解析里
    # 「带 usage 即跳过」的分支会让正文被整段丢弃（补丁见 crew_agents.py 模块说明）。
    CREW_ENABLED: bool = True
    CREW_VERBOSE: bool = True
    CREW_MAX_ITER: int = 5
    CREW_MAX_RPM: int = 10
    # 质检 Agent 为旁路复核（不改写回复），默认关闭以省掉每轮一次完整 LLM 往返；
    # 需要演示质检链路时置 true 开启
    CREW_QA_ENABLED: bool = False

    # ============ 全栈评测（admin「评测」页，见第十七章）============
    # 小样本限流：每个套件最多跑 EVAL_MAX_CASES 条，控制外部 API 成本
    EVAL_MAX_CASES: int = 20
    EVAL_CONCURRENCY: int = 2
    # 各维度 PASS/FAIL 阈值。默认值先给经验值，跑通后按实测分布校准
    # （ASR/TTS 的 CER 越低越好，其余越高越好）
    EVAL_THRESHOLD_ASR_CER: float = 0.15
    EVAL_THRESHOLD_TTS_CER: float = 0.15
    EVAL_THRESHOLD_VLM_FIELD_ACC: float = 0.67
    EVAL_THRESHOLD_CAPTION_KEYPOINT: float = 0.60
    EVAL_THRESHOLD_RETRIEVAL_RECALL: float = 0.70

    # ============ MCP Server 启动参数（见 4.3 / 5.4）============
    MCP_SERVER_COMMAND: str = ""
    MCP_SERVER_SCRIPT: str = "mcp_servers/it_service/server.py"
    TICKETS_DB_PATH: str = ""

    # ------------------------------------------------------------------
    # Key 回退：{能力}_API_KEY 留空时继承 SILICONFLOW_API_KEY
    # 优先级：{能力}_API_KEY > SILICONFLOW_API_KEY
    # ------------------------------------------------------------------
    def _resolve(self, capability_key: str) -> str:
        return capability_key or self.SILICONFLOW_API_KEY

    @property
    def llm_key(self) -> str:
        return self._resolve(self.LLM_API_KEY)

    @property
    def embedding_key(self) -> str:
        return self._resolve(self.EMBEDDING_API_KEY)

    @property
    def reranker_key(self) -> str:
        return self._resolve(self.RERANKER_API_KEY)

    @property
    def vlm_key(self) -> str:
        return self._resolve(self.VLM_API_KEY) or self.llm_key

    @property
    def asr_key(self) -> str:
        return self._resolve(self.ASR_API_KEY)

    def media_dir(self) -> Path:
        return Path(self.MEDIA_DIR)

    def chroma_path(self) -> Path:
        return Path(self.CHROMA_PATH)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# ----------------------------------------------------------------------
# 启动自检（fail-fast）：必填项缺失时定位到「哪个能力、哪个变量」
# ----------------------------------------------------------------------
_REQUIRED: list[tuple[str, str]] = [
    ("LLM", "LLM_BASE_URL"),
    ("LLM", "LLM_MODEL"),
    ("EMBEDDING", "EMBEDDING_BASE_URL"),
    ("EMBEDDING", "EMBEDDING_MODEL"),
]


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def validate_config(check_embedding: bool = True) -> list[str]:
    """校验必填配置。返回告警列表；致命缺失抛 ConfigError。"""
    missing: list[str] = []
    for capability, var in _REQUIRED:
        if not getattr(settings, var):
            missing.append(f"[{capability}] {var}")
    if not settings.llm_key:
        missing.append("[LLM] LLM_API_KEY / SILICONFLOW_API_KEY（至少配置一个）")
    if missing:
        raise ConfigError(
            "配置缺失，请检查 .env：\n  - " + "\n  - ".join(missing)
        )

    warnings: list[str] = []
    if not settings.RERANKER_ENABLED:
        warnings.append("RERANKER_ENABLED=false，检索将直接取向量召回前 N 条。")
    if check_embedding:
        warnings.extend(_check_embedding_dim())
    return warnings


def _check_embedding_dim() -> list[str]:
    """试调一次 embedding，校验返回维度 == EMBEDDING_DIM（见 17.3 要点 2）。

    复用 ``EmbeddingClient`` 的调用路径，确保自检口径与业务实际一致
    （含 dimensions 截断与供应商不支持的降级行为）。
    """
    try:
        from core.rag.embedding import EmbeddingClient

        dim = EmbeddingClient().probe_dim()
        if dim != settings.EMBEDDING_DIM:
            raise ConfigError(
                f"嵌入维度不匹配：模型返回 {dim}，配置 EMBEDDING_DIM={settings.EMBEDDING_DIM}。"
                f"请修正 EMBEDDING_DIM 并重建索引。"
            )
    except ConfigError:
        raise
    except Exception as exc:  # 网络等原因不阻塞启动，仅告警
        return [f"嵌入维度自检未完成（网络或模型不可用）：{exc}"]
    return []
