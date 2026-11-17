import asyncio
import json
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
    base = dict(stt_url="", tts_url="", speech_lang="ru-RU", stt_model="", tts_voice="", tts_rate=22050,
                speechkit_key="", speechkit_voice="alena", speechkit_tts_rate=48000, speechkit_folder="")
    return NS(**{**base, **kw})


def test_make_speech_picks_backend():
    sp = make_speech(settings(stt_url="http://stt", tts_url="http://tts"))
    assert isinstance(sp, LocalSpeech) and sp.enabled and sp.tts_rate == 22050 and sp.lang == "ru"
    assert not make_speech(settings(stt_url="http://stt")).enabled  # без TTS голос выключен
    yc = make_speech(settings(speechkit_key="k"))
    assert isinstance(yc, SpeechKit) and yc.enabled
    assert not make_speech(settings()).enabled
