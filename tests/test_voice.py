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
from jarvis.schedule import Scheduler
from jarvis.store import Store
from jarvis.voice import SentenceSplitter, VoiceService, VoiceSession
from jarvis.wake import WakeWords, close, norm, parse
from jarvis.voice_client import (FRAME, IDLE, LISTENING, WAITING, Config, Conversation, RingSound, level_stats,
                                 load_sound, mic_verdict, voice_ratio)


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

    def where(self):
        return "Telegram"

    async def send(self, text, **kw):
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


async def test_assistant_is_named_after_the_first_wake_word(env, monkeypatch):
    _, store = env

    def prompt(s):
        return VoiceRouter(s, FakeBrain(), store, Notifier(), client=FakeClient([], final())).system()

    monkeypatch.delenv("JARVIS_WAKE_WORDS", raising=False)  # в .env владельца может стоять своё имя
    assert Settings().assistant_name == "Джарвис" and "Тебя зовут Джарвис" in prompt(Settings())
    monkeypatch.setenv("JARVIS_WAKE_WORDS", "клод, клут; плот")
    assert Settings().assistant_name == "Клод" and "Тебя зовут Клод" in prompt(Settings())
    monkeypatch.setenv("JARVIS_WAKE_WORDS", "")
    assert Settings().assistant_name == "Джарвис"


async def test_router_delegates(env):
    s, store = env
    use = NS(type="tool_use", name="delegate", input={"task": "Найди в заметках идею про дашборд"})
    brain = FakeBrain("Идея лежит во входящих.")
    r = VoiceRouter(s, brain, store, Notifier(), client=FakeClient(["Секунду, посмотрю."], final("tool_use", [use])))
    out = "".join(await collect(r.respond("owner", "где идея про дашборд?")))
    assert out == "Секунду, посмотрю. Идея лежит во входящих."
    task, channel = brain.calls[0]
    assert task.startswith("Найди в заметках идею") and channel == "voice-agent"


async def test_router_remembers_that_it_delegated_and_what_the_agent_answered(env):
    """Без вызова в истории модель в следующей реплике заявляла, что ничего не искала и не делала."""
    s, store = env
    use = NS(type="tool_use", id="tu_1", name="delegate", input={"task": "Найди договор"})
    client = FakeClient(["Секунду, посмотрю."], final("tool_use", [use]))
    r = VoiceRouter(s, FakeBrain("Договора на компьютере нет."), store, Notifier(), client=client)
    await collect(r.respond("owner", "найди договор"))
    await collect(r.respond("owner", "точно нет?"))
    past = client.calls[1]["messages"]
    assert [m["role"] for m in past] == ["user", "assistant", "user", "assistant", "user"]
    call, result = past[1]["content"][0], past[2]["content"][0]
    assert call["name"] == "delegate" and call["input"] == {"task": "Найди договор"}
    assert result == {"type": "tool_result", "tool_use_id": call["id"], "content": "Договора на компьютере нет."}
    assert past[3]["content"] == "Секунду, посмотрю. Договора на компьютере нет."
    # агент не успел: в истории видно, что задача ещё выполняется, а не что её не было
    slow = VoiceRouter(s, FakeBrain("Готово.", delay=0.5), store, FakeNotifier(), client=client)
    await collect(slow.respond("owner", "разбери заметки"))
    await collect(slow.respond("owner", "ну как?"))
    results = [m["content"][0]["content"] for m in client.calls[-1]["messages"]
               if m["role"] == "user" and not isinstance(m["content"], str)]
    assert "ещё работает" in results[-1]
    await asyncio.sleep(0.6)


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
    assert msgs[0]["content"].endswith("] привет из телеграма")  # со временем, когда это было сказано
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
        assert ws.receive_json() == {"type": "hello", "tts": True, "stt": True, "sample_rate": 16000, "wake": []}
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


# --- WebSocket: таймеры, напоминания, будильники -------------------------------------------------

