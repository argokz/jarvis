"""Распознавание голосовых через Yandex SpeechKit (синхронный REST API v1).

Telegram присылает OGG/Opus — формат, который API принимает как есть.
Ограничения API: до 30 секунд и 1 МБ на запрос.
"""
from __future__ import annotations

import httpx

URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
MAX_BYTES = 1024 * 1024
MAX_SECONDS = 30


class SttError(Exception):
    pass


async def recognize(ogg: bytes, api_key: str, lang: str = "ru-RU", client: httpx.AsyncClient | None = None) -> str:
    if len(ogg) > MAX_BYTES:
        raise SttError("Голосовое больше 1 МБ — запишите короче (до 30 секунд).")
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        r = await client.post(URL, params={"lang": lang, "format": "oggopus"},
                              headers={"Authorization": f"Api-Key {api_key}"}, content=ogg)
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        raise SttError(f"SpeechKit вернул {r.status_code}: {r.text[:200]}")
    return (r.json().get("result") or "").strip()
