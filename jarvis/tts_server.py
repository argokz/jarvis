"""Свой синтез речи за интерфейсом OpenAI `/v1/audio/speech`.

Piper быстрый, но звучит синтетически. Здесь — нейросетевые голоса; ядро ходит сюда так же, как в
любой OpenAI-совместимый сервер синтеза: `JARVIS_TTS_URL=http://127.0.0.1:5007/v1/audio/speech`.

Движки (у каждого своё окружение, см. README):
  silero  — Silero v5, русская модель на процессоре: фраза за доли секунды, пять готовых голосов.
  espeech — ESpeech-TTS (F5-TTS) на видеокарте: говорит голосом из образца `имя.wav` + `имя.txt`.

Модели читают только русские буквы, поэтому текст сначала готовится: числа, время и знаки —
словами, латиница — русскими буквами (см. normalize).

Запуск:
    python -m jarvis.tts_server --engine silero --voice xenia
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
import types
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response

from .speech import pcm_to_wav

ONES = ("ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать "
        "четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать").split()
TENS = "- - двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split()
HUNDREDS = "- сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот".split()
GROUPS = ((), ("тысяча", "тысячи", "тысяч"), ("миллион", "миллиона", "миллионов"),
          ("миллиард", "миллиарда", "миллиардов"))
# Слова женского рода, которые ассистент чаще всего считает: «одна минута», «две задачи».
FEMININE = ("минут", "секунд", "недел", "тысяч", "задач", "заметк", "копе", "страниц", "строк", "штук", "песн")
UNITS = {"%": ("процент", "процента", "процентов"), "°": ("градус", "градуса", "градусов"),
         "₽": ("рубль", "рубля", "рублей"), "$": ("доллар", "доллара", "долларов"), "€": ("евро",) * 3}
SYMBOLS = {"%": " процентов ", "°": " градусов ", "№": " номер ", "&": " и ", "=": " равно ", "₽": " рублей ",
           "$": " долларов ", "€": " евро ", "@": " собака ", "/": " "}
# Слова, которые по общим правилам читаются неверно.
WORDS = {"jarvis": "джарвис", "claude": "клод", "youtube": "ютуб", "google": "гугл", "python": "пайтон",
         "wifi": "вай фай", "wi": "вай", "fi": "фай", "bluetooth": "блютус", "chrome": "хром", "home": "хоум",
         "github": "гитхаб", "windows": "виндоус", "notion": "ноушен", "online": "онлайн", "ok": "окей",
         "email": "имейл", "iphone": "айфон", "xiaomi": "сяоми", "piper": "пайпер", "haiku": "хайку",
         "api": "эй пи ай", "id": "ай ди", "smart": "смарт", "life": "лайф", "music": "мьюзик"}
# Порядковые: основа по последней значащей части числа, окончание — по падежу.
ORDINALS = {1: "перв", 2: "втор", 3: "трет", 4: "четвёрт", 5: "пят", 6: "шест", 7: "седьм", 8: "восьм",
            9: "девят", 10: "десят", **{n: ONES[n][:-1] for n in range(11, 20)},
            20: "двадцат", 30: "тридцат", 40: "сороков", 50: "пятидесят", 60: "шестидесят", 70: "семидесят",
            80: "восьмидесят", 90: "девяност", 100: "сот", 200: "двухсот", 300: "трёхсот", 400: "четырёхсот",
            500: "пятисот", 600: "шестисот", 700: "семисот", 800: "восьмисот", 900: "девятисот",
            1000: "тысячн", 2000: "двухтысячн"}
ENDINGS = {"nom": "ый", "neut": "ое", "gen": "ого", "prep": "ом", "dat": "ому", "ins": "ым"}
ENDINGS_3 = {"nom": "ий", "neut": "ье", "gen": "ьего", "prep": "ьем", "dat": "ьему", "ins": "ьим"}  # третий
CASES = {"": "nom", "а": "gen", "у": "prep", "е": "prep", "ом": "ins"}  # по окончанию слов «год», «век»
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
TODAY = ("сегодня", "завтра", "послезавтра", "вчера", "позавчера", "число", "по", "на")
LETTERS = dict(a="эй", b="би", c="си", d="ди", e="и", f="эф", g="джи", h="эйч", i="ай", j="джей", k="кей",
               l="эл", m="эм", n="эн", o="оу", p="пи", q="кью", r="ар", s="эс", t="ти", u="ю", v="ви",
               w="дабл ю", x="икс", y="уай", z="зед")
SOUNDS = [("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ck", "к"), ("oo", "у"), ("ee", "и"),
          ("qu", "кв"), ("ya", "я"), ("yu", "ю"),
          *zip("abcdefghijklmnopqrstuvwxyz", "а б к д е ф г х и дж к л м н о п к р с т у в в кс и з".split())]

_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?![\d ])")
_DAY = re.compile(rf"(?:([а-яё]+)\s+)?(?<![\d.,:-])([12]?\d|3[01])(?=\s+(?:{MONTHS.replace(' ', '|')})(?![а-яё]))", re.I)
_YEAR = re.compile(r"(?:\b([а-яё]+)\s+)?(?<![\d.,:-])(\d{1,4})(\s+год(|а|у|ом))(?![а-яё])", re.I)
_CENTURY = re.compile(r"(?:\b([а-яё]+)\s+)?\b([IVXLC]+)(\s+век(|а|у|е|ом))(?![а-яё])")
_UNIT = re.compile(r"(\d+)([.,]\d+)?\s?([%°₽$€])\s?[CС]?(?![а-яёa-z])", re.I)
_PLUS = re.compile(r"(?<![а-яёa-z])\+\s?(?=\d)", re.I)  # «+» после буквы — знак ударения, его не трогаем
_MINUS = re.compile(r"(?<![\w.,])[-−–](?=\d)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_LATIN = re.compile(r"[A-Za-z]+")
_SPEAKABLE = re.compile(r"[а-яё]", re.I)
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def plural(n: int, forms: tuple[str, ...]) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _triple(n: int, feminine: bool) -> list[str]:
    words = [HUNDREDS[n // 100]] if n >= 100 else []
    n %= 100
    if n >= 20:
        words.append(TENS[n // 10])
        n %= 10
    if n:
        words.append({1: "одна", 2: "две"}[n] if feminine and n < 3 else ONES[n])
    return words


def number(n: int, feminine: bool = False) -> str:
    """Целое число словами: 21 → «двадцать один», feminine — «двадцать одна»."""
    if n == 0:
        return ONES[0]
    if n >= 10 ** 12:
        return digits(str(n))
    words: list[str] = []
    for group in range(4):
        n, part = divmod(n, 1000)
        if part:
            chunk = _triple(part, feminine if group == 0 else group == 1)
            words[:0] = chunk + [plural(part, GROUPS[group])] if group else chunk
    return " ".join(words[1:] if words[:2] == ["одна", "тысяча"] else words)  # «тысяча пятьсот»


def digits(s: str) -> str:
    return " ".join(ONES[int(d)] for d in s)


def _time(m: re.Match) -> str:
    minutes = int(m[2])
    return f"{number(int(m[1]))} {digits(m[2]) if minutes < 10 else number(minutes)}"


def ordinal(n: int, case: str) -> str | None:
    """Порядковое числительное: 2026, «prep» → «две тысячи двадцать шестом». None — если не умеем."""
    last = next((n % m for m in (100, 10, 1000, 10000) if n % m in ORDINALS), 0)
    if not last:
        return None
    ending = ENDINGS_3[case] if last == 3 else "ой" if case == "nom" and last in (2, 6, 7, 8, 40) else ENDINGS[case]
    return f"{number(n - last)} {ORDINALS[last]}{ending}" if n > last else ORDINALS[last] + ending


def _day(m: re.Match) -> str:
    """Число перед месяцем: «встреча 19 ноября» → «девятнадцатого», «сегодня 19 ноября» → «девятнадцатое»."""
    word = ordinal(int(m[2]), "neut" if (m[1] or "").lower() in TODAY else "gen")
    return f"{m[1] or ''} {word}".strip() if word else m[0]


def _year(m: re.Match) -> str:
    """«в 1237 году» → «в тысяча двести тридцать седьмом году»; «2 года», «21 год» — срок, остаются как есть."""
    n, form, before = int(m[2]), m[4].lower(), (m[1] or "").lower()
    countable = {"": n % 10 == 1 and n % 100 != 11, "а": n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)}
    if n < 1000 and countable.get(form, False):
        return m[0]
    word = ordinal(n, "dat" if form == "у" and before in ("к", "ко") else CASES[form])
    return f"{m[1] or ''} {word}{m[3]}".strip() if word else m[0]


def _century(m: re.Match) -> str:
    values = [ROMAN[c] for c in m[2]]
    n = sum(-v if v < nxt else v for v, nxt in zip(values, values[1:] + [0]))
    form, before = m[4], (m[1] or "").lower()
    word = ordinal(n, "dat" if form == "у" and before in ("к", "ко") else CASES[form])
    return f"{m[1] or ''} {word}{m[3]}".strip() if word else m[0]


def _unit(m: re.Match) -> str:
    forms = UNITS[m[3]]
    return f"{m[1]}{m[2] or ''} {forms[1] if m[2] else plural(int(m[1]), forms)} "


def _number(m: re.Match) -> str:
    parts = re.split("[.,]", m[0])
    if len(parts) == 1:
        s = parts[0]
        if len(s) > 1 and s[0] == "0":
            return digits(s)  # «007», код или номер: читаем по цифрам
        after = re.match(r"\s*([а-яё]+)", m.string[m.end():], re.I)
        return number(int(s), bool(after) and after[1].lower().startswith(FEMININE))
    if len(parts) == 2:  # дробь: «2,5» → «два и пять», «2,05» → «два и ноль пять»
        return f"{number(int(parts[0]))} и {digits(parts[1]) if parts[1][0] == '0' else number(int(parts[1]))}"
    return " точка ".join(number(int(p)) for p in parts)  # версия: «3.15.2»


def _latin(m: re.Match) -> str:
    word = m[0]
    if word.lower() in WORDS:
        return WORDS[word.lower()]
    if word.isupper() and len(word) <= 5 or len(word) == 1:
        return " ".join(LETTERS[c] for c in word.lower())  # сокращение: «USB» → «ю эс би»
    out, rest = [], word.lower()
    while rest:
        for latin, russian in SOUNDS:
            if rest.startswith(latin):
                out.append(russian)
                rest = rest[len(latin):]
                break
    return "".join(out)


def normalize(text: str) -> str:
    """Текст → то, что модель сможет произнести: только русские слова и знаки препинания."""
    text = _TIME.sub(_time, text)
    text = _DAY.sub(_day, text)
    text = _YEAR.sub(_year, text)
    text = _CENTURY.sub(_century, text)
    text = _THOUSANDS.sub(lambda m: re.sub(r"\s", "", m[0]), text)
    text = _UNIT.sub(_unit, text)
    text = _PLUS.sub(" плюс ", text)
    text = _MINUS.sub("минус ", text)
    text = _NUMBER.sub(_number, text)
    for symbol, word in SYMBOLS.items():
        text = text.replace(symbol, word)
    text = _LATIN.sub(_latin, text)
    return re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"\s+", " ", text)).strip()


def split(text: str, limit: int) -> list[str]:
    """Режет текст на куски не длиннее limit: по предложениям, а длинное предложение — по словам."""
    parts: list[str] = []
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:
            cut = max(sentence.rfind(", ", 0, limit) + 1, sentence.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if parts and len(parts[-1]) + len(sentence) < limit:
            parts[-1] = f"{parts[-1]} {sentence}"
        elif sentence:
            parts.append(sentence)
    return [p for p in parts if _SPEAKABLE.search(p)]


def create_app(engine) -> FastAPI:
    """engine: name, rate, voice, limit, voices() и speak(text, voice) -> PCM16 моно; в тестах подменяется."""
    app = FastAPI(title="Jarvis TTS")
    lock = asyncio.Lock()  # модель одна: фразы синтезируются по очереди

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "engine": engine.name, "rate": engine.rate, "voice": engine.voice,
                "voices": engine.voices()}

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        try:
            body = await request.json()
            text = normalize(str(body.get("input") or ""))
        except (ValueError, AttributeError) as e:
            raise HTTPException(422, "нужен JSON с полем input") from e
        voice = str(body.get("voice") or engine.voice)  # model принимается, но движок здесь один
        if voice not in engine.voices():
            raise HTTPException(400, f"нет голоса «{voice}»; есть: {', '.join(engine.voices())}")
        fmt = body.get("response_format") or "wav"
        if fmt not in ("wav", "pcm"):
            raise HTTPException(400, "response_format: только wav или pcm")
        started = time.monotonic()
        async with lock:
            try:
                pcm = b"".join([await asyncio.to_thread(engine.speak, part, voice)
                                for part in split(text, engine.limit)])
            except Exception as e:  # noqa: BLE001 — любая ошибка модели должна дойти до ядра текстом
                raise HTTPException(500, f"синтез не удался: {type(e).__name__}: {e}") from e
        pcm = pcm or bytes(engine.rate // 5)  # произносить нечего — 0,1 с тишины
        headers = {"X-Sample-Rate": str(engine.rate), "X-Took": f"{time.monotonic() - started:.3f}"}
        if fmt == "pcm":
            return Response(pcm, media_type="audio/pcm", headers=headers)
        return Response(pcm_to_wav(pcm, engine.rate), media_type="audio/wav", headers=headers)

    return app


class Silero:
    """Silero v5 на процессоре. Ударения и «ё» модель расставляет сама; «+» перед гласной задаёт ударение."""
    name, limit = "silero", 800

    def __init__(self, voice: str = "xenia", rate: int = 48000, model: str = "v5_ru", threads: int = 2):
        import torch
        from silero import silero_tts

        torch.set_num_threads(threads)
        self.model, _ = silero_tts(language="ru", speaker=model)  # при первом запуске скачивает модель
        self.voice, self.rate = voice, rate

    def voices(self) -> list[str]:
        return [v for v in self.model.speakers if v != "random"]

    def speak(self, text: str, voice: str) -> bytes:
        audio = self.model.apply_tts(text=text, speaker=voice, sample_rate=self.rate)
        return (audio.clamp(-1, 1) * 32767).short().numpy().tobytes()


class _Stub(types.ModuleType):
    """Заглушка модуля: f5-tts при импорте тянет своё обучение и pydub, которые синтезу не нужны."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (), {})