class Heard(FakeSpeech):
    """Распознаёт то, что задано в тесте."""

    def __init__(self, heard="стоп"):
        self.heard, self.hints = heard, []

    async def recognize(self, audio, fmt="oggopus", sample_rate=None, hint=""):
        self.hints.append(hint)
        return self.heard


class Alerts:
    """Ядро с планировщиком на ручных часах и одним голосовым клиентом."""

    def __init__(self, tmp_path, heard="стоп"):
        self.now, self.router, self.speech = 1_800_000_000.0, FakeRouter(), Heard(heard)
        self.sched = Scheduler(Store(tmp_path / "j.db"), clock=lambda: self.now)
        self.svc = VoiceService(self.router, self.speech, self.sched)
        self.sched.sinks.append(self.svc.on_alert)
        app = FastAPI()

        @app.websocket("/ws")
        async def ep(ws: WebSocket):
            await ws.accept()
            await VoiceSession(ws, self.svc).run()

        self.client = TestClient(app)

    def connect(self, ws, subscribe=True):
        ws.receive_json()
        if subscribe:
            ws.send_json({"type": "subscribe"})
        ws.send_json({"type": "ping"})
        return ws.receive_json()

    def fire(self, kind="timer", seconds=60, **args):
        """Поставить запись и перевести часы на её время."""
        self.client.portal.call(self.sched.call, "schedule_set", {"kind": kind, "in_seconds": seconds, **args})
        self.now += seconds
        self.client.portal.call(self.sched.tick)
        return max(i["id"] for i in self.sched._rows("1=1"))


def test_ws_timer_rings_on_subscribed_client_and_stops_without_model(tmp_path):
    a = Alerts(tmp_path)
    with a.client, a.client.websocket_connect("/ws") as ws:
        assert a.connect(ws) == {"type": "pong"}
        timer = a.fire()
        assert ws.receive_json() == {"type": "ring", "id": timer, "kind": "timer", "text": "Таймер на 1 минуту"}
        assert ws.receive_json() == {"type": "audio", "alert": timer, "bytes": len("Таймер на 1 минуту.".encode())}
        assert ws.receive_bytes() == "Таймер на 1 минуту.".encode()
        ws.send_json({"type": "text", "text": "Стоп!"})
        ev = read_turn(ws)
    kinds = [e["type"] if isinstance(e, dict) else "bytes" for e in ev]
    assert kinds == ["start", "stop", "text", "audio", "bytes", "done"]
    assert ev[1] == {"type": "stop", "id": timer} and ev[2]["text"] == "Выключил."
    assert a.router.seen == [] and not a.sched.ringing()   # модель не спрашивали


def test_ws_passive_reply_is_handled_only_if_it_is_a_command(tmp_path):
    a = Alerts(tmp_path, heard="а потом он выключил свет и ушёл")
    with a.client, a.client.websocket_connect("/ws") as ws:
        a.connect(ws)
        alarm = a.fire("alarm")
        assert ws.receive_json()["type"] == "ring" and ws.receive_json()["alert"] == alarm
        ws.receive_bytes()
        ws.send_json({"type": "passive"})                  # шум из комнаты, пока звенит
        ws.send_bytes(b"\0\0" * 1600)
        assert read_turn(ws) == [{"type": "start", "turn": 1},
                                 {"type": "ignored", "turn": 1, "text": "а потом он выключил свет и ушёл"},
                                 {"type": "done", "turn": 1}]
        assert a.router.seen == [] and a.sched.ringing()
        a.speech.heard = "Отложи на пять минут."
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        ev = read_turn(ws)
        assert [e["type"] for e in ev if isinstance(e, dict)] == ["start", "stop", "transcript", "text", "audio", "done"]
        assert ev[3]["text"] == "Отложил на 5 минут." and not a.sched.ringing()
        ws.send_bytes(b"\0\0" * 1600)                     # метка passive действует на одну реплику
        assert read_turn(ws)[1]["type"] == "transcript" and a.router.seen == ["Отложи на пять минут."]
    # пока звенело, распознаванию подсказывали слова команд; когда перестало — нет
    assert "Стоп" in a.speech.hints[0] and a.speech.hints[1] == a.speech.hints[0] and a.speech.hints[2] == ""


