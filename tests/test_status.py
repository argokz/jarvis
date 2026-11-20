"""Сводка состояния для экрана «Функции»: что работает, что выключено, чем это попробовать."""
import asyncio
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.speech import SpeechError
from jarvis.status import CHECK_PHRASE, Status
from jarvis.store import Store


class FakeSpeech:
    local, enabled, tts_rate, tts_voice = True, True, 48000, "xenia"
    stt_url, tts_url = "http://stt.test/v1/audio/transcriptions", "http://tts.test/v1/audio/speech"

    def __init__(self, heard=CHECK_PHRASE, error=""):
        self.heard, self.error, self.calls = heard, error, []

    async def synthesize(self, text):
        if self.error:
            raise SpeechError(self.error)
        return b"\0\0" * 100

    async def recognize(self, audio, fmt="oggopus", sample_rate=None, hint=""):
        self.calls.append((fmt, sample_rate))
        return self.heard


def settings(tmp_path, **kw):
    base = dict(assistant_name="Клод", model="agent-model", router_model="fast-model", pc_control=False,
                yandex_music_token="", vault=tmp_path, open_roots=(), browser=False, browser_dir=Path("/nope"),
                screen=False, notion_token="", telegram_token="", telegram_allowed=frozenset())
    return SimpleNamespace(**{**base, **kw})


def make(tmp_path, alive=("stt.test", "tts.test"), speech=None, env=None, **kw):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host not in alive:
            raise httpx.ConnectError("refused")
        return httpx.Response(404)  # сервер без /health всё равно жив

    (tmp_path / "10_Projects").mkdir(exist_ok=True)
    voice = SimpleNamespace(sessions=set(), wake=SimpleNamespace(phrases=["клод"]))
    return Status(settings(tmp_path, **kw), Store(tmp_path / "j.db"), speech or FakeSpeech(), home=None, tunes=None,
                  scenarios=None, scheduler=None, voice=voice, notifier=None,
                  client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), env=(env or {}).get)


def by_id(report):
    return {f["id"]: f for f in report["features"]}


def test_everything_optional_is_off_or_waits_for_setup(tmp_path):
    report = asyncio.run(make(tmp_path).collect())
    f = by_id(report)
    assert report["name"] == "Клод" and report["wake"] == ["клод"] and report["mics"] == 0
    assert f["agent"]["state"] == "setup" and "ANTHROPIC_API_KEY" in f["agent"]["hint"]
    assert f["voice"]["state"] == "ready" and "xenia" in f["voice"]["note"]
    assert f["schedule"]["state"] == "ready" and f["home"]["state"] == "setup"
    assert {f[k]["state"] for k in ("pc", "music", "files", "browser", "screen")} == {"off"}
    assert {f[k]["state"] for k in ("notion", "telegram", "push")} == {"setup"}
    assert all(not f[k]["tries"] for k in f if f[k]["state"] != "ready")  # пробовать можно только рабочее


def test_enabled_features_offer_phrases_and_say_which_model_answers(tmp_path):
    report = asyncio.run(make(tmp_path, env={"ANTHROPIC_API_KEY": "k"}, pc_control=True, screen=True,
                              open_roots=(tmp_path,), notion_token="n").collect())
    f = by_id(report)
    assert f["agent"]["state"] == "ready" and f["agent"]["tries"][0]["via"] == "agent"
    assert [t["via"] for t in f["schedule"]["tries"]] == ["fast", "fast"]
    assert f["pc"]["state"] == "ready" and f["screen"]["tries"][0]["via"] == "agent"
    assert f["files"]["state"] == "ready" and f["notion"]["state"] == "ready"
    assert "разрешение" in f["screen"]["tries"][0]["effect"]  # побочное действие названо заранее


def test_voice_is_down_when_a_speech_server_does_not_answer(tmp_path):
    f = by_id(asyncio.run(make(tmp_path, alive=("stt.test",)).collect()))["voice"]
    assert f["state"] == "down" and "синтез речи" in f["note"] and "распознавание" not in f["note"]
    assert not f["tries"]


def test_voice_needs_setup_without_speech(tmp_path):
    speech = FakeSpeech()
    speech.enabled = False
    assert by_id(asyncio.run(make(tmp_path, speech=speech).collect()))["voice"]["state"] == "setup"


def test_agent_spending_today_counts_turns_and_survives_cut_records(tmp_path):
    st = make(tmp_path)
    st.store.log("owner", "app", "assistant", payload={"cost_usd": 0.03, "text": "раз"})
    st.store.log("owner", "app", "assistant", payload={"cost_usd": 0.0421, "text": "я" * 5000})  # запись обрезана
    st.store.log("owner", "voice", "assistant", payload={"text": "быстрая модель цену не пишет"})
    st.store.log("owner", "app", "assistant", payload={"cost_usd": None, "text": "сценарий"})
    assert asyncio.run(st.collect())["agent_today"] == {"usd": 0.0721, "turns": 2}
    assert st.store.spent(st._clock() + 60) == (0.0, 0)


def test_speech_check_synthesizes_and_recognizes_back(tmp_path):
    speech = FakeSpeech(heard="проверка связи, меня хорошо слышно")
    got = asyncio.run(make(tmp_path, speech=speech).speech_check())
    assert got["ok"] and got["error"] == "" and got["tts_s"] is not None and got["stt_s"] is not None
    assert speech.calls == [("lpcm", 48000)]  # распознаём с той частотой, с какой синтезировали


def test_speech_check_reports_what_was_heard_and_errors(tmp_path):
    got = asyncio.run(make(tmp_path, speech=FakeSpeech(heard="проверка связи")).speech_check())
    assert not got["ok"] and got["heard"] == "проверка связи"
    got = asyncio.run(make(tmp_path, speech=FakeSpeech(error="TTS-сервер недоступен")).speech_check())
    assert not got["ok"] and "недоступен" in got["error"]


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_URL", "HASS_URL", "JARVIS_STT_URL", "JARVIS_TTS_URL",
                 "YANDEX_SPEECHKIT_API_KEY", "JARVIS_BROWSER", "JARVIS_PC_CONTROL"):
        monkeypatch.setenv(name, "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        yield api, client


def test_status_endpoints_need_the_token(core):
    _, client = core
    assert client.get("/status").status_code == 401
    assert client.post("/status/speech").status_code == 401
    report = client.get("/status", headers={"Authorization": "Bearer t0k"}).json()
    assert {"agent", "voice", "schedule", "home", "scenarios", "memory"} <= {f["id"] for f in report["features"]}
    check = client.post("/status/speech", headers={"Authorization": "Bearer t0k"}).json()
    assert not check["ok"] and check["error"]


def test_token_from_websocket_address_never_reaches_the_log(core):
    api, _ = core
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]',
                               ("1.2.3.4:0", "/events/ws?token=s3cr3t&x=1"), None)
    assert api.HideToken().filter(record)
    assert record.getMessage() == '1.2.3.4:0 - "WebSocket /events/ws?token=***&x=1" [accepted]'
    assert any(isinstance(f, api.HideToken) for f in logging.getLogger("uvicorn.error").filters)
