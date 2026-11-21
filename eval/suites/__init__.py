"""评测套件注册表：套件名 → 模块（模块需暴露 SUITE / TITLE / async run()）。

这里相当于「插件目录」：runner 只认这三个约定接口，不关心具体套件内部实现，
新增套件时只需写一个同构模块并登记到 SUITES 即可。
"""
from __future__ import annotations

from eval.suites import asr, retrieval, tts, vlm

# 套件名 → 模块。键即 API 里使用的套件标识
SUITES = {
    asr.SUITE: asr,
    tts.SUITE: tts,
    vlm.SUITE: vlm,
    retrieval.SUITE: retrieval,
}

# 默认执行顺序：语音输入 → 语音输出 → 图文理解 → 检索
DEFAULT_ORDER: tuple[str, ...] = (asr.SUITE, tts.SUITE, vlm.SUITE, retrieval.SUITE)

# 套件名 → 展示标题（从各模块的 TITLE 汇总，供前端列表显示）
TITLES = {name: module.TITLE for name, module in SUITES.items()}


def normalize(names: list[str] | None) -> list[str]:
    """校验并归一化套件名列表；为空时返回默认全集。

    参数：
        names：调用方传入的套件名列表；None 或空表示「全部」。
    返回：
        按 DEFAULT_ORDER 排好序的子集列表。空传入返回默认全集。
    异常：
        ValueError：出现未知套件名时报错，避免静默跑空。
    """
    if not names:
        return list(DEFAULT_ORDER)
    # 按默认顺序取交集：既过滤掉非法名，又保证执行顺序稳定
    ordered = [n for n in DEFAULT_ORDER if n in set(names)]
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise ValueError(f"未知评测套件：{', '.join(unknown)}")
    # 传入的名字都合法但都不在默认顺序里（理论上不会发生）时兜底为全集
    return ordered or list(DEFAULT_ORDER)