def test_ws_reminder_is_said_and_late_subscriber_catches_up_then_dismisses(tmp_path):
    a = Alerts(tmp_path)
    with a.client:
        with a.client.websocket_connect("/ws") as ws:
            a.connect(ws)
            reminder = a.fire("reminder", text="позвонить маме")
            assert ws.receive_json() == {"type": "say", "id": reminder, "text": "Напоминаю: позвонить маме", "chime": True}
            assert ws.receive_json()["alert"] == reminder and ws.receive_bytes() == "Напоминаю: позвонить маме.".encode()
        assert not a.svc.sessions                           # клиент ушёл — рассылать некому
        timer = a.fire()                                    # зазвенел, пока никого не было
        with a.client.websocket_connect("/ws") as ws:
            assert a.connect(ws, subscribe=False) == {"type": "pong"} and not a.svc.sessions  # старый клиент не звенит
            ws.send_json({"type": "subscribe"})
            assert ws.receive_json() == {"type": "ring", "id": timer, "kind": "timer", "text": "Таймер на 1 минуту"}
            ws.send_json({"type": "dismiss"})               # Enter в клиенте
            assert ws.receive_json() == {"type": "stop", "id": timer}
    assert not a.sched.ringing()


# --- обращение по имени в распознанном тексте --------------------------------------------------------

def test_wake_words_find_the_command_around_the_name():
    w = WakeWords(parse("джарвис, jarvis; окей дом"))
    assert w.phrases == ["джарвис", "jarvis", "окей дом"]
    assert WakeWords().match("Jarvis, привет") == "привет" and WakeWords().match("жаvis.") == ""  # латиница — по звучанию
    assert w.match("Джарвис, включи свет в спальне.") == "включи свет в спальне."
    assert w.match("Эй, Джарвис! Который час?") == "Который час?"
    assert w.match("Включи свет, Джарвис.") == "Включи свет"
    assert w.match("Скажи, Джарвис, сколько времени?") == "Скажи сколько времени?"
    assert w.match("Джарвис.") == "" and w.match("Слушай, Джарвис") == ""
    assert w.match("Окей, дом, выключи всё") == "выключи всё"
    assert w.match("Jarvis, поставь таймер на 10 минут.") == "поставь таймер на 10 минут."
    # распознавание слышит имя по-разному; на живой проверке было «Эй, джанрис!»
    for heard in ("Жарвис", "Джервис", "Джарвиз", "джанрис", "Джарвис,"):
        assert w.match(f"{heard} включи свет") == "включи свет", heard
    assert w.match("а потом он выключил свет и ушёл") is None and w.match("") is None
    assert w.match("Этот сервис включи завтра") is None and w.match("Угу.") is None
    # имя в середине чужой длинной фразы — не обращение
    assert w.match("А вчера в том фильме Джарвис сказал Тони, что костюм готов") is None
    assert close("дом", "дом") and not close("том", "дом")   # короткие слова — только точно


def test_short_wake_word_is_matched_by_sound_only():
    """Так распознавание писало «Клод» на проверке: совпадать должно звучание, а не соседние слова."""
    w = WakeWords(["клод"])
    for heard in ("Клод", "Клот", "Клотт", "Clod", "Кloт", "клод"):
        assert w.match(f"{heard}, который час?") == "который час?", heard
    assert w.match("Включи музыку Кloт.") == "Включи музыку" and w.match("Клот. Стоп.") == "Стоп."
    for other in ("Код", "Кот", "Плот", "Флот", "Вот", "Клон", "Клад", "Год"):
        assert w.match(f"{other}, который час?") is None, other
    assert norm("Клотт") == norm("клод") == "клот" and norm("Jarvis") == "джарвис" and norm("") == ""
    # то, что слышно в шуме, владелец добавляет сам отдельными словами
    assert WakeWords(parse("клод, плот, флот")).match("Плот, сделай потише.") == "сделай потише."
    assert WakeWords([]).match("Джарвис, привет") is None


