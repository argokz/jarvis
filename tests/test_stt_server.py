import io
import wave

import pytest

np = pytest.importorskip("numpy")  # сервер распознавания живёт в окружении голоса (Python 3.13)

from fastapi.testclient import TestClient  # noqa: E402

from jarvis.speech import pcm_to_wav  # noqa: E402
from jarvis.stt_server import DecodeError, create_app, decode  # noqa: E402


def wav(rate=16000, seconds=1.0, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(np.full(int(rate * seconds) * channels, 8000, dtype=np.int16).tobytes())
    return buf.getvalue()


def test_decode_reads_wav_itself():
    x, rate = decode(pcm_to_wav(np.full(1600, 16384, dtype=np.int16).tobytes(), 16000))
    assert rate == 16000 and x.dtype == np.float32 and x.shape == (1600,) and abs(x[0] - 0.5) < 1e-6
    x, rate = decode(wav(rate=22050, channels=2))          # звук Piper и стерео — без ffmpeg
    assert rate == 22050 and x.shape == (22050,)


def test_decode_reports_garbage(monkeypatch):
    with pytest.raises(DecodeError):
        decode("это не звук".encode())
    monkeypatch.setattr("jarvis.stt_server.shutil.which", lambda name: None)
    with pytest.raises(DecodeError, match="ffmpeg"):
        decode(b"OggS....")


def test_server_speaks_the_whisper_api():
    seen = []

    def recognize(samples, rate):
        seen.append((samples.size, rate))
        return " Джарвис, включи свет. "

    c = TestClient(create_app(recognize, "fake"))
    assert c.get("/health").json() == {"ok": True, "model": "fake"}
    # ядро шлёт те же поля, что и Whisper: лишние принимаются молча
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav(), "audio/wav")},
               data={"language": "ru", "model": "whatever", "prompt": "Стоп.", "response_format": "verbose_json"})
    assert r.status_code == 200 and r.json()["text"] == "Джарвис, включи свет." and r.json()["duration"] == 1.0
    assert seen == [(16000, 16000)]
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav(seconds=0.05), "audio/wav")})
    assert r.json()["text"] == "" and len(seen) == 1       # щелчок короче 0,1 с модели не отдаём
    assert c.post("/v1/audio/transcriptions", data={"language": "ru"}).status_code == 422
    assert c.post("/v1/audio/transcriptions", files={"file": ("a.bin", "мусор".encode(), "audio/wav")}).status_code == 400
