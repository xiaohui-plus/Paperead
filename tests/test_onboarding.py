from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.onboarding import OnboardingStore, build_onboarding_router


def make_client(tmp_path: Path, has_user_data: bool = False) -> TestClient:
    store = OnboardingStore(tmp_path / "data")
    app = FastAPI()
    app.include_router(
        build_onboarding_router(store, lambda: has_user_data)
    )
    return TestClient(app)


def test_first_run_snapshot_prompts_once(tmp_path: Path):
    store = OnboardingStore(tmp_path)

    first = store.snapshot()
    assert first["shouldPrompt"] is True
    assert first["prompted"] is False
    assert first["dismissed"] is False

    store.write({"promptedAt": 100, "status": "prompted"})
    assert store.snapshot()["shouldPrompt"] is False

    # 新实例读同一个目录，状态不会丢
    assert OnboardingStore(tmp_path).snapshot()["prompted"] is True


def test_existing_user_is_treated_as_legacy(tmp_path: Path):
    client = make_client(tmp_path, has_user_data=True)

    state = client.get("/api/onboarding").json()

    assert state["legacyUser"] is True
    assert state["shouldPrompt"] is False


def test_lifecycle_start_progress_complete(tmp_path: Path):
    client = make_client(tmp_path)

    assert client.get("/api/onboarding").json()["shouldPrompt"] is True

    shown = client.post("/api/onboarding", json={"action": "shown"}).json()
    assert shown["prompted"] is True
    assert shown["status"] == "prompted"
    assert shown["shouldPrompt"] is False

    started = client.post(
        "/api/onboarding", json={"action": "start", "stepId": "config-model"}
    ).json()
    assert started["choice"] == "start"
    assert started["status"] == "in_progress"
    assert started["stepId"] == "config-model"

    progress = client.post(
        "/api/onboarding", json={"action": "progress", "stepId": "reader-guide"}
    ).json()
    assert progress["status"] == "in_progress"
    assert progress["stepId"] == "reader-guide"

    completed = client.post("/api/onboarding", json={"action": "complete"}).json()
    assert completed["status"] == "completed"
    assert completed["stepId"] is None

    # 完成或跳过之后，任何一次 GET 都不再提示
    assert client.get("/api/onboarding").json()["shouldPrompt"] is False


def test_dismiss_keeps_progress_but_turns_off_reminder(tmp_path: Path):
    client = make_client(tmp_path)

    client.post(
        "/api/onboarding", json={"action": "start", "stepId": "reader-guide"}
    )
    dismissed = client.post("/api/onboarding", json={"action": "dismiss"}).json()

    assert dismissed["status"] == "in_progress"
    assert dismissed["stepId"] == "reader-guide"
    assert dismissed["dismissed"] is True

    # 重新进入设置时仍能看到并手动继续上次进度
    state = client.get("/api/onboarding").json()
    assert state["stepId"] == "reader-guide"
    assert state["dismissed"] is True


def test_skip_and_legacy_actions(tmp_path: Path):
    client = make_client(tmp_path)

    skipped = client.post("/api/onboarding", json={"action": "skip"}).json()
    assert skipped["choice"] == "skip"
    assert skipped["status"] == "skipped"
    assert skipped["dismissed"] is True

    client = make_client(tmp_path / "other")
    legacy = client.post("/api/onboarding", json={"action": "legacy"}).json()
    assert legacy["choice"] == "legacy"
    assert legacy["status"] == "skipped"
    assert legacy["dismissed"] is False


def test_invalid_action_and_missing_step(tmp_path: Path):
    client = make_client(tmp_path)

    assert (
        client.post("/api/onboarding", json={"action": "nope"}).status_code == 400
    )
    assert (
        client.post("/api/onboarding", json={"action": "progress"}).status_code
        == 400
    )
