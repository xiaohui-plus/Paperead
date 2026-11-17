import time

import pytest

from server.plugin_auth import (
    PairingCodes,
    PairingError,
    PluginRegistry,
    is_extension_origin,
)


def test_extension_origin_detection():
    assert is_extension_origin("chrome-extension://abcdefghijklmnop")
    assert is_extension_origin("moz-extension://6f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f")
    assert not is_extension_origin("https://arxiv.org")
    assert not is_extension_origin("")


def test_registry_issue_verify_and_revoke(tmp_path):
    registry = PluginRegistry(tmp_path / "plugins.json")
    token, plugin = registry.issue("chrome-extension://abc", "Chrome")

    assert registry.verify(token, "chrome-extension://abc") is not None
    assert registry.verify(token, "chrome-extension://other") is None
    assert registry.verify("wrong-token", "chrome-extension://abc") is None

    assert registry.revoke(plugin.id) is True
    assert registry.verify(token, "chrome-extension://abc") is None
    assert registry.revoke(plugin.id) is False


def test_registry_persists_without_plain_token(tmp_path):
    path = tmp_path / "plugins.json"
    registry = PluginRegistry(path)
    token, _ = registry.issue("chrome-extension://abc", "Chrome")

    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert "token_sha256" in text

    reloaded = PluginRegistry(path)
    assert reloaded.verify(token, "chrome-extension://abc") is not None


def test_pairing_code_is_single_use():
    codes = PairingCodes()
    code, expires_at = codes.issue()
    assert len(code) == 6
    assert expires_at > time.time()

    codes.consume("127.0.0.1", code)

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", code)
    assert error.value.status_code == 410


def test_pairing_code_rejects_wrong_code():
    codes = PairingCodes()
    codes.issue()

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "000000")
    assert error.value.status_code == 401


def test_pairing_code_rate_limits_failures():
    codes = PairingCodes()
    codes.issue()

    for _ in range(5):
        with pytest.raises(PairingError):
            codes.consume("127.0.0.1", "000000")

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "000000")
    assert error.value.status_code == 429


def test_wait_window_allows_code_less_claim_once():
    codes = PairingCodes()
    expires_at = codes.open_wait()
    assert expires_at > time.time()

    codes.consume("127.0.0.1", "")

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "")
    assert error.value.status_code == 410


def test_code_less_claim_requires_wait_window():
    codes = PairingCodes()

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "")
    assert error.value.status_code == 410
    assert "连接浏览器插件" in error.value.detail