class LocalHeard(Heard):
    local = True


def wake_app(speech, router):
    app = FastAPI()
    svc = VoiceService(router, speech, wake=WakeWords(["джарвис"]))

    @app.websocket("/ws")
    async def ep(ws: WebSocket):
        await ws.accept()
        await VoiceSession(ws, svc).run()

    return TestClient(app)


def test_ws_passive_phrase_is_answered_only_when_addressed():
    speech, router = LocalHeard("Джарвис, включи свет."), FakeRouter()
    with wake_app(speech, router).websocket_connect("/ws") as ws:
        assert ws.receive_json()["wake"] == ["джарвис"]
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        ev = read_turn(ws)
        assert ev[1] == {"type": "transcript", "turn": 1, "text": "Джарвис, включи свет.", "wake": True}
        assert [e["text"] for e in ev if isinstance(e, dict) and e["type"] == "text"] == ["Первая фраза.", "Вторая"]
        assert router.seen == ["включи свет."]             # модель получает команду без обращения
        speech.heard = "Джарвис!"
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        assert read_turn(ws) == [{"type": "start", "turn": 2}, {"type": "wake", "turn": 2}, {"type": "done", "turn": 2}]
        speech.heard = "ну и что ты будешь делать завтра"
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        assert [e["type"] for e in read_turn(ws)] == ["start", "ignored", "done"]
        speech.heard = ""                                 # шум без слов
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        assert [e["type"] for e in read_turn(ws)] == ["start", "done"]
    assert router.seen == ["включи свет."]


def test_ws_cloud_speech_does_not_listen_for_the_name():
    """Через облачное распознавание комнату постоянно не слушаем: слов активации в hello нет."""
    speech, router = Heard("Джарвис, включи свет."), FakeRouter()
    with wake_app(speech, router).websocket_connect("/ws") as ws:
        assert ws.receive_json()["wake"] == []
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        assert [e["type"] for e in read_turn(ws)] == ["start", "ignored", "done"]
    assert router.seen == []


def test_ws_stop_with_the_name_still_stops_the_alarm(tmp_path):
    a = Alerts(tmp_path, heard="Джарвис, стоп!")
    with a.client, a.client.websocket_connect("/ws") as ws:
        a.connect(ws)
        a.fire("alarm")
        ws.receive_json(), ws.receive_json(), ws.receive_bytes()
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        ev = read_turn(ws)
    assert [e["type"] for e in ev if isinstance(e, dict)] == ["start", "stop", "transcript", "text", "audio", "done"]
    assert not a.sched.ringing() and a.router.seen == []


def test_ws_stop_after_a_misheard_name_stops_without_model(tmp_path):
    a = Alerts(tmp_path, heard="Клот. Стоп.")
    a.speech.local = True
    a.svc.wake = WakeWords(["клод"])
    with a.client, a.client.websocket_connect("/ws") as ws:
        a.connect(ws)
        a.fire("alarm")
        ws.receive_json(), ws.receive_json(), ws.receive_bytes()
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
        ev = read_turn(ws)
    assert [e["type"] for e in ev if isinstance(e, dict)] == ["start", "transcript", "stop", "text", "audio", "done"]
    assert not a.sched.ringing() and a.router.seen == []


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


async def test_client_does_not_listen_on_when_core_says_music_will_play(conv):
    c, det, player, sent = conv
    await c.send_text("включи музыку")
    await c.on_message(json.dumps({"type": "done", "turn": 1, "followup": False}))
    await feed(c, det, ".")
    assert c.state == IDLE                       # иначе в «продолжение» попала бы сама музыка
    await c.send_text("привет")
    await c.on_message(json.dumps({"type": "done", "turn": 2}))
    await feed(c, det, ".")
    assert c.state == LISTENING                  # следующий обычный ответ — снова с продолжением


