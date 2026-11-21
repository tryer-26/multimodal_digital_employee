"""TTS 评测套件：语音合成质量（纯自动客观指标）。

TTS 没有唯一「正确音频」，故用三类**可自动测量**的代理：

1. 反向 ASR CER —— 合成音频再喂 ASR 转写，与原文比对（**可懂度**代理）；
2. 副语言指标 —— 合成成功率、时延、时长/语速（异常偏快 ⇒ 疑似漏读/截断）；
3. 时长一致性 —— 音频时长与文本长度的皮尔逊相关（应接近 1）。

已知局限：反向 CER 与 ASR 同源偏差（本项目 TTS 为 edge-tts、ASR 为 api，异厂商，
偏差较小）；「自然度」需人工 MOS，本套件不覆盖。情感语调控制当前未实装，故不评。

指标与判定：
  - ``round_trip_cer``（主指标）：反向可懂度字错率，**越低越好**，阈值
    ``EVAL_THRESHOLD_TTS_CER``（0.15）。
  - ``success_rate`` 合成成功率；``latency_p50/p95_ms`` 合成时延；
    ``mean_chars_per_sec`` 平均语速；``duration_len_corr`` 时长与文本长度的皮尔逊
    相关（正常应接近 1）；``truncation_rate`` 语速异常偏快（疑似漏读/截断）的比例。
  - 注意 CER / WER 同源：都是「编辑距离 / 参考长度」，CER 按字、WER 按词，越低越好。
前置条件：需 TTS 合成与 ASR 转写服务可用；不依赖知识库索引。
"""
from __future__ import annotations

import time

from core.config import settings
from core.rag.multimodal import process_voice, synthesize_speech
from eval import metrics
from eval.suites._common import FAIL_PREFIX, build_result, load_cases, map_limited

SUITE = "tts"
TITLE = "语音合成（TTS）可懂度与副语言"

# MP3 帧头解析（纯 Python，免 ffmpeg 依赖）：按首帧的比特率/采样率估算总时长。
_BITRATE_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BITRATE_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SAMPLE_RATE = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
_SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}

# 中文正常语速约 4~5 字/秒；超过 8 字/秒视为异常偏快（疑似漏读）
_MAX_REASONABLE_CPS = 8.0


def _mp3_duration_sec(data: bytes) -> float:
    """估算 MP3 时长（秒）。逐帧按「每帧样本数 / 采样率」累加。

    参数：
        data：MP3 文件字节。
    返回：
        估算时长（秒）；空数据或解析不到有效帧时返回 0.0。
    说明：
        纯 Python 解析 MP3 帧头，避免引入 ffmpeg 依赖：先跳过 ID3v2 标签，再逐帧
        读版本/层/比特率/采样率，算出每帧时长并累加。
    """
    if not data:
        return 0.0
    index = 0
    size = len(data)
    if data[:3] == b"ID3":  # 跳过 ID3v2 标签，否则会把标签字节误当音频帧
        tag_size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        index = 10 + tag_size

    total = 0.0
    while index + 4 <= size:
        # 同步字 0xFFE：非帧头则右移一字节继续找
        if data[index] != 0xFF or (data[index + 1] & 0xE0) != 0xE0:
            index += 1
            continue
        version = (data[index + 1] >> 3) & 0x03   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (data[index + 1] >> 1) & 0x03     # 1=Layer III
        bitrate_index = (data[index + 2] >> 4) & 0x0F
        rate_index = (data[index + 2] >> 2) & 0x03
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
            break  # 保留字/不支持的类型，停止解析
        table = _BITRATE_V1_L3 if version == 3 else _BITRATE_V2_L3
        bitrate = table[bitrate_index] * 1000
        sample_rate = _SAMPLE_RATE[version][rate_index]
        samples = _SAMPLES_PER_FRAME[version]
        frame_len = int(samples / 8 * bitrate / sample_rate)  # 本帧字节数
        if frame_len <= 0:
            break
        total += samples / sample_rate  # 累加本帧时长
        index += frame_len
    return total


