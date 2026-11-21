"""评测指标库的离线单测（不触发任何外部调用）。

覆盖：文本归一化 / 错率、检索排序指标、结构化字段、描述质量、统计工具。
"""
from __future__ import annotations

import math

import pytest

from eval import metrics


# ----------------------------------------------------------------------
# 文本归一化
# ----------------------------------------------------------------------
def test_normalize_strips_punct_space_case():
    assert metrics.normalize_text("Hello, World!") == "helloworld"
    assert metrics.normalize_text("错误 619。") == "错误619"


def test_normalize_fullwidth_and_cjk_digits():
    # NFKC 折全角；中文数字整体转阿拉伯
    assert metrics.normalize_text("ＶＰＮ") == "vpn"
    assert metrics.normalize_text("六幺九") == "619"
    assert metrics.normalize_text("六百一十九") == "619"
    assert metrics.normalize_text("十九") == "19"
    assert metrics.normalize_text("一百二十") == "120"
    # 小数点写法归一：口语「三点二」与书面「3.2」必须等价
    assert metrics.normalize_text("零点五") == metrics.normalize_text("0.5")
    assert metrics.normalize_text("三点二") == metrics.normalize_text("3.2")
    # 非小数的「点」不受影响
    assert metrics.normalize_text("重点") == "重点"


def test_normalize_keeps_mixed_numbers():
    assert metrics.normalize_text("0x80070005") == "0x80070005"
    assert metrics.normalize_text("端口 993") == "端口993"


# ----------------------------------------------------------------------
# 编辑距离与错率
# ----------------------------------------------------------------------
def test_edit_distance_basic():
    assert metrics.edit_distance("abc", "abc") == 0
    assert metrics.edit_distance("abc", "abd") == 1   # 替换
    assert metrics.edit_distance("abc", "ab") == 1    # 删除
    assert metrics.edit_distance("abc", "abcd") == 1  # 插入
    assert metrics.edit_distance("", "ab") == 2


def test_char_error_rate():
    assert metrics.char_error_rate("你好世界", "你好世界") == 0.0
    assert metrics.char_error_rate("你好世界", "你好") == pytest.approx(0.5)
    assert metrics.char_error_rate("619", "六幺九") == 0.0  # 归一化后一致


def test_corpus_cer_and_sentence_accuracy():
    pairs = [("你好世界", "你好世界"), ("错误619", "错误619")]
    assert metrics.corpus_cer(pairs) == 0.0
    assert metrics.sentence_accuracy(pairs) == 1.0

    pairs2 = [("你好世界", "你好"), ("错误619", "错误619")]
    # 参考总长 4 + 5 = 9，编辑距离 2
    assert metrics.corpus_cer(pairs2) == pytest.approx(2 / 9)
    assert metrics.sentence_accuracy(pairs2) == pytest.approx(0.5)


def test_word_error_rate():
    assert metrics.word_error_rate("vpn error 619", "vpn error 619") == 0.0
    assert metrics.word_error_rate("vpn error 619", "vpn 619") == pytest.approx(1 / 3)


# ----------------------------------------------------------------------
# 检索排序指标
# ----------------------------------------------------------------------
def test_recall_precision_hit():
    ranked = ["a", "b", "c"]
    gold = ["b", "d"]
    assert metrics.recall_at_k(ranked, gold, 3) == pytest.approx(0.5)
    assert metrics.precision_at_k(ranked, gold, 3) == pytest.approx(1 / 3)
    # ranked 条数不足 k 时仍以 k 为分母，不虚高
    assert metrics.precision_at_k(["b"], gold, 3) == pytest.approx(1 / 3)
    assert metrics.precision_at_k([], gold, 3) == 0.0
    assert metrics.hit_at_k(ranked, gold, 1) == 0.0
    assert metrics.hit_at_k(ranked, gold, 2) == 1.0


def test_reciprocal_rank_and_mrr():
    assert metrics.reciprocal_rank(["x", "a"], ["a"]) == pytest.approx(0.5)
    assert metrics.reciprocal_rank(["x", "y"], ["a"]) == 0.0
    assert metrics.mrr([["a"], ["x", "b"]], [["a"], ["b"]]) == pytest.approx((1 + 0.5) / 2)


def test_ndcg_at_k():
    assert metrics.ndcg_at_k(["a", "b", "c"], ["a"], 3) == pytest.approx(1.0)
    assert metrics.ndcg_at_k(["b", "a"], ["a"], 2) == pytest.approx(
        (1 / math.log2(3)) / 1.0
    )
    assert metrics.ndcg_at_k(["x", "y"], ["a"], 2) == 0.0


# ----------------------------------------------------------------------
# 结构化字段
# ----------------------------------------------------------------------
def test_exact_and_substring_match():
    assert metrics.exact_match("619", "错误码 619") is False
    assert metrics.exact_match("Error 619", "619") is False  # 归一化为 error619 ≠ 619
    assert metrics.exact_match("619", "619") is True
    assert metrics.substring_match("VPN 客户端", "VPN") is True
    assert metrics.substring_match("邮箱客户端", "VPN") is False


def test_field_accuracy():
    assert metrics.field_accuracy(["619", "502"], ["619", "504"]) == pytest.approx(0.5)


# ----------------------------------------------------------------------
# 描述质量与统计工具
# ----------------------------------------------------------------------
def test_key_point_hit_rate():
    text = "企业网络架构：员工终端经公网接入 VPN 网关与内网应用服务器。"
    assert metrics.key_point_hit_rate(text, ["员工终端", "VPN网关"]) == 1.0
    assert metrics.key_point_hit_rate(text, ["员工终端", "防火墙"]) == pytest.approx(0.5)
    assert metrics.key_point_hit_rate(text, []) == 0.0


def test_cosine():
    assert metrics.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert metrics.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert metrics.cosine([], []) == 0.0


def test_percentile_and_mean():
    values = [1, 2, 3, 4]
    assert metrics.mean(values) == pytest.approx(2.5)
    assert metrics.percentile(values, 0.5) == pytest.approx(2.5)
    assert metrics.percentile([], 0.5) == 0.0
    assert metrics.percentile([7], 0.9) == 7


def test_pearson():
    assert metrics.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert metrics.pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert metrics.pearson([1], [1]) == 0.0
