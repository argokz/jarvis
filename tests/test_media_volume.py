"""Громкость музыки и видео отдельно от общей и приглушение их на время разговора."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from jarvis import music, pc
from jarvis.music import Player, Track
from jarvis.voice import VoiceService
from test_music import Proc
from test_pc import Shell


def streams():
    return {
        "41": {"name": "Firefox", "binary": "firefox", "volume": 0.5},
        "42": {"name": "Jarvis — музыка", "binary": "ffplay", "volume": 1.0},     # своя музыка — через плеер
        "43": {"name": "python3.13", "binary": "python3.13", "volume": 1.0},     # голос ассистента
        "44": {"name": "Telegram", "binary": "telegram-desktop", "volume": 0.8},  # не плеер
        "45": {"name": "Google Chrome", "binary": "chrome", "volume": 0.9, "cls": "Stream/Input/Audio"},  # микрофон
    }


class Deck:
    def __init__(self):
        self.runs = []

    async def spawn(self, url, offset, **kw):
        self.runs.append((url, round(offset, 1), kw.get("volume", 100)))
        return Proc()

    async def resolve(self, track):
        return f"http://stream/{track.ref}"


async def playing_player(tmp_path=None):
    d = Deck()
    p = Player(d.resolve, d.spawn, clock=lambda: 0.0, level_file=tmp_path / "vol.json" if tmp_path else None)
    p.play([Track("Песня", ref="a", duration=200)])
    await asyncio.sleep(0.01)
    return p, d


async def test_media_volume_leaves_system_and_assistant_alone(tmp_path):
    sh = Shell(volume=0.7, streams=streams())
    player, deck = await playing_player(tmp_path)
    box = pc.PC(sh, player=player)
    out = await box.volume("down", 20, target="media")
    assert out == "Громкость: музыка ассистента 80%; Firefox 30%"
    await asyncio.sleep(0.01)
    assert player.level == 80 and deck.runs[-1][2] == music.gain(80)   # ffplay перезапущен тише
    st = sh.streams
    assert st["41"]["volume"] == pytest.approx(0.3)
    assert (st["42"]["volume"], st["43"]["volume"], st["44"]["volume"], st["45"]["volume"]) == (1.0, 1.0, 0.8, 0.9)
    assert sh.volume == 0.7                                               # общая громкость не тронута
    assert await box.volume("get", target="media") == "Громкость: музыка ассистента 80%; Firefox 30%"
    assert "без звука" in await box.volume("mute", target="media") and st["41"]["muted"]


async def test_system_target_and_nothing_playing():
    sh = Shell(volume=0.5, streams={"43": streams()["43"]})
    box = pc.PC(sh)
    assert await box.volume("set", 40, target="system") == "Громкость 40%"
    assert await box.volume("get", target="media") == "Музыка и видео сейчас не играют. Общая громкость: 40%"
    assert sh.streams["43"]["volume"] == 1.0
    with pytest.raises(pc.PCError, match="target"):
        await box.volume("get", target="speaker")


async def test_duck_and_restore_keeps_a_change_made_meanwhile():
    sh = Shell(streams=streams())
    box = pc.PC(sh)
    await box.duck(0.3)
    assert sh.streams["41"]["volume"] == pytest.approx(0.15)
    assert sh.streams["43"]["volume"] == 1.0                        # голос ассистента не приглушается
    await box.duck(0.3)                                             # второй раз — не глушит ещё сильнее
    assert sh.streams["41"]["volume"] == pytest.approx(0.15)
    # «сделай видео громче» во время разговора: считаем от громкости до приглушения
    assert await box.volume("up", 20, target="media") == "Громкость: Firefox 70%"
    assert sh.streams["41"]["volume"] == pytest.approx(0.21)
    await box.unduck()
    assert sh.streams["41"]["volume"] == pytest.approx(0.7)
    await box.unduck()                                              # нечего возвращать — ничего не делает
    assert sh.streams["41"]["volume"] == pytest.approx(0.7)


async def test_no_pipewire_means_no_streams():
    async def broken(*a):
        raise pc.PCError("не найдена программа pw-dump")
    assert await pc.PC(broken).media_streams() == []


async def test_player_level_persists_and_waits_while_held(tmp_path):
    p, d = await playing_player(tmp_path)
    p.hold()
    p.set_level(50)
    await asyncio.sleep(0.01)
    assert len(d.runs) == 1                                         # пока ассистент говорит — не запускаем
    p.release()
    await asyncio.sleep(0.01)
    assert d.runs[-1][2] == music.gain(50) == 12
    again = Player(d.resolve, d.spawn, level_file=tmp_path / "vol.json")
    assert again.level == 50 and not again.muted
    p.set_level(muted=True)
    await asyncio.sleep(0.01)
    assert d.runs[-1][2] == 0
    assert music.gain(100) == 100 and music.gain(0) == 0 and music.gain(10) == 1


async def test_voice_ducks_while_talking_and_restores_after():
    calls = []

    class Box:
        async def duck(self, factor):
            calls.append(("duck", factor))

        async def unduck(self):
            calls.append(("unduck",))

    svc = VoiceService(router=None, speech=NS(enabled=False), computer=Box(), duck=0.3)
    svc._hush_music()
    await asyncio.sleep(0)
    svc._resume_music(0.01)
    await asyncio.sleep(0.05)
    assert calls == [("duck", 0.3), ("unduck",)]
    off = VoiceService(router=None, speech=NS(enabled=False), computer=Box(), duck=0)
    off._hush_music()
    await asyncio.sleep(0)
    assert len(calls) == 2                                          # JARVIS_MEDIA_DUCK=0 — не трогаем
