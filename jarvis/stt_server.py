"""Своё распознавание речи: GigaAM (через onnx-asr) за интерфейсом OpenAI `/v1/audio/transcriptions`.

Whisper на процессоре тратит на любую реплику больше двух секунд: он всегда обрабатывает окно в
30 секунд. GigaAM — русская модель, короткую фразу распознаёт за доли секунды и не сочиняет текст
на тишине. Ядро ходит сюда так же, как в Whisper: достаточно поменять `JARVIS_STT_URL`.

Запуск (Python 3.13, набор зависимостей stt):
    uv run --python 3.13 --extra voice --extra stt python -m jarvis.stt_server
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import subprocess
import time
import wave

import numpy as np
from fastapi import FastAPI, HTTPException, Request

RATES = {8000, 16000, 22050, 24000, 32000, 44100, 48000}  # частоты, которые модель принимает сама
MAX_BYTES = 30 * 1024 * 1024
DEFAULT_MODEL = "gigaam-v3-e2e-ctc"  # с пунктуацией и числами цифрами


class DecodeError(Exception):
    pass


def decode(data: bytes) -> tuple[np.ndarray, int]:
    """Звук из файла → отсчёты float32 моно и частота. WAV читается сам, остальное — через ffmpeg."""
    if data[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(data)) as w:
                rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
                raw = w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            rate = width = channels = 0
        if width == 2 and rate in RATES and channels:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
            return (x.reshape(-1, channels).mean(axis=1) if channels > 1 else x), rate
    if not shutil.which("ffmpeg"):
        raise DecodeError("этот формат читается только через ffmpeg, а он не установлен")
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                       input=data, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise DecodeError(f"не удалось прочитать звук: {p.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768, 16000


def create_app(recognize, name: str = DEFAULT_MODEL) -> FastAPI:
    """recognize(samples, rate) -> str — сама модель; в тестах подменяется."""
    app = FastAPI(title="Jarvis STT")
    lock = asyncio.Lock()  # модель одна: реплики распознаются по очереди

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "model": name}

    @app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request) -> dict:
        form = await request.form()  # language, model и prompt принимаются, но модели не нужны
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(422, "нужно поле file со звуком")
        data = await file.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "файл слишком большой")
        try:
            samples, rate = await asyncio.to_thread(decode, data)
        except DecodeError as e:
            raise HTTPException(400, str(e)) from e
        if samples.size < rate // 10:  # короче 0,1 с — распознавать нечего
            return {"text": "", "duration": samples.size / rate, "took": 0.0}
        started = time.monotonic()
        async with lock:
            text = await asyncio.to_thread(recognize, samples, rate)
        return {"text": (text or "").strip(), "duration": round(samples.size / rate, 2),
                "took": round(time.monotonic() - started, 3)}

    return app


def load(name: str = DEFAULT_MODEL, quantization: str | None = "int8"):
    """Загружает модель; при первом запуске onnx-asr скачивает её с Hugging Face (~225 МБ)."""
    import onnx_asr

    model = onnx_asr.load_model(name, quantization=quantization or None)
    return lambda samples, rate: model.recognize(samples, sample_rate=rate)


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Сервер распознавания речи Jarvis (GigaAM)")
    p.add_argument("--host", default=os.getenv("JARVIS_STT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("JARVIS_STT_PORT", "5006")))
    p.add_argument("--model", default=os.getenv("JARVIS_STT_ENGINE", DEFAULT_MODEL),
                   help="имя модели onnx-asr: gigaam-v3-e2e-ctc, gigaam-v3-ctc, gigaam-v3-e2e-rnnt …")
    p.add_argument("--quantization", default="int8", help="int8 — быстрее и втрое меньше; пусто — полная модель")
    args = p.parse_args()
    print(f"Загружаю {args.model}…")
    recognize = load(args.model, args.quantization)
    recognize(np.zeros(16000, dtype=np.float32), 16000)  # прогрев: первая реплика не должна ждать
    uvicorn.run(create_app(recognize, args.model), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