# --- клиент: обращение по имени без сигнала ---------------------------------------------------------

@pytest.fixture
def named():
    det, player, sent = Script(), FakePlayer(), []

    async def send(x):
        sent.append(x)

    cfg = Config(end_silence_ms=240, listen_timeout_ms=400, followup_ms=320, phrase_wake=True, passive_ms=160,
                 phrase_end_silence_ms=240)
    return Conversation(det, det, player, send, cfg, log=lambda *a: None), det, player, sent


async def test_client_sends_any_phrase_and_answers_only_when_core_heard_the_name(named):
    c, det, player, sent = named
    await feed(c, det, "..ss")                          # заговорили без сигнала
    assert c.state == LISTENING and c.passive and player.beeps == 0
    await feed(c, det, "sss...")
    assert c.state == WAITING and json.loads(sent[0]) == {"type": "passive"}
    assert len(sent[1]) == FRAME * 2 * 10                # фраза целиком, с началом: «Джарвис» не срезан
    await c.on_message(json.dumps({"type": "ignored", "turn": 1, "text": "просто разговор"}))
    await c.on_message(json.dumps({"type": "done", "turn": 1}))
    await feed(c, det, ".")
    assert c.state == IDLE
    await feed(c, det, "ss")                            # без звонка паузы после чужой фразы нет
    assert c.state == LISTENING and c.passive
    await feed(c, det, "s...")
    assert c.turn == 2
    await c.on_message(json.dumps({"type": "transcript", "turn": 2, "text": "Джарвис, включи свет", "wake": True}))
    await c.on_message(json.dumps({"type": "audio", "turn": 2, "bytes": 2}))
    await c.on_message(b"ok")
    assert player.buf == [b"ok"]
    await c.on_message(json.dumps({"type": "done", "turn": 2}))
    player.buf.clear()
    await feed(c, det, ".")
    assert c.state == LISTENING and not c.passive       # обратились — дальше разговор с продолжением


async def test_client_keeps_the_sound_before_a_phrase_and_never_sends_it_twice(named):
    """Имя стоит в начале фразы, а детектор речи замечает её с опозданием: «Джарвис» не должен срезаться."""
    c, det, player, sent = named
    await feed(c, det, "." * 30 + "ss")
    assert len(c.utterance) == 20                         # 1,6 с до срабатывания, включая начало фразы
    await feed(c, det, "...")
    assert c.state == WAITING and len(sent[1]) == FRAME * 2 * 23
    await feed(c, det, "..")                              # ядро ещё разбирает фразу — звук копится
    await c.on_message(json.dumps({"type": "done", "turn": 1}))
    await feed(c, det, ".ss")
    assert c.state == LISTENING and len(c.utterance) == 5  # только новое: прежняя фраза не приклеилась


async def test_client_beeps_and_listens_when_only_the_name_was_said(named):
    c, det, player, sent = named
    await feed(c, det, "ss...")
    await c.on_message(json.dumps({"type": "wake", "turn": 1}))
    assert c.state == LISTENING and not c.passive and player.beeps == 1
    await c.on_message(json.dumps({"type": "done", "turn": 1}))
    await feed(c, det, "sss...")
    assert c.state == WAITING and c.turn == 2
    # вторая запись — уже обычная реплика: метка passive стоит только перед первой
    sent = [x for x in sent if isinstance(x, bytes) or json.loads(x)["type"] != "warm"]  # прогрев — отдельно
    assert [x if isinstance(x, bytes) else json.loads(x)["type"] for x in sent][::2] == ["passive", sent[2]]
    assert len(sent) == 3 and isinstance(sent[1], bytes)


async def test_client_does_not_take_a_spoken_reminder_for_a_phrase(named):
    c, det, player, sent = named
    player.play(b"napominanie")                         # ядро говорит напоминание, микрофон его слышит
    await feed(c, det, "ssss")
    assert c.state == IDLE and not sent
    player.buf.clear()
    await feed(c, det, "ss")
    assert c.state == LISTENING


