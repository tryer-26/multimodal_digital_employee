"""ASR 评测套件：语音识别字错率。

金标来自 ``datasets/speech_cases.jsonl`` 的参考文本；音频**运行时用 TTS 现合成**
（TTS 自建主集，零人工录音）。对每条用例做「参考文本 → TTS 合成 → ASR 转写 →
算 CER」。因 TTS（edge-tts）与 ASR（api）为**异厂商**，回环 CER 可作为识别准确率
的合理代理；但仍是两者复合误差，报告需标注。

已知局限：自建音频无口音/噪声/语速变化，CER 属**乐观下界**。

指标与判定：
  - ``cer``（主指标）：语料级字错率 = Σ字级编辑距离 / Σ参考字数，见
    ``eval.metrics``。**CER 越低越好**，默认阈值 ``EVAL_THRESHOLD_ASR_CER``（0.15）。
  - ``sentence_accuracy``：整句完全正确的句子占比；``failure_rate``：调用失败率。
  - 编辑距离（Levenshtein）= 把假设文本改成参考文本所需的最少「替换 + 删除 + 插入」
    次数；除以参考长度即得错率，故为 0 表示完全正确，>1 表示插入过多。
前置条件：无需知识库索引，但 TTS 合成与 ASR 转写服务需可用。
"""
from __future__ import annotations

import time

from core.config import settings
from core.rag.multimodal import process_voice, synthesize_speech
from eval import metrics
from eval.suites._common import FAIL_PREFIX, build_result, load_cases, map_limited

SUITE = "asr"
TITLE = "语音识别（ASR）字错率"


async def _one(case: dict) -> dict:
    """跑单条 ASR 用例：TTS 合成 → ASR 转写 → 算 CER。

    参数：
        case：一条 jsonl 用例，含 ``id`` / ``text``（参考文本）/ ``voice``（可选音色）。
    返回：
        明细 dict，含 ``hypothesis``（ASR 转写结果）、``cer``（句级字错率，未测为
        None）、``latency_ms``（合成+转写总耗时）等。
    异常：
        不向外抛异常。合成失败时置 ``synth_failed=True`` 并返回（该条不计入 CER
        分母）；转写失败时把 ``hypothesis`` 置为 FAIL_PREFIX 前缀串，让上层识别为
        「未测」而非「识别错误」。
    """
    voice = case.get("voice") or settings.TTS_VOICE  # 用例未指定音色则用全局默认
    started = time.perf_counter()  # 计时起点，覆盖合成+转写全流程
    try:
        audio, ext = await synthesize_speech(case.get("text", ""), voice)
    except Exception as exc:  # noqa: BLE001
        # 合成失败 ⇒ 根本没有音频可测，单独标记 synth_failed，不计入 CER 分母
        return {
            "id": case.get("id"),
            "text": case.get("text"),
            "voice": voice,
            "ok": False,
            "synth_failed": True,
            "hypothesis": "",
            "cer": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"TTS 合成失败：{exc}",
        }

    try:
        hypothesis = await process_voice(audio, filename=f"{case.get('id')}.{ext}")
    except Exception as exc:  # noqa: BLE001
        # 转写失败用统一前缀标记，避免与真实识别文本混淆
        hypothesis = f"{FAIL_PREFIX}：{exc}"
    latency_ms = round((time.perf_counter() - started) * 1000, 1)

    failed = hypothesis.startswith(FAIL_PREFIX)
    # 失败条目不参与 CER 计算（cer=None），否则「未测」会被当成识别全错
    cer = None if failed else metrics.char_error_rate(case.get("text", ""), hypothesis)
    return {
        "id": case.get("id"),
        "text": case.get("text"),
        "voice": voice,
        "ok": not failed,
        "synth_failed": False,
        "hypothesis": hypothesis,
        "cer": None if cer is None else round(cer, 4),
        "latency_ms": latency_ms,
        "error": "" if not failed else hypothesis,
    }


async def run(limit: int | None = None) -> dict:
    """执行 ASR 套件：逐条评测并聚合指标。

    参数：
        limit：最多评测条数；None 时用 ``EVAL_MAX_CASES``（供小样本调试）。
    返回：
        统一结果 dict（结构见 ``build_result``）。主指标 ``cer`` 越低越好，对照
        ``EVAL_THRESHOLD_ASR_CER`` 判 PASS/FAIL。
    """
    cases = load_cases("speech_cases.jsonl", limit)
    if not cases:
        # 无用例直接给空结果，避免出现 0 分母
        return build_result(SUITE, TITLE, {}, [], notes=["无可用用例（datasets/speech_cases.jsonl 为空）"])

    items = await map_limited(cases, _one)  # 受 EVAL_CONCURRENCY 限流地并发执行

    # pairs 只取算得出 CER 的条目，供语料级 CER / 句准率使用
    pairs = [
        (it["text"], it["hypothesis"]) for it in items if it.get("cer") is not None
    ]
    measured = [it for it in items if it.get("cer") is not None]
    latencies = [it["latency_ms"] for it in items if it.get("latency_ms")]

    metrics_map = {
        # 语料级 CER = Σ字级编辑距离 / Σ参考字数：长句权重更高，比句级平均更稳健
        "cer": round(metrics.corpus_cer(pairs), 4) if pairs else None,
        # 句准率：归一化后整句全对（即 CER=0）的句子占比
        "sentence_accuracy": round(metrics.sentence_accuracy(pairs), 4) if pairs else None,
        # 失败率：真正跑了却失败的占比（分母排除合成失败者）
        "failure_rate": round(
            sum(1 for it in items if not it.get("synth_failed") and not it.get("ok"))
            / max(1, sum(1 for it in items if not it.get("synth_failed"))),
            4,
        ),
        "synth_failed": sum(1 for it in items if it.get("synth_failed")),
        "latency_p50_ms": round(metrics.percentile(latencies, 0.5), 1),  # 时延中位数
        "latency_p95_ms": round(metrics.percentile(latencies, 0.95), 1),  # 时延 P95
        "measured": len(measured),  # 实际参与评分的样本数
    }

    return build_result(
        SUITE,
        TITLE,
        metrics_map,
        items,
        primary="cer",
        primary_threshold=settings.EVAL_THRESHOLD_ASR_CER,
        lower_is_better=True,  # CER 越低越好
    )
