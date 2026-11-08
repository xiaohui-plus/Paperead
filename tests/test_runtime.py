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
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    assert default_data_dir() == (tmp_path / "lib").resolve()


def test_default_data_dir_uses_localappdata_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPEREAD_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    assert default_data_dir() == tmp_path / "paperead-data"


def test_default_data_dir_migrates_legacy_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPEREAD_DATA", raising=False)
    legacy = tmp_path / "paper-reflow-data"
    (legacy / "papers").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert default_data_dir() == tmp_path / "paperead-data"
    assert (tmp_path / "paperead-data" / "papers").is_dir()
    assert not legacy.exists()


def test_legacy_env_prefix_still_works(monkeypatch):
    from paperead.env import env

    monkeypatch.delenv("PAPEREAD_MOCK", raising=False)
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")
    assert env("PAPEREAD_MOCK") == "1"

    monkeypatch.setenv("PAPEREAD_MOCK", "0")
    assert env("PAPEREAD_MOCK") == "0"


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