async def test_client_without_phrase_wake_ignores_plain_speech(conv):
    c, det, _, sent = conv
    await feed(c, det, "ssss....")
    assert c.state == IDLE and not sent


# --- клиент: звонок и «стоп» без слова активации -------------------------------------------------

class FakeRing:
    rate = 1  # два байта — секунда: удобно считать, когда звук доиграет

    def cycle(self, kind, elapsed):
        return f"{kind}@{elapsed:.0f}".encode()

    def chime(self):
        return b"chime"


@pytest.fixture
def ringer():
    det, player, sent, now = Script(), FakePlayer(), [], [100.0]

    async def send(x):
        sent.append(x)

    c = Conversation(det, det, player, send, Config(followup_ms=320), log=lambda *a: None, ring=FakeRing(),
                     clock=lambda: now[0])
    return c, det, player, sent, now


RING = json.dumps({"type": "ring", "id": 7, "kind": "alarm", "text": "Будильник на 7:00"})


async def test_client_rings_in_a_loop_until_core_says_stop(ringer):
    c, det, player, sent, now = ringer
    await c.on_message(RING)
    await feed(c, det, "..")
    assert player.buf == [b"alarm@0"]                      # сигнал повторяется, когда доиграл, а не каждый кадр
    await c.on_message(json.dumps({"type": "audio", "alert": 7, "bytes": 3}))
    await c.on_message(b"pcm")                             # фраза «Будильник на 7 часов» — следом за сигналом
    assert player.buf == [b"alarm@0", b"pcm"]
    player.buf.clear()
    now[0] += 12
    await feed(c, det, ".")
    assert player.buf == [b"alarm@12"] and c.state == IDLE  # громкость растёт со временем
    await c.dismiss()                                      # Enter
    assert json.loads(sent[-1]) == {"type": "dismiss"} and c.ringing
    await c.on_message(json.dumps({"type": "stop", "id": 7}))
    assert not c.ringing and not player.buf
    await feed(c, det, "...")
    assert not player.buf and c.state == IDLE


async def test_client_hears_stop_without_wake_word_while_ringing(ringer):
    c, det, player, sent, now = ringer
    await feed(c, det, "sss")
    assert c.state == IDLE and not sent                    # ничего не звенит — речь без wake word не слушаем
    await c.on_message(RING)
    await feed(c, det, ".sssss")
    assert c.state == IDLE and player.buf                  # первые секунды звонок должен просто прозвучать
    now[0] += 2.1
    await feed(c, det, ".ss")
    assert c.state == IDLE and player.buf                  # коротко — ещё звеним
    await feed(c, det, ".sss")
    assert c.state == LISTENING and c.passive and not player.buf  # звонок смолк, слушаем
    await feed(c, det, "s.......")
    assert c.state == WAITING and json.loads(sent[-2]) == {"type": "passive"} and isinstance(sent[-1], bytes)
    assert len(sent[-1]) == FRAME * 2 * (4 + 1 + 7)        # преролл с началом слова + речь + тишина
    assert c.passive_misses == 0
    await c.on_message(json.dumps({"type": "stop", "id": 7}))
    await c.on_message(json.dumps({"type": "text", "turn": 1, "text": "Выключил."}))
    await c.on_message(json.dumps({"type": "audio", "turn": 1, "bytes": 2}))
    await c.on_message(b"ok")
    assert player.buf == [b"ok"] and not c.ringing         # ответ звучит, stop его не обрывает
    await c.on_message(json.dumps({"type": "done", "turn": 1}))
    player.buf.clear()
    await feed(c, det, "..")
    assert c.state == IDLE and not c.passive and not player.buf   # продолжения разговора не ждём


