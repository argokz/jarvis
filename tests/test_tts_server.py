import pytest
from fastapi.testclient import TestClient

from jarvis.speech import wav_to_pcm
from jarvis.tts_server import create_app, normalize, number, ordinal, split


@pytest.mark.parametrize("n, words", [
    (0, "ноль"), (7, "семь"), (13, "тринадцать"), (21, "двадцать один"), (100, "сто"), (110, "сто десять"),
    (305, "триста пять"), (1000, "тысяча"), (2002, "две тысячи два"), (21000, "двадцать одна тысяча"),
    (5_000_000, "пять миллионов"), (1_234_567, "один миллион двести тридцать четыре тысячи пятьсот шестьдесят семь"),
])
def test_number_in_words(n, words):
    assert number(n) == words


@pytest.mark.parametrize("n, case, word", [
    (1, "nom", "первый"), (2, "nom", "второй"), (3, "nom", "третий"), (3, "gen", "третьего"), (3, "neut", "третье"),
    (23, "prep", "двадцать третьем"), (40, "nom", "сороковой"), (100, "gen", "сотого"), (1900, "prep", "тысяча девятисотом"),
    (2000, "nom", "двухтысячный"), (2026, "ins", "две тысячи двадцать шестым"), (0, "nom", None), (5000, "nom", None),
])
def test_ordinal(n, case, word):
    assert ordinal(n, case) == word


def test_number_agrees_with_feminine_noun():
    assert number(1, feminine=True) == "одна" and number(22, feminine=True) == "двадцать две"
    assert number(12, feminine=True) == "двенадцать"
    assert number(10 ** 12) == "один" + " ноль" * 12  # слишком длинное — по цифрам


@pytest.mark.parametrize("text, spoken", [
    ("Таймер на 10 минут.", "Таймер на десять минут."),
    ("Осталась 1 минута и 2 секунды, 21 задача.", "Осталась одна минута и две секунды, двадцать одна задача."),
    ("Сейчас 14:30, будильник на 7:05 и 07:00.",
     "Сейчас четырнадцать тридцать, будильник на семь ноль пять и семь ноль ноль."),
    ("Громкость 25%, заряд 1 %.", "Громкость двадцать пять процентов, заряд один процент."),
    ("На улице -5°C, дома 22,5 °C.", "На улице минус пять градусов, дома двадцать два и пять градуса."),
    ("Это 1 500 ₽ или 20$.", "Это тысяча пятьсот рублей или двадцать долларов."),
    ("Ход стоил 3,5 цента, точнее 0,05.", "Ход стоил три и пять цента, точнее ноль и ноль пять."),
    ("Версия 3.15.2.", "Версия три точка пятнадцать точка два."),
    ("Код 007, 2+2=4.", "Код ноль ноль семь, два плюс два равно четыре."),
    ("Через 10-15 минут, 24/7.", "Через десять-пятнадцать минут, двадцать четыре семь."),
    ("Сегодня 19 ноября, встреча 3 декабря.", "Сегодня девятнадцатое ноября, встреча третьего декабря."),
    ("С 23 февраля по 8 марта, 31 мая.", "С двадцать третьего февраля по восьмое марта, тридцать первого мая."),
    ("Купи 5 мартовских открыток.", "Купи пять мартовских открыток."),
    ("Рим основан в 753 году, Мадрид — около 865 года.",
     "Рим основан в семьсот пятьдесят третьем году, Мадрид — около восемьсот шестьдесят пятого года."),
    ("Сейчас 2026 год, к 2030 году, с 2000 года.",
     "Сейчас две тысячи двадцать шестой год, к две тысячи тридцатому году, с двухтысячного года."),
    ("Прошло 2 года, 21 год и 5 лет.", "Прошло два года, двадцать один год и пять лет."),   # срок, а не дата
    ("В IX веке, XXI век, к XIX веку.", "В девятом веке, двадцать первый век, к девятнадцатому веку."),
    ("Включил YouTube и Wi-Fi на USB.", "Включил ютуб и вай-фай на ю эс би."),
    ("Открыл Home Assistant и Chrome.", "Открыл хоум ассистант и хром."),
    ("Прив+ет,   з+амок\nи зам+ок.", "Прив+ет, з+амок и зам+ок."),   # «+» — знак ударения, остаётся
])
def test_normalize_makes_text_speakable(text, spoken):
    assert normalize(text) == spoken


