"""Распознавание (STT) и синтез (TTS) речи. Два бэкенда с одним интерфейсом:

SpeechKit — Yandex SpeechKit, REST API v1.
  STT v1 — синхронный, до 30 секунд и 1 МБ аудио на запрос.
  TTS v1 — ответ приходит целиком; формат lpcm = сырой PCM 16 бит, моно, little-endian.
LocalSpeech — свои серверы, без облака.
  STT — OpenAI-совместимый /v1/audio/transcriptions (faster-whisper-server, speaches).
  TTS — HTTP-сервер Piper: POST /synthesize {"text"} → WAV, либо OpenAI-совместимый
        /v1/audio/speech (свой jarvis.tts_server и почти любой другой сервер синтеза) — если
        JARVIS_TTS_URL оканчивается на /audio/speech.

Выбор — make_speech(): задан JARVIS_STT_URL или JARVIS_TTS_URL → LocalSpeech, иначе SpeechKit.
"""
from __future__ import annotations

import io
import struct
import wave
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Settings

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
MAX_STT_BYTES = 1024 * 1024
MAX_STT_SECONDS = 30
NO_SPEECH_MAX = 0.5  # Whisper: выше — в отрезке не речь, а шум (на шуме он «слышит» слова из подсказки)


class SpeechError(Exception):
    pass


class SpeechKit:
    local = False  # звук уходит в облако и оплачивается: слушать комнату постоянно через него нельзя

    def __init__(self, api_key: str, lang: str = "ru-RU", voice: str = "alena",
                 tts_rate: int = 48000, folder_id: str = "", client: httpx.AsyncClient | None = None):
        self.api_key, self.lang, self.voice = api_key, lang, voice
        self.tts_rate, self.folder_id = tts_rate, folder_id
        self._client = client or httpx.AsyncClient(timeout=30)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Api-Key {self.api_key}"}

    def _common(self) -> dict:
        return {"folderId": self.folder_id} if self.folder_id else {}

    async def recognize(self, audio: bytes, fmt: str = "oggopus", sample_rate: int | None = None,
                        hint: str = "") -> str:
        """fmt: oggopus (голосовые Telegram) или lpcm (PCM16 моно, нужен sample_rate). hint не используется."""
        if len(audio) > MAX_STT_BYTES:
            raise SpeechError(f"Аудио больше 1 МБ — запишите короче (до {MAX_STT_SECONDS} секунд).")
        params = {"lang": self.lang, "format": fmt, **self._common()}
        if fmt == "lpcm":
            params["sampleRateHertz"] = str(sample_rate or 16000)
        r = await self._client.post(STT_URL, params=params, headers=self._headers(), content=audio)
        if r.status_code != 200:
            raise SpeechError(f"SpeechKit STT вернул {r.status_code}: {r.text[:200]}")
        return (r.json().get("result") or "").strip()

    async def synthesize(self, text: str) -> bytes:
        """Возвращает PCM16 моно с частотой self.tts_rate."""
        data = {"text": text[:4900], "lang": self.lang, "voice": self.voice, "format": "lpcm",
                "sampleRateHertz": str(self.tts_rate), **self._common()}
        r = await self._client.post(TTS_URL, headers=self._headers(), data=data)
        if r.status_code != 200:
            raise SpeechError(f"SpeechKit TTS вернул {r.status_code}: {r.text[:200]}")
        return r.content


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[int, bytes]:
    """WAV → (частота, PCM16 моно). Другие форматы не поддерживаем — клиенты ждут именно его.

    Разбор свой, а не через модуль wave: серверы, которые отдают звук потоком, не знают длину заранее
    и пишут в заголовок ноль или 0xFFFFFFFF — тогда звуком считается всё до конца файла.
    """
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("TTS вернул не WAV")
    pos, rate = 12, 0
    while pos + 8 <= len(data):
        tag, size, body = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little"), pos + 8
        if tag == b"fmt " and size >= 16:
            kind, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, body)
            if kind not in (1, 0xFFFE) or channels != 1 or bits != 16:
                raise SpeechError(f"TTS вернул {channels} кан., {bits} бит, формат {kind}; нужен PCM16 моно")
        elif tag == b"data" and rate:
            pcm = data[body:body + size] if 0 < size <= len(data) - body else data[body:]
            return rate, pcm[:len(pcm) - len(pcm) % 2]
        pos = body + size + size % 2
    raise SpeechError("TTS вернул WAV без звука")