async def test_client_resumes_ringing_after_noise_and_ignores_it_longer_each_time(ringer):
    c, det, player, sent, now = ringer
    await c.on_message(RING)
    now[0] += 2.1

    async def noise(turn):                                 # кто-то говорит рядом, ядро молчит: не команда
        await feed(c, det, "sss.......")
        assert c.state == WAITING and c.passive
        await c.on_message(json.dumps({"type": "done", "turn": turn}))
        await feed(c, det, ".")
        assert c.state == IDLE and c.ringing

    await noise(1)
    await feed(c, det, ".sssss")
    assert c.state == IDLE and player.buf                  # пауза: шум не должен глушить будильник
    now[0] += 3.1
    await noise(2)
    now[0] += 3.1
    await feed(c, det, "sssss")
    assert c.state == IDLE and player.buf                  # второй промах подряд — пауза уже 9 секунд
    now[0] += 6
    await feed(c, det, "sss")
    assert c.state == LISTENING and c.passive
    await feed(c, det, "w")
    assert c.state == LISTENING and not c.passive and player.beeps == 1  # позвали по имени — обычная реплика


async def test_client_microphone_stuck_on_speech_does_not_silence_the_alarm(ringer):
    """Нашлось на живой проверке: микрофон в клиппинге, детектор всё время слышит «речь» — и звонок молчал."""
    c, det, player, sent, now = ringer
    await c.on_message(RING)
    silent = turns = 0
    start = now[0]
    while now[0] - start < 120:                            # две минуты кадров, и каждый — «речь»
        now[0] += 0.08
        await feed(c, det, "s")
        player.buf.clear()                                 # очередной сигнал «доиграл»
        if c.state != IDLE:                                # пока идёт запись и распознавание, звонок молчит
            silent += 0.08
        if c.state == WAITING:                             # ядро распознаёт около 2,5 с и молчит: не команда
            turns += 1
            now[0] += 2.5
            silent += 2.5
            await c.on_message(json.dumps({"type": "done", "turn": turns}))
            await feed(c, det, "s")
    assert 3 <= turns <= 5 and silent / (now[0] - start) < 0.25   # звенит не меньше трёх четвертей времени


async def test_client_does_not_take_its_own_phrase_for_a_command(ringer):
    c, det, player, sent, now = ringer
    await c.on_message(RING)
    now[0] += 2.1
    await feed(c, det, ".")                                # сигнал b"alarm@2" — 3,5 «секунды»
    await c.on_message(json.dumps({"type": "audio", "alert": 7, "bytes": 4}))
    await c.on_message(b"pcm!")                            # фраза — ещё 2 секунды после сигнала
    await feed(c, det, "sssss")
    assert c.state == IDLE                                 # это Jarvis говорит из колонок, а не команда
    now[0] += 5.9
    await feed(c, det, "sss")
    assert c.state == LISTENING and c.passive


async def test_client_says_reminder_with_chime(ringer):
    c, det, player, sent, now = ringer
    await c.on_message(json.dumps({"type": "say", "id": 3, "text": "Напоминаю: чайник", "chime": True}))
    await c.on_message(json.dumps({"type": "audio", "alert": 3, "bytes": 2}))
    await c.on_message(b"tt")
    assert player.buf == [b"chime", b"tt"] and c.state == IDLE and not c.ringing


def test_ring_sound_alarm_grows_and_custom_file_replaces_it(tmp_path):
    import wave

    ring, logs = RingSound(16000), []
    level = lambda pcm: int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max())  # noqa: E731
    quiet, mid, loud = (level(ring.cycle("alarm", t)) for t in (0, 10, 60))
    assert quiet < mid < loud and loud == level(ring.cycle("alarm", 600)) and quiet < loud * 0.35
    assert level(ring.cycle("timer", 0)) == level(ring.cycle("timer", 60)) > 0
    assert 0 < len(ring.chime()) < 16000

    path = tmp_path / "alarm.wav"
    with wave.open(str(path), "wb") as f:                  # стерео 8 кГц, полсекунды
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes((np.ones(8000, dtype=np.int16) * 5000).tobytes())
    plain = load_sound(str(path), 16000, logs.append, which=lambda _: None)
    assert plain.size == 8000 + 8000 and plain[:8000].max() == pytest.approx(5000)   # 0,5 с звука + пауза
    assert load_sound(str(tmp_path / "нет.wav"), 16000, logs.append, which=lambda _: None) is None
    assert "не загружен" in logs[0]
    custom = RingSound(16000, str(path), logs.append)
    assert custom.sounds["alarm"].size >= 16000 and custom.sounds["timer"].size == ring.sounds["timer"].size
    assert RingSound(16000, str(tmp_path / "нет.wav"), logs.append).sounds["alarm"].size == ring.sounds["alarm"].size


