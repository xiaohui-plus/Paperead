import importlib

from fastapi.testclient import TestClient


def test_token_and_origin_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_REFLOW_TOKEN", "test-token")
    monkeypatch.setenv("PAPER_REFLOW_PORT", "8123")
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path))
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")

    import server.main as main

    importlib.reload(main)

    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 403
        assert (
            client.get(
                "/api/health", headers={"X-Paper-Reflow-Token": "test-token"}
            ).status_code
            == 200
        )

        home = client.get("/")
        assert home.status_code == 200
        assert "pr_token" in client.cookies

        papers = client.get("/api/papers")
        assert papers.status_code == 200
        assert papers.json()["papers"] == []

        blocked = client.get(
            "/api/papers", headers={"Origin": "https://evil.example"}
        )
        assert blocked.status_code == 403
