"""公式 LaTeX 校验回归测试。

对应缺陷：视觉模型返回 `\\left.x_{t+1}^{\\mathrm{down}}\\right\\}` 时，
裸 `{`/`}` 计数把 `\\right\\}` 的转义大括号当成未配对的组括号，
识别结果被丢弃、阅读页一直提示「部分公式暂时无法识别，保留为截图」。
"""

from __future__ import annotations

from paperead.formula import _normalize_delimiters, validate_latex


def test_right_brace_delimiter_is_not_unbalanced():
    tex = _normalize_delimiters(r"\left.x_{t+1}^{\mathrm{down}}\right\}")
    assert tex == r"x_{t+1}^{\mathrm{down}}\}"
    ok, reason = validate_latex(tex)
    assert ok, reason


def test_set_braces_are_valid():
    tex = _normalize_delimiters(
        r"\left\{x_t^{\text{front}}, x_t^{\text{down}}\right\}"
    )
    ok, reason = validate_latex(tex)
    assert ok, reason
    assert r"\{" in tex and r"\}" in tex


def test_left_right_parentheses_are_normalized():
    tex = _normalize_delimiters(r"\left( a + b \right)")
    assert tex == r"( a + b )"
    ok, reason = validate_latex(tex)
    assert ok, reason


def test_truly_unbalanced_braces_are_rejected():
    ok, reason = validate_latex(r"x_{t")
    assert not ok
    assert reason == "unbalanced braces"