import sys

from server.runtime import (
    allowed_origins,
    default_data_dir,
    ensure_data_dir,
    new_token,
    web_dist,
)


def test_web_dist_uses_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert web_dist() == tmp_path / "web" / "dist"


def test_default_data_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    assert default_data_dir() == (tmp_path / "lib").resolve()


def test_default_data_dir_uses_localappdata_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER_REFLOW_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    assert default_data_dir() == tmp_path / "paper-reflow"


def test_ensure_data_dir_creates_layout(tmp_path):
    ensure_data_dir(tmp_path)
    assert (tmp_path / "papers").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "backups").is_dir()


def test_new_token_is_random():
    assert new_token() != new_token()
    assert len(new_token()) >= 32


def test_allowed_origins_include_loopback_and_dev():
    origins = allowed_origins(8123)
    assert "http://127.0.0.1:8123" in origins
    assert "http://localhost:8123" in origins
    assert "http://localhost:5173" in origins