async def _one(case: dict) -> dict:
    """跑单条 TTS 用例：合成 → 量时长/语速 → 反向 ASR 转写算 CER。

    参数：
        case：一条 jsonl 用例，含 ``id`` / ``text`` / ``voice``（可选音色）。
    返回：
        明细 dict：``duration_sec`` 音频时长、``chars_per_sec`` 语速、``hypothesis``
        反向转写文本、``round_trip_cer`` 反向字错率（未测为 None）等。
    异常：
        不向外抛异常。合成失败置 ``ok=False``；反向 ASR 失败只把 hypothesis 标为
        FAIL_PREFIX 并让 cer 留 None（不误判为可懂度差）。
    """
    voice = case.get("voice") or settings.TTS_VOICE  # 用例未指定音色则用全局默认
    text = case.get("text", "")
    started = time.perf_counter()  # 计时起点
    try:
        audio, ext = await synthesize_speech(text, voice)
    except Exception as exc:  # noqa: BLE001
        # 合成失败：没有任何音频可测，直接标记失败
        return {
            "id": case.get("id"), "text": text, "voice": voice,
            "ok": False, "duration_sec": 0.0, "chars_per_sec": 0.0,
            "hypothesis": "", "round_trip_cer": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"合成失败：{exc}",
        }
    latency_ms = round((time.perf_counter() - started) * 1000, 1)

    # 仅 MP3 走轻量帧头解析估时长；其他格式暂不测时长（记 0）
    duration = _mp3_duration_sec(audio) if ext == "mp3" else 0.0
    norm_len = len(metrics.normalize_text(text))  # 归一化后字数，剔除标点/空白干扰
    chars_per_sec = round(norm_len / duration, 2) if duration > 0 else 0.0

    hypothesis = ""
    cer = None
    try:
        hypothesis = await process_voice(audio, filename=f"{case.get('id')}.{ext}")
        if not hypothesis.startswith(FAIL_PREFIX):
            # 反向 CER：合成音频 → ASR 转写 → 与原文比，作为「可懂度」代理
            cer = metrics.char_error_rate(text, hypothesis)
    except Exception as exc:  # noqa: BLE001
        hypothesis = f"{FAIL_PREFIX}：{exc}"

    return {
        "id": case.get("id"), "text": text, "voice": voice,
        "ok": True, "duration_sec": round(duration, 3),
        "chars_per_sec": chars_per_sec, "hypothesis": hypothesis,
        "round_trip_cer": None if cer is None else round(cer, 4),
        "latency_ms": latency_ms, "error": "",
    }


async def run(limit: int | None = None) -> dict:
    """执行 TTS 套件：逐条合成评测并聚合指标。

    参数：
        limit：最多评测条数；None 时用 ``EVAL_MAX_CASES``。
    返回：
        统一结果 dict。主指标 ``round_trip_cer`` 越低越好，对照
        ``EVAL_THRESHOLD_TTS_CER`` 判 PASS/FAIL。
    """
    cases = load_cases("speech_cases.jsonl", limit)
    if not cases:
        return build_result(SUITE, TITLE, {}, [], notes=["无可用用例（datasets/speech_cases.jsonl 为空）"])

    items = await map_limited(cases, _one)  # 受 EVAL_CONCURRENCY 限流地并发执行

    ok_items = [it for it in items if it.get("ok")]  # 合成成功者才参与音频指标
    pairs = [(it["text"], it["hypothesis"]) for it in ok_items if it.get("round_trip_cer") is not None]
    latencies = [it["latency_ms"] for it in items if it.get("latency_ms")]
    durations = [it["duration_sec"] for it in ok_items if it["duration_sec"] > 0]
    text_lens = [len(metrics.normalize_text(it["text"])) for it in ok_items if it["duration_sec"] > 0]
    cps = [it["chars_per_sec"] for it in ok_items if it["chars_per_sec"] > 0]

    metrics_map = {
        "success_rate": round(len(ok_items) / len(items), 4) if items else None,  # 合成成功率
        "round_trip_cer": round(metrics.corpus_cer(pairs), 4) if pairs else None,  # 反向可懂度
        "latency_p50_ms": round(metrics.percentile(latencies, 0.5), 1),  # 时延中位数
        "latency_p95_ms": round(metrics.percentile(latencies, 0.95), 1),  # 时延 P95
        "mean_chars_per_sec": round(metrics.mean(cps), 2) if cps else None,  # 平均语速
        # 时长 vs 文本长度应强相关（≈1）；偏低暗示漏读/截断
        "duration_len_corr": round(metrics.pearson(text_lens, durations), 4) if len(durations) > 1 else None,
        # 语速超过 _MAX_REASONABLE_CPS 视为异常偏快（疑似漏读），统计其占比
        "truncation_rate": round(
            sum(1 for c in cps if c > _MAX_REASONABLE_CPS) / len(cps), 4
        ) if cps else None,
        "measured": len(pairs),  # 实际参与反向 CER 的样本数
    }

    return build_result(
        SUITE,
        TITLE,
        metrics_map,
        items,
        primary="round_trip_cer",
        primary_threshold=settings.EVAL_THRESHOLD_TTS_CER,
        lower_is_better=True,
    )
