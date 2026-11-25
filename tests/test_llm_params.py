from paperead.llm import _completion_kwargs, _openai_reasoning_model
from server.llm_proxy import _adapt_body_for_model

MESSAGES = [{"role": "user", "content": "hi"}]


def test_reasoning_model_detection():
    assert _openai_reasoning_model("gpt-5.6-sol")
    assert _openai_reasoning_model("gpt-5.6-luna")
    assert _openai_reasoning_model("gpt-6.2-astra")
    assert _openai_reasoning_model("openai/gpt-5.6-sol")
    assert _openai_reasoning_model("o3-mini")
    assert not _openai_reasoning_model("deepseek-flash")
    assert not _openai_reasoning_model("gpt-4o")


def test_gpt56_uses_max_completion_tokens_without_temperature():
    kwargs = _completion_kwargs("gpt-5.6-sol", MESSAGES, temperature=0.25, max_tokens=4096)

    assert kwargs["max_completion_tokens"] == 4096
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs


def test_reasoning_model_omits_empty_max_tokens():
    kwargs = _completion_kwargs("gpt-6.2-astra", MESSAGES, temperature=0.25, max_tokens=None)

    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs
    assert "temperature" not in kwargs


def test_compatible_model_keeps_legacy_params():
    kwargs = _completion_kwargs("deepseek-chat", MESSAGES, temperature=0.25, max_tokens=4096)

    assert kwargs["max_tokens"] == 4096
    assert kwargs["temperature"] == 0.25
    assert "max_completion_tokens" not in kwargs


def test_proxy_adapts_reasoning_body():
    body = {
        "model": "gpt-6.2-astra",
        "messages": MESSAGES,
        "max_tokens": 8192,
        "temperature": 0.25,
        "top_p": 0.9,
        "stream": True,
    }

    _adapt_body_for_model(body, "gpt-6.2-astra")

    assert body["max_completion_tokens"] == 8192
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert "top_p" not in body
    assert body["stream"] is True


def test_proxy_leaves_other_models_untouched():
    body = {"model": "deepseek-chat", "max_tokens": 10, "temperature": 0.25}

    _adapt_body_for_model(body, "deepseek-chat")

    assert body == {"model": "deepseek-chat", "max_tokens": 10, "temperature": 0.25}
