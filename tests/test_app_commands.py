"""Голосовые команды из приложения: правка фраз сценария и своя команда без участия агента."""
import importlib

import pytest
from fastapi.testclient import TestClient

H = {"Authorization": "Bearer t0k"}


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_URL", "HASS_URL", "HASS_TOKEN", "JARVIS_PC_CONTROL"):
        monkeypatch.setenv(name, "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        yield api, client, tmp_path / "vault" / "50_Scenarios"


def test_owner_creates_a_voice_command_without_a_confirmation(core):
    api, client, folder = core
    body = {"name": "Тихий вечер", "phrases": ["тихий вечер", "сделай потише"], "steps": [{"volume": 20}, {"say": "Сделал тише"}]}
    assert client.post("/scenarios", json=body).status_code == 401
    r = client.post("/scenarios", json=body, headers=H)
    assert r.status_code == 200 and "сохранён" in r.json()["text"]
    assert not api.confirms.pending()                      # владелец собрал команду сам — спрашивать его незачем
    note = (folder / "Тихий вечер.md").read_text(encoding="utf-8")
    assert "тихий вечер" in note and "volume: 20" in note
    listed = client.get("/scenarios", headers=H).json()
    assert [s["phrases"] for s in listed if s["id"] == "Тихий вечер"] == [["тихий вечер", "сделай потише"]]


def test_bad_commands_are_explained(core):
    _, client, _ = core
    ok = {"name": "Раз", "phrases": ["раз два"], "steps": [{"say": "три"}]}
    assert client.post("/scenarios", json=ok, headers=H).status_code == 200
    taken = client.post("/scenarios", json={**ok, "name": "Два"}, headers=H)
    assert taken.status_code == 400 and "уже есть" in taken.json()["detail"]
    assert client.post("/scenarios", json={"name": "Пусто", "phrases": ["пусто"], "steps": []}, headers=H).status_code == 400
    home = client.post("/scenarios", json={"name": "Свет", "phrases": ["свет"], "steps": [{"on": "Лампа"}]}, headers=H)
    assert home.status_code == 400 and "Home Assistant" in home.json()["detail"]   # дом не подключён — шаг невыполним


def test_phrases_are_edited_and_the_rest_of_the_note_stays(core):
    _, client, folder = core
    client.post("/scenarios", json={"name": "Утро", "phrases": ["доброе утро"], "steps": [{"say": "Доброе"}]}, headers=H)
    note = folder / "Утро.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nЗаметка владельца.\n", encoding="utf-8")
    r = client.post("/scenarios/Утро/phrases", json={"phrases": ["с добрым утром", "я проснулся"], "without_name": True}, headers=H)
    assert r.status_code == 200 and r.json()["phrases"] == ["с добрым утром", "я проснулся"] and r.json()["without_name"]
    text = note.read_text(encoding="utf-8")
    assert "Заметка владельца." in text and "say: Доброе" in text and "доброе утро" not in text
    assert client.post("/scenarios/Утро/phrases", json={"phrases": []}, headers=H).status_code == 400   # без фраз и расписания не запустить
    assert client.post("/scenarios/Нет/phrases", json={"phrases": ["а б"]}, headers=H).status_code == 404
    client.post("/scenarios", json={"name": "Вечер", "phrases": ["добрый вечер"], "steps": [{"say": "Добрый"}]}, headers=H)
    clash = client.post("/scenarios/Утро/phrases", json={"phrases": ["добрый вечер"]}, headers=H)
    assert clash.status_code == 400 and "Вечер" in clash.json()["detail"]
