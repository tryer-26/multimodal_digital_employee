"""评测套件公共工具：用例加载、并发限流、阈值判定、结果装配。

四个套件（asr / tts / vlm / retrieval）都走同一条流水线：
「读用例 → 逐条执行 → 聚合指标 → 判定 PASS/FAIL/SKIP → 装配结果」。
本模块把这些共性抽出来，避免各套件重复造轮子。

关键概念：
  - **用例（case）**：一行 JSON，来自 ``eval/datasets/*.jsonl``。jsonl 即
    「每行一个独立 JSON 对象」，好处是能逐行流式解析、追加不破坏全文，
    且某一行坏了只丢这一行、不影响其余用例。
  - **skip 与 fail 的区别**：SKIP 表示「没法测」（依赖缺失、指标全为空），
    不算误报；FAIL 表示「确实测了但不达标」。两者必须区分，否则把环境问题
    当成质量问题，会误伤被测能力。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# 数据集目录：本文件位于 eval/suites/ ，上溯两级即 eval/，其下 datasets/*.jsonl
DATASETS_DIR = Path(__file__).resolve().parent.parent / "datasets"

# ASR 调用失败时以该前缀标记 hypothesis（ASR 套件与 TTS 回环共用），据此判定「未测」
FAIL_PREFIX = "语音识别失败"


def load_cases(filename: str, limit: Optional[int] = None) -> list[dict]:
    """读取 jsonl 用例（跳过空行），按 limit 截断。

    参数：
        filename：数据集文件名，如 ``speech_cases.jsonl``（相对 DATASETS_DIR）。
        limit：最多返回条数。传 None 时用配置上限 ``EVAL_MAX_CASES``（默认 20，
            是为控制外部 API 成本的小样本限流）；显式传 0 表示一条都不跑。
    返回：
        用例 dict 列表。文件不存在返回空列表；某行非法 JSON 则跳过该行并告警，
        不影响其余用例（这正是 jsonl 逐行解析的容错优势）。
    """
    path = DATASETS_DIR / filename
    if not path.exists():
        return []
    cases: list[dict] = []
    # 逐行解析：每行是一个独立的 JSON 对象
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:  # 跳过空行（文件末尾/分隔用）
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("用例解析失败，已跳过：%s", line[:80])
    # limit=None 用配置上限；显式传 0 表示一条都不跑（不能用真假值混同二者）
    size = settings.EVAL_MAX_CASES if limit is None else limit
    return cases[:size]


def verdict(value: Optional[float], threshold: float, lower_is_better: bool = False) -> str:
    """单指标 PASS/FAIL 判定；value 为 None（未测）时返回 SKIP。

    参数：
        value：实测指标值；None 表示该指标没有测到（如 ASR 用例全部调用失败）。
        threshold：阈值，来自 ``EVAL_THRESHOLD_*`` 配置（默认值是经验值，跑通后
            按实测分布校准）。
        lower_is_better：True 表示越小越好（如 CER），判定 ``value <= threshold``；
            False 表示越大越好（如准确率 / 召回率），判定 ``value >= threshold``。
    返回：
        "PASS" / "FAIL" / "SKIP" 之一。
    """
    if value is None:
        return "SKIP"  # 未测到不能判 FAIL，否则把环境问题误当质量不达标
    ok = value <= threshold if lower_is_better else value >= threshold
    return "PASS" if ok else "FAIL"


async def map_limited(
    items: list, worker: Callable[[dict], Awaitable[dict]], concurrency: Optional[int] = None
) -> list[dict]:
    """带并发上限的逐条执行；worker 自行处理异常并返回明细 dict。

    这是「异步并发 + 限流」的通用写法：用信号量（Semaphore）控制同时在跑的
    任务数，既能提速，又不会把外部 ASR/TTS/VLM 接口打爆或触发限流。

    参数：
        items：待处理用例列表。
        worker：异步处理函数 ``async (case) -> dict``；要求它内部 try/except
            自行兜底、绝不向外抛异常，否则整个 gather 会整体失败。
        concurrency：并发上限；None 时用 ``settings.EVAL_CONCURRENCY``（默认 2）。
    返回：
        与 items **顺序一致** 的明细 dict 列表（gather 按入参顺序返回结果）。
    """
    limit = concurrency or settings.EVAL_CONCURRENCY
    # 信号量：最多允许 limit 个任务同时进入 with 块
    sem = asyncio.Semaphore(max(1, limit))

    async def _wrap(item: dict) -> dict:
        # 每个任务先抢一个名额，跑完自动释放，从而平滑并发
        async with sem:
            return await worker(item)

    # gather 并发调度所有任务，返回顺序与输入一致
    return list(await asyncio.gather(*(_wrap(i) for i in items)))


def combine_verdict(criteria: list[tuple[str, float, bool]], metrics: dict) -> str:
    """多指标联合判定：(指标名, 阈值, 越低越好)。

    参数：
        criteria：判定项列表，每项形如 ``(指标名, 阈值, 越低越好)``。例如 VLM 同时
            检查「诊断字段准确率」和「描述关键点命中率」。
        metrics：指标名 → 实测值。
    返回：
        全部通过 = PASS；任一未过 = FAIL；全部未测 = SKIP。
        采用「一票否决」，避免用某个高分项掩盖另一项的短板。
    """
    results = [
        verdict(metrics.get(name), threshold, lower)
        for name, threshold, lower in criteria
    ]
    if all(r == "SKIP" for r in results):
        return "SKIP"
    return "FAIL" if "FAIL" in results else "PASS"


def build_result(
    suite: str,
    title: str,
    metrics: dict,
    items: list[dict],
    primary: Optional[str] = None,
    primary_threshold: float = 0.0,
    lower_is_better: bool = False,
    criteria: Optional[list[tuple[str, float, bool]]] = None,
    notes: Optional[list[str]] = None,
) -> dict:
    """装配套件结果（所有套件 return 的统一结构）。

    参数：
        suite / title：套件标识与展示标题。
        metrics：指标名 → 实测值（含 null 表示该指标未测）。
        items：逐条明细（前端报告页可展开查看每条）。
        primary / primary_threshold / lower_is_better：单指标判定（简单套件用）；
        criteria：多指标联合判定（如 VLM 同时看诊断准确率与描述命中率）。
        notes：备注（如跳过的套件及原因、样本局限说明）。
    返回：
        统一结果 dict，含 ``sample_count`` / ``metrics`` / ``items`` / ``criteria``
        / ``verdict``（PASS/FAIL/SKIP）/ ``notes``。runner 会把它整体落库。
    """
    if criteria:
        # 多指标：任一不达标则 FAIL，全未测则 SKIP
        verdict_text = combine_verdict(criteria, metrics)
    elif primary:
        # 单指标：直接对该主指标做判定
        verdict_text = verdict(metrics.get(primary), primary_threshold, lower_is_better)
    else:
        # 没有任何判定依据，只能给 SKIP
        verdict_text = "SKIP"
    return {
        "suite": suite,
        "title": title,
        "sample_count": len(items),
        "metrics": metrics,
        "items": items,
        "primary_metric": primary,
        "criteria": [
            {"metric": name, "threshold": threshold, "lower_is_better": lower}
            for name, threshold, lower in (criteria or [])
        ],
        "verdict": verdict_text,
        "notes": notes or [],
    }


def skipped(suite: str, title: str, reason: str) -> dict:
    """套件无法执行（如依赖知识库未播种）时的占位结果。

    与「失败」区分：这里返回 ``verdict="SKIP"``、``sample_count=0``，并把原因
    写进 ``notes``，报告页据此提示「请先做前置操作」而非报错。
    """
    return {
        "suite": suite,
        "title": title,
        "sample_count": 0,
        "metrics": {},
        "items": [],
        "primary_metric": None,
        "verdict": "SKIP",  # 明确标 SKIP，避免被误读为质量不达标
        "notes": [reason],
    }
