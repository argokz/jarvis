"""Слова активации по распознанному тексту: «Джарвис, включи свет».

Клиент присылает каждую услышанную фразу, ядро распознаёт её своим STT и отвечает, только если в
ней есть обращение. Фразы задаются в `JARVIS_WAKE_WORDS` — обучать модель под новое слово не нужно.
"""
from __future__ import annotations

import re

DEFAULT = ("джарвис",)
# Слова перед обращением, которые в команду не входят: «эй, Джарвис», «слушай, Джарвис».
FILLER = frozenset({"эй", "хей", "ок", "окей", "слушай", "послушай", "привет", "ну", "а", "и", "hey", "ok", "okay"})
MAX_BEFORE = 2  # «скажи, Джарвис, который час» — обращение; имя в середине длинной чужой фразы — нет
_TRIM = " \t\n,.!?…:;—–-«»\"'"


def distance(a: str, b: str) -> int:
    """Расстояние Левенштейна: сколько букв заменить, вставить или убрать."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


_LATIN = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
          "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
          "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з"}
_DEVOICE = {"б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с"}


def norm(word: str) -> str:
    """Слово так, как оно звучит: распознавание пишет «Клод» как «Клот», «Клотт», «Clod» и даже «Кloт»."""
    w = "".join(_LATIN.get(c, c) for c in word.lower().replace("ё", "е"))
    w = re.sub(r"(.)\1+", r"\1", w)                      # «клотт» → «клот»
    return w[:-1] + _DEVOICE.get(w[-1], w[-1]) if w else w  # на конце слова звонкая звучит глухо


def close(word: str, target: str) -> bool:
    """Длинное имя сравнивается с допуском («Жарвис», «Джервис»), короткое — только по звучанию.

    У слова короче пяти букв в одной букве от него слишком много обычных слов: «клод» — «код», «кот».
    """
    a, b = norm(word), norm(target)
    return distance(a, b) <= (0 if len(b) < 5 else 1 if len(b) < 7 else 2)


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def parse(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace(";", ",").split(",") if words(p)]


class WakeWords:
    def __init__(self, phrases=DEFAULT):
        self.phrases = [p.strip() for p in phrases if words(p)]
        self._tokens = [words(p) for p in self.phrases]

    def match(self, text: str) -> str | None:
        """None — к ассистенту не обращались; "" — только позвали; иначе — команда без обращения."""
        found = [(m.group().lower().replace("ё", "е"), m.start(), m.end()) for m in re.finditer(r"\w+", text)]
        for i in range(len(found)):
            for phrase in self._tokens:
                part = found[i:i + len(phrase)]
                if len(part) != len(phrase) or not all(close(w[0], p) for w, p in zip(part, phrase)):
                    continue
                before = [w for w in found[:i] if w[0] not in FILLER]
                head = text[before[0][1]:found[i][1]].strip(_TRIM) if before else ""
                tail = text[part[-1][2]:].lstrip(_TRIM).rstrip()
                if not re.search(r"\w", tail):
                    return head            # «включи свет, Джарвис» или просто «Джарвис»
                if len(before) <= MAX_BEFORE:
                    return f"{head} {tail}".strip()
        return None
