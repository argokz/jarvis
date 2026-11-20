import asyncio
import io
import json
import wave
from types import SimpleNamespace as NS

import httpx
import pytest

from jarvis.speech import LocalSpeech, SpeechError, SpeechKit, make_speech, pcm_to_wav, wav_to_pcm

PCM = b"\x01\x00\x02\x00" * 100


def local(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LocalSpeech("http://stt/v1/audio/transcriptions", "http://tts/", client=client, **kw)


def test_wav_roundtrip():
    assert wav_to_pcm(pcm_to_wav(PCM, 16000)) == (16000, PCM)


def test_recognize_wraps_pcm_in_wav_and_sends_lang_and_model():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "  какие проекты есть \n"})

    sp = local(handler, stt_model="Systran/faster-whisper-small")
    text = asyncio.run(sp.recognize(PCM, fmt="lpcm", sample_rate=16000))
    assert text == "какие проекты есть"
    assert seen["url"] == "http://stt/v1/audio/transcriptions"
    body = seen["body"]
    assert b'name="language"\r\n\r\nru' in body
    assert b'name="model"\r\n\r\nSystran/faster-whisper-small' in body
    assert b'filename="audio.wav"' in body and pcm_to_wav(PCM, 16000) in body
    assert b'name="prompt"' not in body and b'name="response_format"\r\n\r\njson' in body


def test_recognize_with_hint_sends_prompt_and_drops_noise_segments():
    seen = {}
    segments = [{"text": " Стоп!", "no_speech_prob": 0.02}, {"text": " Хватит.", "no_speech_prob": 0.86}]

    def handler(req: httpx.Request):
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "Стоп! Хватит.", "segments": segments})

    sp = local(handler)
    assert asyncio.run(sp.recognize(PCM, fmt="lpcm", sample_rate=16000, hint="Стоп. Хватит.")) == "Стоп!"
    assert b'name="prompt"\r\n\r\n' + "Стоп. Хватит.".encode() in seen["body"]
    assert b'name="response_format"\r\n\r\nverbose_json' in seen["body"]
    segments[0]["no_speech_prob"] = 0.9   # один шум: Whisper «услышал» слова подсказки
    assert asyncio.run(sp.recognize(PCM, fmt="lpcm", sample_rate=16000, hint="Стоп. Хватит.")) == ""
    del segments[:]                        # сервер без сегментов в ответе — берём текст как есть
    sp2 = local(lambda req: httpx.Response(200, json={"text": " стоп "}))
    assert asyncio.run(sp2.recognize(PCM, fmt="lpcm", sample_rate=16000, hint="Стоп.")) == "стоп"


def test_recognize_ogg_is_sent_as_is():
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "ок"})

    assert asyncio.run(local(handler).recognize(b"OggS-data")) == "ок"
    assert b'filename="voice.ogg"' in seen["body"] and b"OggS-data" in seen["body"]
    assert b'name="model"' not in seen["body"]


def test_synthesize_returns_pcm_from_piper_wav():
    seen = {}

    def handler(req):
        seen["url"], seen["json"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, content=pcm_to_wav(PCM, 22050))

    sp = local(handler, tts_voice="ru_RU-irina-medium")
    assert asyncio.run(sp.synthesize("Привет")) == PCM
    assert seen["url"] == "http://tts/synthesize"
    assert seen["json"] == {"text": "Привет", "voice": "ru_RU-irina-medium"}


def test_synthesize_through_openai_compatible_server():
    seen = {}

    def handler(req):
        seen["url"], seen["json"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, content=pcm_to_wav(PCM, 48000))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sp = LocalSpeech("http://stt", "http://tts/v1/audio/speech/", tts_voice="xenia", tts_rate=48000, client=client)
    assert sp.tts_openai and asyncio.run(sp.synthesize("Привет")) == PCM
    assert seen["url"] == "http://tts/v1/audio/speech"
    assert seen["json"] == {"input": "Привет", "response_format": "wav", "voice": "xenia"}
    sp = LocalSpeech("http://stt", "http://tts/v1/audio/speech", tts_rate=48000, tts_model="tts-1", client=client)
    asyncio.run(sp.synthesize("Привет"))
    assert seen["json"] == {"input": "Привет", "response_format": "wav", "model": "tts-1"}
    assert not local(handler).tts_openai  # адрес без /audio/speech — сервер Piper


@pytest.mark.parametrize("riff, size", [(0, 0), (36, 0), (0xFFFFFFFF, 0xFFFFFFFF)])
def test_wav_from_streaming_server_has_no_length_in_header(riff, size):
    wav = bytearray(pcm_to_wav(PCM, 24000))
    wav[4:8], wav[40:44] = riff.to_bytes(4, "little"), size.to_bytes(4, "little")
    assert wav_to_pcm(bytes(wav)) == (24000, PCM)
    assert wav_to_pcm(bytes(wav) + b"\x07") == (24000, PCM)  # оборванный отсчёт отбрасывается


def test_wav_must_be_pcm16_mono():
    with pytest.raises(SpeechError, match="не WAV"):
        wav_to_pcm(b"ID3 mp3 data")
    assert wav_to_pcm(pcm_to_wav(b"", 24000)) == (24000, b"")
    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(24000)
        w.writeframes(PCM)
    with pytest.raises(SpeechError, match="2 кан"):
        wav_to_pcm(stereo.getvalue())
    with pytest.raises(SpeechError, match="без звука"):
        wav_to_pcm(pcm_to_wav(PCM, 24000)[:36])


def test_synthesize_rejects_wrong_rate():
    sp = local(lambda req: httpx.Response(200, content=pcm_to_wav(PCM, 16000)))
    with pytest.raises(SpeechError, match="16000"):
        asyncio.run(sp.synthesize("Привет"))


def test_errors_become_speech_error():
    sp = local(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(SpeechError, match="500"):
        asyncio.run(sp.recognize(b"x"))
    with pytest.raises(SpeechError, match="500"):
        asyncio.run(sp.synthesize("x"))

    def down(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(SpeechError, match="недоступен"):
        asyncio.run(local(down).synthesize("x"))


def settings(**kw):
    base = dict(stt_url="", tts_url="", speech_lang="ru-RU", stt_model="", tts_voice="", tts_rate=22050, tts_model="",
                speechkit_key="", speechkit_voice="alena", speechkit_tts_rate=48000, speechkit_folder="")
    return NS(**{**base, **kw})


def test_make_speech_picks_backend():
    sp = make_speech(settings(stt_url="http://stt", tts_url="http://tts"))
    assert isinstance(sp, LocalSpeech) and sp.enabled and sp.tts_rate == 22050 and sp.lang == "ru"
    assert not make_speech(settings(stt_url="http://stt")).enabled  # без TTS голос выключен
    sp = make_speech(settings(stt_url="http://stt", tts_url="http://tts/v1/audio/speech", tts_model="silero"))
    assert sp.tts_openai and sp.tts_model == "silero"
    yc = make_speech(settings(speechkit_key="k"))
    assert isinstance(yc, SpeechKit) and yc.enabled
    assert not make_speech(settings()).enabled