class ESpeech:
    """ESpeech-TTS (F5-TTS) на видеокарте: говорит голосом из образца.

    Голос — пара файлов в папке голосов: `имя.wav` (до 12 секунд чистой речи) и `имя.txt` (что в нём
    сказано, слово в слово). Ударения в обоих текстах расставляет RUAccent; «+» перед гласной задаёт своё.
    """
    name, rate, limit = "espeech", 24000, 200
    CONFIG = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
    MAX_SAMPLE = 12.0

    def __init__(self, model_dir: Path, voices_dir: Path, voice: str = "", steps: int = 8, half: bool = False):
        for heavy in ("f5_tts.model.trainer", "pydub"):
            sys.modules.setdefault(heavy, _Stub(heavy))
        import soundfile
        import torch
        from f5_tts.infer import utils_infer as f5
        from f5_tts.model import CFM, DiT
        from f5_tts.model.utils import get_tokenizer
        from ruaccent import RUAccent

        self.f5, self.torch, self.steps = f5, torch, steps
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        vocab, size = get_tokenizer(str(model_dir / "vocab.txt"), "custom")
        model = CFM(transformer=DiT(**self.CONFIG, text_num_embeds=size, mel_dim=f5.n_mel_channels),
                    mel_spec_kwargs=dict(n_fft=f5.n_fft, hop_length=f5.hop_length, win_length=f5.win_length,
                                         n_mel_channels=f5.n_mel_channels, target_sample_rate=self.rate,
                                         mel_spec_type="vocos"),
                    odeint_kwargs=dict(method="euler"), vocab_char_map=vocab)
        # Половинная точность вдвое экономит видеопамять, но на GTX 16xx считает в 4–5 раз медленнее полной.
        dtype = torch.float16 if half and self.device == "cuda" else torch.float32
        # В файле две копии весов и состояние обучения: сразу в видеопамять он не влезет. Читаем с диска
        # по частям (mmap) и берём только сглаженную копию — ею модель и говорит.
        saved = torch.load(next(model_dir.glob("*.pt")), map_location="cpu", weights_only=True, mmap=True)
        skip = ("initted", "step", "mel_spec.mel_stft.mel_scale.fb", "mel_spec.mel_stft.spectrogram.window")
        model.load_state_dict({k.removeprefix("ema_model."): v for k, v in saved["ema_model_state_dict"].items()
                               if k.removeprefix("ema_model.") not in skip})
        del saved
        self.model = model.to(dtype).to(self.device).eval()
        self.vocoder = f5.load_vocoder(device=self.device)
        self.accentizer = RUAccent()
        self.accentizer.load(omograph_model_size="turbo3.1", use_dictionary=True, tiny_mode=False)
        self._voices: dict[str, tuple] = {}
        for wav in sorted(voices_dir.glob("*.wav")):
            note = wav.with_suffix(".txt")
            if not note.is_file():
                continue
            audio, rate = soundfile.read(str(wav), dtype="float32", always_2d=True)
            if len(audio) / rate > self.MAX_SAMPLE:
                raise SystemExit(f"образец {wav.name} длиннее {self.MAX_SAMPLE:.0f} секунд — обрежьте")
            text = self.accent(normalize(note.read_text(encoding="utf-8")))
            text = text if text[-1:] in ".!?…" else text + "."
            self._voices[wav.stem] = (torch.from_numpy(audio.T.copy()), rate, text)
        if not self._voices:
            raise SystemExit(f"в {voices_dir} нет голосов: нужны пары имя.wav + имя.txt")
        self.voice = voice or next(iter(self._voices))

    def accent(self, text: str) -> str:
        return text if "+" in text else self.accentizer.process_all(text)

    def voices(self) -> list[str]:
        return list(self._voices)

    def speak(self, text: str, voice: str) -> bytes:
        audio, rate, said = self._voices[voice]
        wave, _, _ = next(self.f5.infer_batch_process(
            (audio, rate), said + " ", [self.accent(text)], self.model, self.vocoder, progress=None,
            nfe_step=self.steps, device=self.device))
        if wave is None:
            return b""
        return (self.torch.from_numpy(wave).clamp(-1, 1) * 32767).short().numpy().tobytes()


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Сервер синтеза речи Jarvis")
    p.add_argument("--engine", default=os.getenv("JARVIS_TTS_ENGINE", "silero"), choices=["silero", "espeech"])
    p.add_argument("--host", default=os.getenv("JARVIS_TTS_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("JARVIS_TTS_PORT", "5007")))
    p.add_argument("--voice", default="", help="голос по умолчанию; какие есть — GET /health")
    p.add_argument("--rate", type=int, default=48000, help="silero: частота звука, 24000 или 48000")
    p.add_argument("--threads", type=int, default=2, help="silero: потоков процессора; на скорость почти не влияет")
    p.add_argument("--model-dir", type=Path, default=Path.home() / ".local/share/jarvis/models/espeech-rl-v2",
                   help="espeech: папка с файлом модели .pt и vocab.txt")
    p.add_argument("--voices", type=Path, default=Path("data/voices"), help="espeech: папка с образцами имя.wav + имя.txt")
    p.add_argument("--steps", type=int, default=8, help="espeech: шагов генерации; время растёт пропорционально")
    p.add_argument("--half", action="store_true", help="espeech: половинная точность — меньше видеопамяти; на GTX 16xx в разы медленнее")
    args = p.parse_args()
    print(f"Загружаю {args.engine}…")
    if args.engine == "espeech":
        engine = ESpeech(args.model_dir, args.voices, args.voice, args.steps, args.half)
    else:
        engine = Silero(args.voice or "xenia", args.rate, threads=args.threads)
    if engine.voice not in engine.voices():
        raise SystemExit(f"нет голоса «{engine.voice}»; есть: {', '.join(engine.voices())}")
    engine.speak("Проверка связи.", engine.voice)  # прогрев: первая фраза не должна ждать
    uvicorn.run(create_app(engine), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