def tone(amp: int, n: int = 10, hz: int = 300) -> list:
    t = np.arange(FRAME * n) / 16000
    return list((np.sin(2 * np.pi * hz * t) * amp).astype(np.int16).reshape(n, FRAME))


def test_mic_check_names_the_problem():
    quiet, voice = tone(20), tone(6000)
    clipped = [np.full(FRAME, 32767, dtype=np.int16)] * 10      # так выглядел микрофон на живой проверке
    stuck = level_stats(clipped, [0.9] * 10)
    assert (stuck["rms"], stuck["peak"], stuck["clip"], stuck["speech"]) == (32767.0, 32767, 100.0, 100.0)
    assert stuck["loud"] < 1                                    # постоянный уровень — не звук
    ok_noise, ok_speech = level_stats(quiet, [0.01] * 10), level_stats(voice, [0.9] * 8 + [0.1] * 2)
    assert ok_noise["speech"] == 0 and ok_speech["speech"] == 80 and mic_verdict(ok_noise, ok_speech) == []
    assert 250 < voice_ratio(ok_noise, ok_speech) < 350
    bad = mic_verdict(stuck, stuck)
    assert "Перегруз" in bad[0] and "Фон принимается за речь" in bad[1]
    assert "Речь не обнаружена" in mic_verdict(ok_noise, level_stats(voice, [0.0] * 10))[0]
    assert "тихо" in mic_verdict(ok_noise, level_stats(tone(400), [0.9] * 10))[0]
    # Так было на встроенном микрофоне: фон 1800, голос издалека чуть громче — усилением не лечится.
    far = mic_verdict(level_stats(tone(2500), [0.1] * 10), level_stats(tone(4000), [0.3] * 10))
    assert len(far) == 1 and "едва громче фона (в 1.6 раза)" in far[0] and "ближе" in far[0]
    # Гул и постоянная составляющая в отношение не попадают.
    hum = [f + 9000 for f in tone(20)]
    assert abs(level_stats(hum, [0.0] * 10)["floor"] - ok_noise["floor"]) < 2
    empty = level_stats([], [])
    assert (empty["rms"], empty["peak"], empty["loud"], empty["speech"]) == (0.0, 0, 0.0, 0.0)


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


def test_client_reconnects_until_token_rejected():
    from websockets.exceptions import ConnectionClosedError, InvalidStatus

    from jarvis.voice_client import RETRY_S, keep_connected

    script = [ConnectionRefusedError("отказано"), OSError("сеть"), "ok", ConnectionClosedError(None, None),
              None, InvalidStatus(NS(status_code=401))]
    log, pauses = [], []

    async def session(connected):
        step = script.pop(0)
        if step == "ok":       # подключились, потом ядро перезапустили
            connected()
            raise ConnectionError("ядро закрыло соединение")
        if step is not None:
            raise step

    async def sleep(delay):
        pauses.append(delay)

    with pytest.raises(SystemExit, match="JARVIS_API_TOKEN"):
        asyncio.run(keep_connected(session, sleep=sleep, log=log.append))
    # паузы растут, а после удачного подключения отсчёт начинается заново
    assert pauses == [RETRY_S[0], RETRY_S[1], RETRY_S[0], RETRY_S[1], RETRY_S[2]]
    assert "отказано" in log[0] and len(log) == 5 and not script
