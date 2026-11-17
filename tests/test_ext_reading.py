import importlib
import json

from fastapi.testclient import TestClient

ORIGIN = "chrome-extension://abcdefghijklmnop"


def _client(monkeypatch, tmp_path):
    data = tmp_path / "lib"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAPEREAD_DATA", str(data))
    monkeypatch.setenv("PAPEREAD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)

    async def noop(paper_id: str) -> None:
        return None

    monkeypatch.setattr(main, "_parse_task", noop)
    return main, TestClient(main.app)


def _pair(client: TestClient) -> str:
    code = client.post("/api/ext/v1/pair/session", json={"name": "Chrome"}).json()["code"]
    response = client.post(
        "/api/ext/v1/pair",
        json={"code": code, "name": "Chrome"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def _write_model(main, *, ready: bool = True) -> None:
    models = (
        [
            {
                "id": "m1",
                "kind": "answer",
                "baseUrl": "https://api.example.com/v1",
                "apiKey": "sk-test-123456",
                "model": "test-model",
            }
        ]
        if ready
        else []
    )
    payload = {"settings": {"models": models, "active": {"answer": "m1" if ready else ""}}}
    main.MODEL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.MODEL_SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_reading_status_reports_missing_model(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        response = client.get("/api/ext/v1/reading/status", headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"model_ready": False, "model_label": ""}


def test_reading_status_reports_configured_model(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        response = client.get("/api/ext/v1/reading/status", headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"model_ready": True, "model_label": "test-model"}


def test_reading_translate_uses_saved_model(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        calls = []

        async def fake_translate(req):
            calls.append(req)
            return {
                "ok": True,
                "translation": "译文",
                "target_lang": req.target_lang,
                "target_name": "简体中文",
            }

        monkeypatch.setattr(main, "translate_text_api", fake_translate)
        response = client.post(
            "/api/ext/v1/reading/translate",
            json={"text": "hello world"},
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["translation"] == "译文"
        assert calls[0].text == "hello world"
        assert calls[0].config.api_key == "sk-test-123456"
        assert calls[0].config.model == "test-model"


def test_reading_ask_uses_saved_model(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        monkeypatch.setattr(main.store, "get", lambda paper_id: {"id": paper_id})
        calls = []

        async def fake_ask(paper_id, req):
            calls.append((paper_id, req))
            return {"answer": "42", "sources": []}

        monkeypatch.setattr(main, "ask_paper", fake_ask)
        response = client.post(
            "/api/ext/v1/reading/ask",
            json={"paper_id": "p_test", "question": "贡献是什么？"},
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["answer"] == "42"
        assert calls[0][0] == "p_test"
        assert calls[0][1].config.model == "test-model"
        assert calls[0][1].web is False


def test_reading_requires_scope(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        plugin = main.plugin_registry._load()[0]
        plugin.scopes = ["library:read"]
        response = client.post(
            "/api/ext/v1/reading/translate",
            json={"text": "hello"},
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert "reading:translate" in response.json()["detail"]


def test_reading_requires_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        response = client.post("/api/ext/v1/reading/translate", json={"text": "hello"})
        assert response.status_code == 401


def _paper(main) -> str:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return main.store.add("test.pdf", data)["id"]


def test_reading_guide_without_cache_returns_null(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        paper_id = _paper(main)
        response = client.get(
            "/api/ext/v1/reading/guide",
            params={"paper_id": paper_id},
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["guide"] is None


def test_reading_annotations_create_and_list(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        paper_id = _paper(main)
        created = client.post(
            "/api/ext/v1/reading/annotations",
            json={
                "paper_id": paper_id,
                "quote": "important sentence",
                "note": "重点",
                "block_id": "",
                "page": 0,
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["quote"] == "important sentence"
        assert row["note"] == "重点"
        assert row["id"]

        listed = client.get(
            "/api/ext/v1/reading/annotations",
            params={"paper_id": paper_id},
            headers=_auth(token),
        )
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["annotations"]] == [row["id"]]


def test_reading_highlights_without_translation(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        paper_id = _paper(main)
        main.store.write_blocks(
            paper_id,
            [
                {
                    "id": "b1",
                    "page": 1,
                    "kind": "text",
                    "text": "hello",
                    "translatable": True,
                    "translated": "",
                }
            ],
        )
        listed = client.get(
            "/api/ext/v1/reading/highlights",
            params={"paper_id": paper_id},
            headers=_auth(token),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["translated_pages"] == 0

        made = client.post(
            "/api/ext/v1/reading/highlights",
            json={"paper_id": paper_id, "pages": [1], "force": False},
            headers=_auth(token),
        )
        assert made.status_code == 200, made.text
        assert made.json()["translated_pages"] == 0
        assert made.json()["needs_translation"] is True


def test_reading_annotations_requires_scope(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        paper_id = _paper(main)
        plugin = main.plugin_registry._load()[0]
        plugin.scopes = ["library:read"]
        response = client.post(
            "/api/ext/v1/reading/annotations",
            json={"paper_id": paper_id, "quote": "hello"},
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert "reading:annotate" in response.json()["detail"]


def test_reading_ask_stream_requires_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        response = client.post(
            "/api/ext/v1/reading/ask/stream",
            json={"paper_id": "p_test", "question": "贡献是什么？"},
        )
        assert response.status_code == 401


def test_reading_ask_stream_missing_paper(monkeypatch, tmp_path):
    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        monkeypatch.setattr(main.store, "get", lambda paper_id: None)
        response = client.post(
            "/api/ext/v1/reading/ask/stream",
            json={"paper_id": "p_missing", "question": "贡献是什么？"},
            headers=_auth(token),
        )
        assert response.status_code == 404, response.text


def test_reading_ask_stream_uses_saved_model(monkeypatch, tmp_path):
    from fastapi.responses import StreamingResponse

    main, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        _write_model(main)
        monkeypatch.setattr(main.store, "get", lambda paper_id: {"id": paper_id})
        calls = []
        event = 'data: {"type": "done", "answer": "42"}\n\n'

        async def fake_stream(paper_id, req):
            calls.append((paper_id, req))
            return StreamingResponse(
                iter([event]), media_type="text/event-stream"
            )

        monkeypatch.setattr(main, "ask_stream", fake_stream)
        response = client.post(
            "/api/ext/v1/reading/ask/stream",
            json={"paper_id": "p_test", "question": "贡献是什么？"},
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert event in response.text
        assert calls[0][0] == "p_test"
        assert calls[0][1].config.model == "test-model"
        assert calls[0][1].question == "贡献是什么？"
