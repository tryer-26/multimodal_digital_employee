"""评测运行的持久化（文件式，零额外依赖）。

    data/eval/runs/{run_id}.json   单次评测的完整结果
    data/eval/baseline.json        基线快照（回归对比用）

一次 run 的结构::

    {
      "id": "eval-20261004-153000-a1b2",
      "status": "running | done | failed",
      "suites": ["asr", "tts", "vlm", "retrieval"],
      "started_at": "...", "finished_at": "...",
      "progress": {"done": 1, "total": 4, "current": "tts"},
      "config": {...},            # 模型/参数快照，保证可复现
      "results": {"asr": {...}},  # 各套件结果（含 metrics / items / verdict）
      "notes": ["..."],           # 跳过的套件及原因等
      "error": ""
    }
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# 目录/文件位置：runs 存每次评测，baseline 存基线快照
RUNS_DIR = Path("./data/eval/runs")
BASELINE_PATH = Path("./data/eval/baseline.json")

# run_id 白名单，避免路径穿越（run_id 会拼进文件名）
_ID_RE = re.compile(r"^eval-\d{8}-\d{6}-[0-9a-f]{4}$")


def now_iso() -> str:
    """返回当前本地时间的 ISO 字符串（精确到秒），用作时间戳。"""
    return datetime.now().isoformat(timespec="seconds")


def new_run_id() -> str:
    """生成运行 id：``eval-<日期>-<时分秒>-<4位随机hex>``。

    末尾随机后缀用于避免同一秒内多次创建时 id 冲突。
    """
    return f"eval-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def _run_path(run_id: str) -> Path:
    """由 run_id 解析出结果文件路径，并做白名单校验。

    参数：
        run_id：运行 id。
    返回：
        对应的 JSON 文件路径。
    异常：
        ValueError：run_id 不符合约定格式时抛出（防止路径穿越）。
    """
    if not _ID_RE.match(run_id or ""):
        raise ValueError(f"非法 run_id：{run_id}")
    return RUNS_DIR / f"{run_id}.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + 原子替换，避免读侧读到「写了一半」的 JSON。

    runner 边跑边落盘、前端每 2s 轮询，非原子写会偶发半截内容导致
    详情 404 或列表漏项。
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_run(run: dict) -> None:
    """把一次运行记录原子写入 ``data/eval/runs/{id}.json``。

    参数：
        run：完整 run dict（须含 id）。
    返回：
        无。
    异常：
        ValueError：run 的 id 非法时由 ``_run_path`` 抛出。
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = _run_path(run["id"])
    _atomic_write_text(path, json.dumps(run, ensure_ascii=False, indent=2))


def load_run(run_id: str) -> Optional[dict]:
    """读取一次运行记录。

    参数：
        run_id：运行 id。
    返回：
        run dict；id 非法、文件不存在或 JSON 损坏时返回 None（不让坏文件导致 500）。
    """
    try:
        path = _run_path(run_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(limit: int = 50) -> list[dict]:
    """历史运行摘要（按开始时间倒序）。

    参数：
        limit：最多返回条数（至少 1）。
    返回：
        摘要列表，每项含 id / status / suites / 时间 / summary（各套件判定与指标）。
        目录不存在时返回空列表。
    """
    if not RUNS_DIR.exists():
        return []
    items: list[dict] = []
    for path in RUNS_DIR.glob("eval-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 坏文件跳过，列表页不因此报错
        if not isinstance(data, dict):  # 脏文件（根非对象）直接跳过，避免 500
            continue
        items.append(
            {
                "id": data.get("id"),
                "status": data.get("status"),
                "suites": data.get("suites", []),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "summary": _summarize(data),
            }
        )
    items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return items[: max(1, limit)]


def _summarize(run: dict) -> dict:
    """汇总各套件的判定与核心指标，供列表页展示。"""
    out: dict = {}
    for suite, result in (run.get("results") or {}).items():
        out[suite] = {
            "verdict": result.get("verdict"),
            "metrics": result.get("metrics", {}),
        }
    return out


def delete_run(run_id: str) -> bool:
    """删除一次运行记录文件。

    参数：
        run_id：运行 id。
    返回：
        删除成功返回 True；id 非法或文件不存在返回 False。
    """
    try:
        path = _run_path(run_id)
    except ValueError:
        return False
    if path.exists():
        path.unlink()
        return True
    return False


def save_baseline(run: dict) -> None:
    """把某次 run 的各套件指标存为基线快照（供后续回归对比）。

    参数：
        run：完整 run dict；只提取各套件的 metrics，不保存逐条明细。
    返回：
        无。
    """
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run.get("id"),
        "saved_at": now_iso(),
        "results": {
            suite: result.get("metrics", {})
            for suite, result in (run.get("results") or {}).items()
        },
    }
    _atomic_write_text(
        BASELINE_PATH, json.dumps(payload, ensure_ascii=False, indent=2)
    )


def load_baseline() -> Optional[dict]:
    """读取基线快照。

    返回：
        基线 dict；不存在或内容损坏时返回 None。
    """
    if not BASELINE_PATH.exists():
        return None
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_baseline() -> None:
    """删除基线快照（当基线所指的 run 被删除时调用，避免基线悬空）。"""
    if BASELINE_PATH.exists():
        BASELINE_PATH.unlink()


def recover_stale_runs() -> int:
    """把遗留在 ``running`` 的记录标记为 ``failed``（进程重启后调用）。

    后台任务随进程被杀时不会走到收尾逻辑，run 会永久停在 running，
    而删除接口又禁止删 running——该记录将永远无法清理。启动时统一收敛。
    返回被恢复的记录数。
    """
    if not RUNS_DIR.exists():
        return 0
    recovered = 0
    for path in RUNS_DIR.glob("eval-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("status") != "running":
            continue
        data["status"] = "failed"
        data["finished_at"] = data.get("finished_at") or now_iso()
        data["error"] = data.get("error") or "服务重启，任务被中断"
        data["progress"] = {**(data.get("progress") or {}), "current": ""}
        save_run(data)
        recovered += 1
    return recovered
