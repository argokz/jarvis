import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from jarvis import files, registry
from jarvis.agent import Reply
from jarvis.config import Settings
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.store import Store
from jarvis.voice import SentenceSplitter, VoiceService, VoiceSession
from jarvis.voice_client import FRAME, IDLE, LISTENING, WAITING, Config, Conversation


# --- нарезка на фразы -------------------------------------------------------------------------

def test_splitter_streams_sentences():
    sp = SentenceSplitter()
    out = []
    for chunk in ["При", "вет! Как ", "дела? Всё **хор", "ошо**", ". Хвост без точки"]:
        out += sp.feed(chunk)
    assert out == ["Привет!", "Как дела?", "Всё хорошо."]
    assert sp.flush() == ["Хвост без точки"]
    assert sp.flush() == []


def test_splitter_long_phrase_cut():
    sp = SentenceSplitter(soft_limit=30)
    out = sp.feed("один два три четыре пять шесть семь восемь девять десять ")
    assert out and all(len(x) <= 30 for x in out)


# --- роутер -----------------------------------------------------------------------------------

class FakeStream:
    def __init__(self, texts, final):
        self.texts, self.final = texts, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.texts:
                yield NS(type="text", text=t)
        return gen()

    async def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, texts, final):
        self.calls = []
        self.messages = NS(stream=lambda **kw: (self.calls.append(kw), FakeStream(texts, final))[1])


class FakeBrain:
    def __init__(self, text="Ответ агента.", delay=0.0):
        self.text, self.delay, self.calls = text, delay, []

    async def ask(self, user, text, channel="api"):
        self.calls.append((text, channel))
        await asyncio.sleep(self.delay)
        return Reply(self.text)


class FakeNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    @property
    def available(self):
        return True

    async def send(self, text):
        self.sent.append(text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_VOICE_AGENT_TIMEOUT", "0.2")
    s = Settings()
    registry.ensure_vault(s.vault)
    return s, Store(tmp_path / "j.db")


def final(stop="end_turn", content=()):
    return NS(stop_reason=stop, content=list(content))


async def collect(gen):
    return [x async for x in gen]


async def test_router_direct_answer(env):
    s, store = env
    client = FakeClient(["Два плюс два ", "— четыре."], final())
    r = VoiceRouter(s, FakeBrain(), store, Notifier(), client=client)
    out = await collect(r.respond("owner", "сколько будет два плюс два"))
    assert "".join(out) == "Два плюс два — четыре."
    kw = client.calls[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["output_config"] == {"effort": "low"}
    assert kw["messages"][-1]["role"] == "user" and "два плюс два" in kw["messages"][-1]["content"]
    assert store.history("owner")[-1] == ("assistant", "Два плюс два — четыре.")


async def test_router_delegates(env):
    s, store = env
    use = NS(type="tool_use", name="delegate", input={"task": "Найди в заметках идею про дашборд"})
    brain = FakeBrain("Идея лежит во входящих.")
    r = VoiceRouter(s, brain, store, Notifier(), client=FakeClient(["Секунду, посмотрю."], final("tool_use", [use])))
    out = "".join(await collect(r.respond("owner", "где идея про дашборд?")))
    assert out == "Секунду, посмотрю. Идея лежит во входящих."
    task, channel = brain.calls[0]
    assert task.startswith("Найди в заметках идею") and channel == "voice-agent"


async def test_router_bad_tool_input_falls_back_to_utterance(env):
    s, store = env
    use = NS(type="tool_use", name="delegate", input={"task": 5})
    brain = FakeBrain()
    r = VoiceRouter(s, brain, store, Notifier(), client=FakeClient([], final("tool_use", [use])))
    out = "".join(await collect(r.respond("owner", "запиши идею")))
    assert out.startswith("Секунду.") and brain.calls[0][0].startswith("запиши идею")


async def test_router_refusal(env):
    s, store = env
    r = VoiceRouter(s, FakeBrain(), store, Notifier(), client=FakeClient([], final("refusal")))
    assert "".join(await collect(r.respond("owner", "x"))) == "С этим я помочь не могу."


async def test_router_slow_agent_notifies_later(env):
    s, store = env
    use = NS(type="tool_use", name="delegate", input={"task": "долгая задача"})
    notifier = FakeNotifier()
    r = VoiceRouter(s, FakeBrain("Сделал.", delay=0.5), store, notifier,
                    client=FakeClient(["Секунду."], final("tool_use", [use])))
    out = "".join(await collect(r.respond("owner", "сделай долгое")))
    assert "Пришлю результат в Telegram" in out
    await asyncio.sleep(0.6)
    assert notifier.sent and "Сделал." in notifier.sent[0]


async def test_router_history_shared_across_channels(env):
    s, store = env
    store.log("owner", "telegram", "user", payload="привет из телеграма")
    store.log("owner", "telegram", "assistant", payload={"text": "привет!"})
    store.log("owner", "voice-agent", "user", payload="внутренняя задача")  # не должна попасть
    client = FakeClient(["ок"], final())
    r = VoiceRouter(s, FakeBrain(), store, Notifier(), client=client)
    await collect(r.respond("owner", "что я писал?"))
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "привет из телеграма"
    assert all("внутренняя" not in m["content"] for m in msgs)


# --- WebSocket ----------------------------------------------------------------------------------

class FakeSpeech:
    enabled, tts_rate = True, 16000

    async def recognize(self, audio, fmt="oggopus", sample_rate=None):
        assert fmt == "lpcm" and sample_rate == 16000
        return "распознанный текст"

    async def synthesize(self, text):
        return text.encode()


class FakeRouter:
    def __init__(self, delay=0.0):
        self.delay, self.seen = delay, []

    async def respond(self, user, text, channel="voice"):
        self.seen.append(text)
        for chunk in ["Первая фраза. ", "Вторая"]:
            await asyncio.sleep(self.delay)
            yield chunk


def ws_app(router):
    app = FastAPI()
    svc = VoiceService(router, FakeSpeech())

    @app.websocket("/ws")
    async def ep(ws: WebSocket):
        await ws.accept()
        await VoiceSession(ws, svc).run()

    return app


def read_turn(ws):
    events = []
    while True:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            events.append(("bytes", msg["bytes"]))
            continue
        data = json.loads(msg["text"])
        events.append(data)
        if data["type"] == "done":
            return events


def test_ws_text_turn():
    router = FakeRouter()
    with TestClient(ws_app(router)).websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "hello", "tts": True, "stt": True, "sample_rate": 16000}
        ws.send_json({"type": "text", "text": "привет"})
        ev = read_turn(ws)
    assert ev[0] == {"type": "start", "turn": 1}
    texts = [e["text"] for e in ev if isinstance(e, dict) and e["type"] == "text"]
    assert texts == ["Первая фраза.", "Вторая"]
    audio = [e[1] for e in ev if isinstance(e, tuple)]
    assert audio == ["Первая фраза.".encode(), "Вторая".encode()]
    # заголовок audio идёт прямо перед своими байтами
    i = next(k for k, e in enumerate(ev) if isinstance(e, dict) and e["type"] == "audio")
    assert isinstance(ev[i + 1], tuple) and ev[i]["bytes"] == len(ev[i + 1][1])


def test_ws_audio_turn_and_cancel():
    router = FakeRouter(delay=0.3)
    with TestClient(ws_app(router)).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_bytes(b"\0\0" * 1600)
        assert ws.receive_json() == {"type": "start", "turn": 1}
        assert ws.receive_json()["text"] == "распознанный текст"
        ws.send_json({"type": "text", "text": "новый вопрос"})  # перебиваем первый ход
        ev = read_turn(ws)
    assert all(e.get("turn") == 2 for e in ev if isinstance(e, dict))
    assert router.seen == ["распознанный текст", "новый вопрос"]


# --- клиент: логика разговора --------------------------------------------------------------------

class Script:
    """Подставные детекторы: кадр несёт метку — 'w' wake word, 's' речь, '.' тишина."""

    def __init__(self):
        self.label = "."

    def score(self, frame):
        return 1.0 if self.label == "w" else 0.0

    def reset(self):
        pass

    def prob(self, frame):
        return 1.0 if self.label == "s" else 0.0


class FakePlayer:
    def __init__(self):
        self.buf, self.beeps = [], 0

    def play(self, pcm):
        self.buf.append(pcm)

    def beep(self):
        self.beeps += 1

    def stop(self):
        self.buf.clear()

    @property
    def playing(self):
        return bool(self.buf)


async def feed(conv, det, labels):
    for ch in labels:
        det.label = ch
        await conv.on_frame(np.zeros(FRAME, dtype=np.int16))


@pytest.fixture
def conv():
    det, player, sent = Script(), FakePlayer(), []

    async def send(x):
        sent.append(x)

    c = Conversation(det, det, player, send, Config(end_silence_ms=240, listen_timeout_ms=400, followup_ms=320),
                     log=lambda *a: None)
    return c, det, player, sent


async def test_client_wake_record_send(conv):
    c, det, player, sent = conv
    await feed(c, det, "..w")
    assert c.state == LISTENING and player.beeps == 1
    await feed(c, det, ".sss...")
    assert c.state == WAITING and c.turn == 1
    pcm = sent[-1]
    assert isinstance(pcm, bytes) and len(pcm) == FRAME * 2 * 7  # преролл + речь + тишина


async def test_client_listen_timeout(conv):
    c, det, _, sent = conv
    await feed(c, det, "w.....")
    assert c.state == IDLE and not sent


async def test_client_plays_only_current_turn_and_barges_in(conv):
    c, det, player, sent = conv
    await feed(c, det, "wsss...")
    await c.on_message(json.dumps({"type": "audio", "turn": 1, "bytes": 2}))
    await c.on_message(b"ab")
    assert player.buf == [b"ab"]
    await feed(c, det, "w")                      # перебили wake word'ом
    assert json.loads(sent[-1]) == {"type": "cancel"} and c.state == LISTENING and not player.buf
    await c.on_message(json.dumps({"type": "audio", "turn": 1, "bytes": 2}))
    await c.on_message(b"zz")                    # хвост старого хода — игнор
    assert not player.buf
    await feed(c, det, "ss...")
    assert c.turn == 2


async def test_client_followup_then_idle(conv):
    c, det, player, sent = conv
    await c.send_text("привет")
    await c.on_message(json.dumps({"type": "done", "turn": 1}))
    await feed(c, det, ".")
    assert c.state == LISTENING                  # слушаем продолжение без wake word
    await feed(c, det, "....")
    assert c.state == IDLE


# --- файлы -------------------------------------------------------------------------------------

def test_files_find_and_inside(tmp_path):
    root = (tmp_path / "docs").resolve()
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "Бюджет_2026.xlsx").write_text("x")
    (root / "notes.txt").write_text("x")
    (root / ".git").mkdir()
    (root / ".git" / "бюджет 2026 копия.xlsx").write_text("x")
    hits = files.find("бюджет 2026", (root,))
    assert [h.name for h in hits] == ["Бюджет_2026.xlsx"]
    assert files.inside(root / "sub" / "Бюджет_2026.xlsx", (root,))
    assert not files.inside(tmp_path / "other.txt", (root,))
    assert not files.inside(root / ".." / "other.txt", (root,))