def test_normalize_leaves_no_digits_or_latin():
    import re

    text = normalize("Заказ №12 от user@mail.com: 3 шт. по $4.99, скидка 10%, GPU GTX 1660 Ti & 6 GB, +7 900 123-45-67.")
    assert not re.search(r"[0-9A-Za-z№@$%&]", text), text


def test_split_keeps_sentences_and_cuts_long_ones():
    assert split("Раз. Два! Три?", 100) == ["Раз. Два! Три?"]
    assert split("Раз. Два! Три?", 8) == ["Раз.", "Два!", "Три?"]
    parts = split("слово, " * 30 + "конец.", 50)
    assert all(len(p) <= 50 for p in parts) and " ".join(parts) == ("слово, " * 30 + "конец.")
    assert split("...  — !", 50) == [] and split("", 50) == []      # произносить нечего
    assert split("а" * 120, 50) == ["а" * 50, "а" * 50, "а" * 20]   # без пробелов режем как есть


class FakeEngine:
    name, rate, voice, limit = "fake", 24000, "xenia", 25

    def __init__(self):
        self.calls = []

    def voices(self):
        return ["xenia", "aidar"]

    def speak(self, text, voice):
        self.calls.append((text, voice))
        if "сбой" in text:
            raise RuntimeError("модель упала")
        return b"\x01\x00" * len(text)


def test_speech_endpoint_returns_wav_of_prepared_text():
    engine = FakeEngine()
    client = TestClient(create_app(engine))
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "Таймер на 5 минут. Готово!"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-sample-rate"] == "24000" and float(r.headers["x-took"]) >= 0
    assert engine.calls == [("Таймер на пять минут.", "xenia"), ("Готово!", "xenia")]   # по кускам до limit
    assert wav_to_pcm(r.content) == (24000, b"\x01\x00" * len("Таймер на пять минут.Готово!"))
    r = client.post("/v1/audio/speech", json={"input": "Да", "voice": "aidar", "response_format": "pcm"})
    assert r.content == b"\x01\x00" * 2 and engine.calls[-1] == ("Да", "aidar")
    assert client.get("/health").json() == {"ok": True, "engine": "fake", "rate": 24000, "voice": "xenia",
                                            "voices": ["xenia", "aidar"]}


def test_speech_endpoint_rejects_bad_requests():
    engine = FakeEngine()
    client = TestClient(create_app(engine))
    r = client.post("/v1/audio/speech", json={"input": "Привет", "voice": "nobody"})
    assert r.status_code == 400 and "xenia, aidar" in r.json()["detail"]
    assert client.post("/v1/audio/speech", json={"input": "Привет", "response_format": "mp3"}).status_code == 400
    assert client.post("/v1/audio/speech", content=b"not json").status_code == 422
    assert client.post("/v1/audio/speech", json=["Привет"]).status_code == 422
    r = client.post("/v1/audio/speech", json={"input": "Тут сбой."})
    assert r.status_code == 500 and "модель упала" in r.json()["detail"]
    assert not [c for c in engine.calls if c[0] == "Привет"]


def test_speech_endpoint_answers_silence_when_nothing_to_say():
    engine = FakeEngine()
    r = TestClient(create_app(engine)).post("/v1/audio/speech", json={"input": " … 🙂 "})
    assert r.status_code == 200 and wav_to_pcm(r.content) == (24000, bytes(4800)) and not engine.calls
