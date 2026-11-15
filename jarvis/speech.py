"""Yandex SpeechKit, REST API v1: распознавание (STT) и синтез (TTS).

STT v1 — синхронный, до 30 секунд и 1 МБ аудио на запрос.
TTS v1 — ответ приходит целиком; формат lpcm = сырой PCM 16 бит, моно, little-endian.
"""
from __future__ import annotations

import httpx

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
MAX_STT_BYTES = 1024 * 1024
MAX_STT_SECONDS = 30


class SpeechError(Exception):
    pass


class SpeechKit:
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

    async def recognize(self, audio: bytes, fmt: str = "oggopus", sample_rate: int | None = None) -> str:
        """fmt: oggopus (голосовые Telegram) или lpcm (PCM16 моно, нужен sample_rate)."""
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