class LocalSpeech:
    local = True

    def __init__(self, stt_url: str, tts_url: str, lang: str = "ru-RU", stt_model: str = "",
                 tts_voice: str = "", tts_rate: int = 22050, client: httpx.AsyncClient | None = None,
                 tts_model: str = ""):
        self.stt_url, self.tts_url = stt_url, tts_url.rstrip("/")
        self.lang, self.stt_model = lang.split("-")[0], stt_model
        self.tts_voice, self.tts_rate, self.tts_model = tts_voice, tts_rate, tts_model
        self.tts_openai = self.tts_url.endswith("/audio/speech")  # иначе — сервер Piper
        self._client = client or httpx.AsyncClient(timeout=60)

    @property
    def enabled(self) -> bool:
        # Голосовой протокол объявляет stt и tts одним флагом — нужны оба.
        return bool(self.stt_url and self.tts_url)

    async def recognize(self, audio: bytes, fmt: str = "oggopus", sample_rate: int | None = None,
                        hint: str = "") -> str:
        """fmt: oggopus (голосовые Telegram) или lpcm (PCM16 моно, нужен sample_rate).

        hint — фразы, которые сейчас вероятны (поле prompt у Whisper): одиночное «стоп» без подсказки
        распознаётся ненадёжно. С подсказкой Whisper слышит её слова и в шуме, поэтому отрезки, где он
        сам оценивает вероятность «не речь» выше NO_SPEECH_MAX, отбрасываются.
        """
        if fmt == "lpcm":
            file = ("audio.wav", pcm_to_wav(audio, sample_rate or 16000), "audio/wav")
        else:
            file = ("voice.ogg", audio, "audio/ogg")
        data = {"language": self.lang, "response_format": "json"}
        if self.stt_model:
            data["model"] = self.stt_model
        if hint:
            data.update(prompt=hint, response_format="verbose_json")
        try:
            r = await self._client.post(self.stt_url, data=data, files={"file": file})
        except httpx.HTTPError as e:
            raise SpeechError(f"STT-сервер недоступен ({self.stt_url}): {e}") from e
        if r.status_code != 200:
            raise SpeechError(f"STT вернул {r.status_code}: {r.text[:200]}")
        body = r.json()
        if hint and isinstance(body.get("segments"), list):
            return " ".join(s.get("text", "").strip() for s in body["segments"]
                            if s.get("no_speech_prob", 0) <= NO_SPEECH_MAX).strip()
        return (body.get("text") or "").strip()

    async def synthesize(self, text: str) -> bytes:
        """Возвращает PCM16 моно с частотой self.tts_rate."""
        if self.tts_openai:
            url, body = self.tts_url, {"input": text[:4900], "response_format": "wav"}
            if self.tts_model:
                body["model"] = self.tts_model
        else:
            url, body = f"{self.tts_url}/synthesize", {"text": text[:4900]}
        if self.tts_voice:
            body["voice"] = self.tts_voice
        try:
            r = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise SpeechError(f"TTS-сервер недоступен ({self.tts_url}): {e}") from e
        if r.status_code != 200:
            raise SpeechError(f"TTS вернул {r.status_code}: {r.text[:200]}")
        rate, pcm = wav_to_pcm(r.content)
        if rate != self.tts_rate:
            # Клиенты узнают частоту из hello до синтеза — несовпадение исказит скорость речи.
            raise SpeechError(f"TTS вернул {rate} Гц, а JARVIS_TTS_RATE={self.tts_rate}")
        return pcm


def make_speech(s: Settings) -> SpeechKit | LocalSpeech:
    if s.stt_url or s.tts_url:
        return LocalSpeech(s.stt_url, s.tts_url, s.speech_lang, s.stt_model, s.tts_voice, s.tts_rate,
                           tts_model=s.tts_model)
    return SpeechKit(s.speechkit_key, s.speech_lang, s.speechkit_voice, s.speechkit_tts_rate, s.speechkit_folder)
