"""评测编排（由 admin 接口在后台任务中调用，**不提供命令行入口**）。

生命周期：
    create_run(suites)  建记录（status=running）→ 返回 run_id，接口立即回给前端；
    execute(run_id)     后台逐套件执行，边跑边落盘（前端轮询进度）；
    finalize            收尾（status=done / failed）并写入汇总。
"""
from __future__ import annotations

import asyncio
import logging

from core.config import settings
from eval.store import load_run, new_run_id, now_iso, save_run
from eval.suites import SUITES, normalize

logger = logging.getLogger(__name__)


def _config_snapshot() -> dict:
    """记录本次评测的模型与阈值，保证结果可复现、可溯源。

    返回：
        快照 dict：各模态模型名、检索参数、样本上限与并发、以及各套件阈值。
        历史报告带上它，才能解释「为什么这次的分数和上次不一样」。
    """
    return {
        "asr_provider": settings.ASR_PROVIDER,
        "asr_model": settings.ASR_MODEL,
        "tts_provider": settings.TTS_PROVIDER,
        "tts_voice": settings.TTS_VOICE,
        "vlm_model": settings.VLM_MODEL,
        "embedding_model": settings.EMBEDDING_MODEL,
        "reranker_model": settings.RERANKER_MODEL,
        "retrieval_top_k": settings.RETRIEVAL_TOP_K,
        "reranker_top_n": settings.RERANKER_TOP_N,
        "max_cases": settings.EVAL_MAX_CASES,
        "concurrency": settings.EVAL_CONCURRENCY,
        "thresholds": {
            "asr_cer": settings.EVAL_THRESHOLD_ASR_CER,
            "tts_cer": settings.EVAL_THRESHOLD_TTS_CER,
            "vlm_field_acc": settings.EVAL_THRESHOLD_VLM_FIELD_ACC,
            "caption_keypoint": settings.EVAL_THRESHOLD_CAPTION_KEYPOINT,
            "retrieval_recall": settings.EVAL_THRESHOLD_RETRIEVAL_RECALL,
        },
    }


def create_run(suites: list[str] | None) -> dict:
    """创建一次评测记录并落盘，返回运行记录（含 id）。

    参数：
        suites：要跑的套件名列表；None/空表示全部（经 normalize 校验并排序）。
    返回：
        新建的 run 记录 dict，``status="running"``。先落盘再返回，接口据此立即
        把 run_id 回给前端，真正的执行交给后台 ``execute(run_id)``。
    """
    ordered = normalize(suites)  # 校验合法套件名并按默认顺序排序
    run = {
        "id": new_run_id(),
        "status": "running",
        "suites": ordered,
        "started_at": now_iso(),
        "finished_at": "",
        "progress": {"done": 0, "total": len(ordered), "current": ""},
        "config": _config_snapshot(),
        "results": {},
        "notes": [],
        "error": "",
    }
    save_run(run)
    return run


async def execute(run_id: str) -> None:
    """后台执行：逐套件运行并持续落盘，最终置为 done / failed。

    参数：
        run_id：``create_run`` 生成的运行 id。
    返回：
        无（结果全部落盘，前端轮询读取）。
    说明：
        每跑完一个套件就 save_run 一次，前端轮询即可看到进度；单个套件抛异常
        只把该套件记为 FAIL，不影响其余套件继续跑。
    """
    run = load_run(run_id)
    if not run:
        logger.error("评测运行不存在：%s", run_id)
        return

    try:
        for name in run.get("suites", []):
            module = SUITES.get(name)
            run["progress"]["current"] = name  # 标记当前正在跑哪个套件
            save_run(run)
            if module is None:
                continue  # 未注册的套件名直接跳过
            try:
                run["results"][name] = await module.run()
            except Exception as exc:  # noqa: BLE001
                # 单套件失败降级为 FAIL，不连累其他套件
                logger.exception("评测套件 %s 执行失败", name)
                run["results"][name] = {
                    "suite": name,
                    "title": getattr(module, "TITLE", name),
                    "sample_count": 0,
                    "metrics": {},
                    "items": [],
                    "verdict": "FAIL",
                    "notes": [f"套件执行异常：{exc}"],
                }
            run["progress"]["done"] += 1
            save_run(run)  # 每跑完一个套件立即落盘，供前端轮询进度

        run["status"] = "done"
    except asyncio.CancelledError:
        # 任务被取消（应用关闭/重启）时 Exception 分支不会命中，
        # 必须显式收尾，否则记录会永久停在 running 且无法删除。
        logger.warning("评测运行 %s 被取消，标记为 failed", run_id)
        run["status"] = "failed"
        run["error"] = "任务被取消（服务重启或主动中断）"
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("评测运行 %s 失败", run_id)
        run["status"] = "failed"
        run["error"] = str(exc)
    finally:
        # 无论成功、失败还是取消，都在此统一收尾并落盘
        run["finished_at"] = now_iso()
        run["progress"]["current"] = ""
        save_run(run)
