import importlib
import json

import fitz
from fastapi.testclient import TestClient

ORIGIN = "chrome-extension://abcdefghijklmnop"


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Paperead plugin test")
    data = doc.tobytes()
    doc.close()
    return data


def _client(monkeypatch, tmp_path, token: str = ""):
    data = tmp_path / "lib"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAPEREAD_DATA", str(data))
    monkeypatch.setenv("PAPEREAD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    if token:
        monkeypatch.setenv("PAPEREAD_TOKEN", token)
    else:
        monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)

    async def noop(paper_id: str) -> None:
        return None

    monkeypatch.setattr(main, "_parse_task", noop)
    return main, TestClient(main.app)


def _pair(client: TestClient) -> str:
    session = client.post("/api/ext/v1/pair/session", json={"name": "Chrome"})
    assert session.status_code == 200, session.text
    code = session.json()["code"]
    paired = client.post(
        "/api/ext/v1/pair",
        json={"code": code, "name": "Chrome"},
        headers={"Origin": ORIGIN},
    )
    assert paired.status_code == 200, paired.text
    return paired.json()["token"]


def test_ping_is_public(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, token="test-token")
    with client:
        response = client.get("/api/ext/v1/ping")
        assert response.status_code == 200
        assert response.json()["app"] == "paperead"


def test_pair_requires_extension_origin(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        code = client.post("/api/ext/v1/pair/session", json={"name": "Chrome"}).json()[
            "code"
        ]
        denied = client.post(
            "/api/ext/v1/pair",
            json={"code": code, "name": "Chrome"},
            headers={"Origin": "https://evil.example"},
        )
        assert denied.status_code == 403


def test_ingest_lookup_and_dedupe(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        metadata = {
            "source": "arxiv",
            "source_id": "arxiv:2401.00001",
            "title": "A Test Paper",
            "authors": "Ada Lovelace, Alan Turing",
            "abstract": "Testing ingestion.",
            "doi": "10.1000/test",
            "url": "https://arxiv.org/abs/2401.00001",
            "year": 2024,
            "venue": "TestConf",
        }
        files = {"file": ("paper.pdf", _pdf_bytes(), "application/pdf")}
        first = client.post(
            "/api/ext/v1/papers/ingest",
            files=files,
            data={"metadata": json.dumps(metadata)},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["already"] is False
        assert body["paper"]["title"] == "A Test Paper"
        assert body["paper"]["year"] == 2024
        assert body["paper"]["venue"] == "TestConf"
        paper_id = body["paper"]["id"]

        second = client.post(
            "/api/ext/v1/papers/ingest",
            files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
            data={"metadata": json.dumps(metadata)},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["already"] is True
        assert second.json()["paper"]["id"] == paper_id

        found = client.get(
            "/api/ext/v1/papers/lookup",
            params={"source_id": "arxiv:2401.00001"},
            headers=headers,
        )
        assert found.status_code == 200
        assert found.json()["found"] is True
        assert found.json()["paper"]["id"] == paper_id

        status = client.get(f"/api/ext/v1/papers/{paper_id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["paper"]["status"] in {"uploaded", "parsing", "ready"}


def test_ingest_rejects_non_pdf(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        response = client.post(
            "/api/ext/v1/papers/ingest",
            files={"file": ("paper.pdf", b"not a pdf", "application/pdf")},
            data={"metadata": "{}"},
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        )
        assert response.status_code == 400


def test_plugin_token_is_revoked(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        plugins = client.get("/api/ext/v1/admin/plugins").json()["plugins"]
        assert len(plugins) == 1

        deleted = client.delete(f"/api/ext/v1/admin/plugins/{plugins[0]['id']}")
        assert deleted.status_code == 200

        denied = client.get("/api/ext/v1/folders", headers=headers)
        assert denied.status_code == 401


def test_plugin_endpoint_requires_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/ext/v1/folders")
        assert response.status_code == 401


def test_main_api_still_requires_main_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, token="main-token")
    with client:
        assert client.get("/api/papers").status_code == 403
        assert (
            client.get("/api/papers", headers={"X-Paperead-Token": "main-token"}).status_code
            == 200
        )


def test_pair_wait_allows_code_less_claim(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        wait = client.post("/api/ext/v1/pair/wait")
        assert wait.status_code == 200
        assert wait.json()["expires_at"] > 0

        paired = client.post(
            "/api/ext/v1/pair",
            json={"name": "Auto"},
            headers={"Origin": ORIGIN},
        )
        assert paired.status_code == 200, paired.text
        assert paired.json()["token"]


def test_pair_without_wait_is_rejected(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        denied = client.post(
            "/api/ext/v1/pair",
            json={"name": "Auto"},
            headers={"Origin": ORIGIN},
        )
        assert denied.status_code == 410


def test_pair_wait_requires_main_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, token="main-token")
    with client:
        assert client.post("/api/ext/v1/pair/wait").status_code == 403
        assert (
            client.post(
                "/api/ext/v1/pair/wait",
                headers={"X-Paperead-Token": "main-token"},
            ).status_code
            == 200
        )


def test_extension_preflight_allows_private_network(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        response = client.options(
            "/api/ext/v1/pair",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-private-network"] == "true"


def test_pair_wait_accepts_request_without_origin(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        assert client.post("/api/ext/v1/pair/wait").status_code == 200
        paired = client.post("/api/ext/v1/pair", json={"name": "用户脚本"})
        assert paired.status_code == 200, paired.text
        assert paired.json()["token"]


def test_plugin_token_allows_page_origin(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        client.post("/api/ext/v1/pair/wait")
        token = client.post("/api/ext/v1/pair", json={"name": "用户脚本"}).json()["token"]
        response = client.get(
            "/api/ext/v1/folders",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://arxiv.org",
            },
        )
        assert response.status_code == 200
