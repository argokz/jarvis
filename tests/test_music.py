import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from jarvis import music, pc, voice
from jarvis.agent import Reply, integrations_hint, music_tools
from jarvis.config import Settings
from jarvis.music import Music, MusicError, Player, Station, Track, Yandex, YouTube
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.scenarios import ScenarioError, Scenarios, parse_step
from jarvis.store import Store
from jarvis.voice import VoiceService, VoiceSession
from jarvis.wake import WakeWords


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


class Proc:
    def __init__(self):
        self.returncode, self.killed, self._done = None, False, asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, code=0):
        self.returncode = code
        self._done.set()

    def kill(self):
        self.killed = True
        self.finish(-9)


class Deck:
    """Поддельный ffplay с ручными часами: помнит, что и с какой секунды запускали."""

    def __init__(self):
        self.now, self.runs, self.procs, self.resolved = 0.0, [], [], []

    async def spawn(self, url, offset):
        self.runs.append((url, round(offset, 1)))
        self.procs.append(Proc())
        return self.procs[-1]

    async def resolve(self, track):
        self.resolved.append(track.ref)
        return f"http://stream/{track.ref}/{self.resolved.count(track.ref)}"

    def player(self):
        return Player(self.resolve, self.spawn, clock=lambda: self.now)

    async def end(self, code=0, after=200.0):
        """Текущий трек доиграл (или ffplay упал) через after секунд."""
        self.now += after
        self.procs[-1].finish(code)
        await settle()


def tracks(n=3):
    return [Track(f"Песня {i}", "Кино", 200.0, "fake", f"t{i}") for i in range(1, n + 1)]


# --- проигрыватель -------------------------------------------------------------------------------

async def test_player_plays_the_queue_in_order_and_stops_at_the_end():
    d = Deck()
    p = d.player()
    assert p.status() == "Музыка не играет." and not p.active and p.control("pause") == "Музыка не играет."
    p.play(tracks(2))
    await settle()
    assert d.runs == [("http://stream/t1/1", 0.0)] and p.status() == "Играет: Кино — Песня 1 (1 из 2)"
    await d.end()
    assert d.runs[-1] == ("http://stream/t2/1", 0.0) and p.current.title == "Песня 2"
    await d.end()
    assert p.state == "stopped" and len(d.runs) == 2 and not p.active


async def test_pause_remembers_the_second_and_resume_starts_from_it():
    d = Deck()
    p = d.player()
    p.play(tracks())
    await settle()
    d.now += 83.4
    assert p.control("pause") == "Пауза: Кино — Песня 1" and d.procs[0].killed and p.state == "paused"
    await settle()
    assert len(d.runs) == 1 and p.status() == "На паузе: Кино — Песня 1 (1 из 3)"   # убитый ffplay — не конец трека
    d.now += 500                                                                    # пауза в счёт не идёт
    assert p.control("toggle") == "Играет: Кино — Песня 1"
    await settle()
    assert d.runs[-1] == ("http://stream/t1/1", 83.4) and d.resolved == ["t1"]      # адрес потока тот же
    d.now += 10
    p.control("toggle")
    p.control("play")
    await settle()
    assert d.runs[-1][1] == 93.4


async def test_next_previous_and_stop():
    d = Deck()
    p = d.player()
    p.play(tracks(2))
    await settle()
    assert p.control("next") == "Играет: Кино — Песня 2" and d.procs[0].killed
    await settle()
    assert d.runs[-1] == ("http://stream/t2/1", 0.0)
    assert p.control("previous") == "Играет: Кино — Песня 1" and p.control("previous") == "Играет: Кино — Песня 1"
    p.control("next")
    assert p.control("next") == "Это был последний трек в очереди, музыка остановлена." and p.state == "stopped"
    p.play(tracks(2))
    await settle()
    assert p.control("stop") == "Музыка остановлена." and d.procs[-1].killed and p.queue == []
    with pytest.raises(MusicError):
        p.play(tracks(1)) or p.control("rewind")
    live = Track("Лофи радио", duration=None, source="fake", ref="live")
    p.play([live])
    await settle()
    d.now += 60
    p.control("pause")
    p.control("play")
    await settle()
    assert d.runs[-1] == ("http://stream/live/1", 0.0)       # прямой эфир не перематывается


async def test_stream_that_does_not_open_is_asked_again_then_skipped():
    d = Deck()
    p = d.player()
    p.play(tracks(5))
    await settle()
    await d.end(code=1, after=0.5)                           # адрес устарел — запрашиваем заново
    assert d.runs[-1] == ("http://stream/t1/2", 0.0) and p.index == 0
    await d.end(code=1, after=0.5)                           # и снова нет — следующий трек
    assert p.index == 1 and p.error == "не удалось проиграть «Кино — Песня 1»"
    await d.end(code=1, after=120.0)                         # оборвался посреди трека — это не «не открылся»
    assert p.index == 2
    for _ in range(6):
        await d.end(code=1, after=0.1)
    assert p.state == "stopped" and p.index == 4             # три трека подряд не открылись — хватит
    assert "Последняя ошибка: не удалось проиграть «Кино — Песня 5»" in p.status()


async def test_source_errors_do_not_leave_the_player_playing_forever():
    async def no_stream(track):
        raise MusicError("YouTube не ответил")

    async def no_ffplay(url, offset):
        raise MusicError("не найдена программа ffplay")

    p = Player(no_stream, Deck().spawn)
    p.play(tracks(5))
    await settle()
    assert p.state == "stopped" and p.error == "YouTube не ответил" and p.index == 2
    p = Player(Deck().resolve, no_ffplay)
    p.play(tracks(1))
    await settle()
    assert p.state == "stopped" and p.error == "не найдена программа ffplay"

    async def crash(track):
        raise RuntimeError("неожиданно")

    p = Player(crash, Deck().spawn)
    p.play(tracks(1))
    await settle()
    assert p.state == "stopped"


async def test_music_keeps_quiet_for_the_assistant_and_continues_from_the_same_second():
    d = Deck()
    p = d.player()
    p.play(tracks())
    await settle()
    d.now += 42
    p.hold()
    p.hold()                                                 # второй раз — то же молчание
    assert d.procs[0].killed and p.state == "playing"        # это не пауза пользователя
    await settle()
    assert len(d.runs) == 1
    d.now += 30
    p.release()
    await settle()
    assert d.runs[-1] == ("http://stream/t1/1", 42.0)
    p.release()                                              # лишний release ничего не запускает
    await settle()
    assert len(d.runs) == 2


async def test_what_is_asked_during_the_hold_happens_after_it():
    d = Deck()
    p = d.player()
    p.hold()
    p.play(tracks())                                         # «включи Кино»: заиграет, когда ассистент договорит
    await settle()
    assert d.runs == [] and p.state == "playing"
    assert d.resolved == ["t1"]                              # а адрес потока запрошен уже сейчас
    p.release()
    await settle()
    assert d.runs == [("http://stream/t1/1", 0.0)] and d.resolved == ["t1"]
    p.hold()
    assert p.control("next") == "Играет: Кино — Песня 2"
    await settle()
    assert len(d.runs) == 1
    p.release()
    await settle()
    assert d.runs[-1] == ("http://stream/t2/1", 0.0)
    d.now += 15
    p.hold()
    p.control("pause")                                       # попросили паузу — после ответа музыка не вернётся
    p.release()
    await settle()
    assert len(d.runs) == 2 and p.state == "paused"
    p.control("play")
    await settle()
    assert d.runs[-1] == ("http://stream/t2/1", 15.0)


async def test_forgotten_hold_ends_by_itself():
    d = Deck()
    p = d.player()
    p.play(tracks())
    await settle()
    p.hold(limit=0.02)
    await asyncio.sleep(0.06)
    await settle()
    assert len(d.runs) == 2 and not p._held


# --- YouTube -------------------------------------------------------------------------------------

async def test_youtube_search_and_stream():
    calls = []

    async def run(*a, timeout=None):
        calls.append(a)
        if "-g" in a:
            return "https://rr2.googlevideo.com/videoplayback?x=1\n"
        return ("xKpzH5bxYsk\tВиктор Цой - Группа Крови\t291\nb40ZmyQ-8OE\tЛофи радио\tNA\n"
                "мусор без табов\nRcQuuvMHbOM\t\t233.5\n")

    yt = YouTube(run, lambda: "/bin/yt-dlp")
    found = await yt.search("Кино Группа крови", 5)
    assert [(t.title, t.duration, t.ref) for t in found] == [
        ("Виктор Цой - Группа Крови", 291.0, "xKpzH5bxYsk"), ("Лофи радио", None, "b40ZmyQ-8OE"),
        ("без названия", 233.5, "RcQuuvMHbOM")]
    assert calls[0][-1] == "ytsearch5:Кино Группа крови" and found[0].label == "Виктор Цой - Группа Крови"
    assert await yt.resolve(found[0]) == "https://rr2.googlevideo.com/videoplayback?x=1"
    assert calls[1][-1] == "https://www.youtube.com/watch?v=xKpzH5bxYsk"
    with pytest.raises(MusicError, match="что включить"):
        await yt.search("", 5)


async def test_youtube_failures_are_music_errors():
    async def broken(*a, timeout=None):
        raise pc.PCError("yt-dlp не ответил вовремя")

    async def empty(*a, timeout=None):
        return "WARNING: nothing\n"

    with pytest.raises(MusicError, match="не ответил"):
        await YouTube(broken, lambda: "/bin/yt-dlp").search("Кино", 5)
    with pytest.raises(MusicError, match="не дал звук"):
        await YouTube(empty, lambda: "/bin/yt-dlp").resolve(Track("Песня", ref="x" * 11))
    missing = YouTube(empty, lambda: None)
    assert not missing.available
    with pytest.raises(MusicError, match="yt-dlp"):
        await missing.search("Кино", 5)


# --- Яндекс Музыка -------------------------------------------------------------------------------

def ya_track(i, artist="Кино", available=True):
    return NS(title=f"Песня {i}", artists=[NS(name=artist)], duration_ms=200_000, track_id=f"{i}:9", available=available)


class YaClient:
    """Поддельный клиент yandex-music: помнит, что у него спрашивали."""

    def __init__(self, best=None, found=()):
        self.asked, self.best, self.found = [], best, list(found)

    async def init(self):
        return self

    async def search(self, text):
        self.asked.append(("search", text))
        return NS(best=self.best, tracks=NS(results=self.found))

    async def artists_tracks(self, artist_id, page_size=20):
        self.asked.append(("artist", artist_id, page_size))
        return NS(tracks=[ya_track(i) for i in range(1, 4)])

    async def albums_with_tracks(self, album_id):
        self.asked.append(("album", album_id))
        return NS(volumes=[[ya_track(1), ya_track(2)], [ya_track(3)]])

    async def rotor_station_tracks(self, station):
        self.asked.append(("station", station))
        return NS(sequence=[NS(track=ya_track(7, "Сплин")), NS(track=ya_track(8, "Сплин", available=False))])

    async def users_likes_tracks(self):
        return NS(tracks=[NS(track_id="5:1"), NS(track_id="6:1")])

    async def tracks(self, ids):
        self.asked.append(("tracks", list(ids)))
        return [ya_track(i.split(":")[0]) for i in ids]

    async def tracks_download_info(self, track_id):
        self.asked.append(("download", track_id))

        async def link(rate):
            return f"https://ya/{track_id}/{rate}.mp3"

        return [NS(codec="mp3", bitrate_in_kbps=r, get_direct_link_async=lambda r=r: link(r)) for r in (128, 320, 192)] \
            + [NS(codec="aac", bitrate_in_kbps=999, get_direct_link_async=lambda: link(999))]


def yandex(client):
    return Yandex("tok", factory=lambda token: client)


async def test_yandex_plays_what_the_best_match_is():
    c = YaClient(best=NS(type="artist", result=NS(id=41)))
    found = await yandex(c).search("Кино", 20)
    assert [t.label for t in found] == ["Кино — Песня 1", "Кино — Песня 2", "Кино — Песня 3"]
    assert found[0].duration == 200.0 and found[0].ref == "1:9" and found[0].source == "yandex"
    assert c.asked == [("search", "Кино"), ("artist", 41, 20)]
    c = YaClient(best=NS(type="album", result=NS(id=7)))
    assert len(await yandex(c).search("Группа крови альбом", 20)) == 3
    hit = ya_track(2)
    c = YaClient(best=NS(type="track", result=hit), found=[ya_track(1), hit, ya_track(3)])
    assert [t.title for t in await yandex(c).search("Песня 2", 20)] == ["Песня 2", "Песня 1", "Песня 3"]   # она первая
    c = YaClient(best=None, found=[ya_track(4)])
    assert [t.title for t in await yandex(c).search("редкое", 20)] == ["Песня 4"]
    playlist = NS(fetch_tracks_async=lambda: asyncio.sleep(0, [NS(track_id="3:1"), NS(track_id="4:1")]))
    c = YaClient(best=NS(type="playlist", result=playlist))
    assert [t.title for t in await yandex(c).search("плейлист дня", 20)] == ["Песня 3", "Песня 4"]


async def test_yandex_wave_likes_and_stream():
    c = YaClient()
    src = yandex(c)
    for query in ("Моя волна", "", "мою волну!"):
        assert [t.label for t in await src.search(query, 20)] == ["Сплин — Песня 7"]      # недоступный трек отброшен
    assert c.asked == [("station", "user:onyourwave")] * 3
    assert [t.title for t in await src.search("моё любимое", 20)] == ["Песня 5", "Песня 6"]
    assert await src.resolve(Track("Песня", "Кино", ref="1:9")) == "https://ya/1:9/320.mp3"   # лучший mp3, не aac
    assert not Yandex("").available and src.available


async def test_yandex_errors_are_music_errors():
    class Broken(YaClient):
        async def search(self, text):
            raise RuntimeError("Unauthorized")

        async def tracks_download_info(self, track_id):
            raise RuntimeError("timeout")

    src = yandex(Broken())
    with pytest.raises(MusicError, match="не ответила: Unauthorized"):
        await src.search("Кино", 20)
    with pytest.raises(MusicError, match="не отдала трек «Кино — Песня»"):
        await src.resolve(Track("Песня", "Кино", ref="1:9"))


# --- Яндекс Станция ------------------------------------------------------------------------------

def hass(calls, states=None, status=200):
    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None,
                      req.headers.get("authorization")))
        if req.url.path == "/api/states":
            return httpx.Response(status, json=states if states is not None else [
                {"entity_id": "light.lampa"}, {"entity_id": "media_player.tv"},
                {"entity_id": "media_player.yandex_station_r10"}])
        return httpx.Response(status, json=[])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_station_gets_only_a_music_command():
    assert Station.command("Кино") == "включи музыку кино"
    assert Station.command("  ") == "включи музыку" and Station.command("Мою волну!") == "включи мою волну"
    # что бы ни пришло в запросе, Алиса услышит просьбу о музыке — знаки и вторая команда не пройдут
    assert Station.command("Кино. Выключи автомат!") == "включи музыку кино выключи автомат"
    assert len(Station.command("а" * 500)) == len("включи музыку ") + 100


async def test_station_finds_its_player_and_sends_the_command():
    calls = []
    st = Station("http://ha:8123/", "tok", client=hass(calls))
    assert await st.play("Кино") == "включи музыку кино"
    assert calls[0][:2] == ("GET", "/api/states") and calls[0][3] == "Bearer tok"
    assert calls[1] == ("POST", "/api/services/media_player/play_media", {
        "entity_id": "media_player.yandex_station_r10", "media_content_id": "включи музыку кино",
        "media_content_type": "command"}, "Bearer tok")
    await st.play("джаз")
    assert len(calls) == 3                                   # плеер второй раз не ищем
    calls = []
    await Station("http://ha", "tok", "media_player.kitchen", client=hass(calls)).play("джаз")
    assert len(calls) == 1 and calls[0][2]["entity_id"] == "media_player.kitchen"   # из JARVIS_STATION_ENTITY


async def test_station_errors():
    with pytest.raises(MusicError, match="нет Яндекс Станции"):
        await Station("http://ha", "tok", client=hass([], states=[{"entity_id": "media_player.tv"}])).play("x")
    with pytest.raises(MusicError, match="вернул 401"):
        await Station("http://ha", "tok", "media_player.s", client=hass([], status=401)).play("x")

    def refuse(req):
        raise httpx.ConnectError("refused")

    down = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(MusicError, match="недоступен"):
        await Station("http://ha", "tok", "media_player.s", client=down).play("x")
    assert not Station("", "").available


# --- всё вместе ----------------------------------------------------------------------------------

class Source:
    available = True

    def __init__(self, name, found=3):
        self.name, self.found, self.asked = name, found, []

    async def search(self, query, limit):
        self.asked.append((query, limit))
        return [replace(t, source=self.name) for t in tracks(self.found)]

    async def resolve(self, track):
        return f"http://{self.name}/{track.ref}"


class FakeStation:
    available = True

    def __init__(self):
        self.asked = []

    async def play(self, query):
        self.asked.append(query)
        return Station.command(query)


def make(sources=("youtube",), station=False, default="", found=3):
    d = Deck()
    srcs = [Source(n, found) for n in sources]
    m = Music(srcs, FakeStation() if station else None, default, spawn=d.spawn, clock=lambda: d.now)
    return m, d, {s.name: s for s in srcs}


async def test_music_play_searches_the_default_source_and_says_what_it_found():
    m, d, src = make(("youtube", "yandex"), default="yandex")
    assert await m.play({"query": "  Кино   Группа крови "}) == "Включаю: Кино — Песня 1."
    await settle()
    assert src["yandex"].asked == [("Кино Группа крови", 20)] and d.runs == [("http://yandex/t1", 0.0)]
    await m.play({"query": "Кино", "source": "youtube"})
    await settle()
    assert src["youtube"].asked == [("Кино", 5)] and d.runs[-1] == ("http://youtube/t1", 0.0) and d.procs[0].killed
    assert make(("youtube", "yandex"), default="spotify")[0].default == "youtube"    # неизвестный — первый из подключённых


async def test_music_play_on_the_station_and_refusals():
    m, d, src = make(("youtube",), station=True)
    assert await m.play({"query": "Кино", "where": "station"}) == "Попросил Станцию: «включи музыку кино»."
    assert m.station.asked == ["Кино"] and src["youtube"].asked == [] and d.runs == []
    only_station = make((), station=True)[0]
    assert await only_station.play({"query": "джаз"}) == "Попросил Станцию: «включи музыку джаз»."   # ПК нет — на Станцию
    cases = [(make(("youtube",))[0], {"query": "x", "where": "station"}, "Станция не подключена"),
             (only_station, {"query": "x", "where": "pc"}, "на компьютере не подключена"),
             (make(("youtube",))[0], {"query": "x", "source": "yandex"}, "источник «yandex» не подключён"),
             (make(("youtube",), found=0)[0], {"query": "абракадабра"}, "ничего не нашлось"),
             (make(("youtube",))[0], {"query": "x", "where": "kitchen"}, "where")]
    for m, args, why in cases:
        with pytest.raises(MusicError, match=why):
            await m.play(args)
    assert not make(())[0].available and only_station.available


def test_long_titles_are_not_read_aloud():
    assert music.spoken("Кино — Группа крови") == "Включаю: Кино — Группа крови."
    assert music.spoken("Виктор Цой - Группа Крови (Official Video) [HD]") == "Включаю: Виктор Цой - Группа Крови."
    assert music.spoken("Лофи радио | 24/7 beats to relax") == "Включаю: Лофи радио."
    long = "Music for Working on a Computer Background Music for Concentration and Productivity"
    assert music.spoken(long) == music.spoken("(live)") == "Включаю музыку."


def test_tool_describes_only_what_is_connected():
    both = make(("youtube", "yandex"), station=True, default="yandex")[0].tool()
    assert both["name"] == "music_play" and both["input_schema"]["required"] == ["query"]
    assert both["input_schema"]["properties"]["where"]["enum"] == ["pc", "station"]
    assert both["input_schema"]["properties"]["source"]["enum"] == ["youtube", "yandex"]
    assert "по умолчанию yandex" in both["description"] and "моя волна" in both["description"]
    plain = make(("youtube",))[0].tool()
    assert set(plain["input_schema"]["properties"]) == {"query"} and "Станци" not in plain["description"]
    assert "играет на Яндекс Станции" in make((), station=True)[0].tool()["description"]


def test_unavailable_sources_are_dropped():
    m = Music([YouTube(ytdlp=lambda: None), Yandex("")], Station("", ""))
    assert m.sources == {} and m.station is None and not m.available


# --- команды плееру на ПК ------------------------------------------------------------------------

async def test_pc_media_goes_to_the_assistants_music_first():
    browser = {"org.mpris.MediaPlayer2.chromium": ("Playing", "{'xesam:title': <'Видео'>}")}
    cmds = []

    async def shell(*a, timeout=None):
        cmds.append(a)
        if a[8].endswith("ListNames"):
            return "([" + ", ".join(f"'{n}'" for n in browser) + "],)"
        if a[8].endswith("Properties.Get"):
            return "(<'Playing'>,)" if a[10] == "PlaybackStatus" else "(<{'xesam:title': <'Видео'>}>,)"
        return "()"

    m, d, _ = make()
    box = pc.PC(run=shell, player=m.player)
    assert await box.media("pause") == "Выполнено: chromium"        # своей музыки нет — команда браузеру
    assert cmds[-1][8].endswith("Player.Pause")
    await m.play({"query": "Кино"})
    await settle()
    cmds.clear()
    assert await box.media("pause") == "Пауза: Кино — Песня 1" and cmds == []   # своя музыка — браузер не трогаем
    assert await box.media("status") == "На паузе: Кино — Песня 1 (1 из 3)\nchromium: играет, Видео"
    assert await box.media("next") == "Играет: Кино — Песня 2"
    assert await box.media("stop") == "Музыка остановлена." and not m.player.active
    assert await box.media("stop") == "Выполнено: chromium" and cmds[-1][8].endswith("Player.Stop")
    browser.clear()
    await m.play({"query": "Кино"})
    assert await box.media("status") == "Играет: Кино — Песня 1 (1 из 3)"       # других плееров нет — не ошибка
    with pytest.raises(pc.PCError):
        await box.media("rewind")


# --- роутер, агент, сценарии ---------------------------------------------------------------------

class Stream:
    def __init__(self, texts, final):
        self.texts, self.final = texts, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.texts:
                yield NS(type="text", text=t)
        return gen()

    async def get_final_message(self):
        return self.final


class Client:
    def __init__(self, *rounds):
        self.rounds, self.calls = list(rounds), []
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        texts, stop, content = self.rounds.pop(0)
        return Stream(texts, NS(stop_reason=stop, content=content))


class Brain:
    def __init__(self):
        self.notes = []

    def journal_note(self, line):
        self.notes.append(line)

    async def ask(self, user, text, channel="api"):
        return Reply("Ответ агента.")


def use(name, args):
    return NS(type="tool_use", id="tu_1", name=name, input=args)


def router(tmp_path, client, tunes, computer=None):
    s = replace(Settings(), vault=(tmp_path / "vault").resolve(), hass_url="", hass_token="", notion_token="",
                open_roots=())
    (tmp_path / "vault" / "10_Projects").mkdir(parents=True, exist_ok=True)
    brain = Brain()
    return VoiceRouter(s, brain, Store(tmp_path / "j.db"), Notifier(), client=client, tunes=tunes, computer=computer), brain


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_router_turns_music_on_in_one_model_call(tmp_path):
    m, d, src = make()
    client = Client(([], "tool_use", [use("music_play", {"query": "Кино"})]))
    r, brain = router(tmp_path, client, m)
    assert await say(r, "включи Кино") == "Включаю: Кино — Песня 1."
    await settle()
    assert len(client.calls) == 1 and d.runs == [("http://youtube/t1", 0.0)]
    kw = client.calls[0]
    assert [t["name"] for t in kw["tools"]] == ["delegate", "music_play"] and "music_play" in kw["system"][0]["text"]
    assert brain.notes == ["музыка: Кино (voice)"]
    past = [b for msg in r.history("owner", {"music_play"}) if not isinstance(msg["content"], str) for b in msg["content"]]
    assert past[0]["input"] == {"query": "Кино"} and past[1]["content"] == "Включаю: Кино — Песня 1."


async def test_router_explains_when_nothing_is_found_and_has_no_tool_without_music(tmp_path):
    m = make(found=0)[0]
    client = Client(([], "tool_use", [use("music_play", {"query": "абракадабра"})]), (["Такого не нашёл."], "end_turn", []))
    r, _ = router(tmp_path, client, m)
    assert await say(r, "включи абракадабру") == "Такого не нашёл."
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    client = Client((["Привет."], "end_turn", []))
    r, _ = router(tmp_path, client, make(())[0])
    await say(r, "привет")
    assert [t["name"] for t in client.calls[0]["tools"]] == ["delegate"] and "music_play" not in client.calls[0]["system"][0]["text"]


async def test_agent_gets_the_same_music_tool():
    m, d, _ = make()
    tool, = music_tools(m, "telegram")
    assert tool.name == "music_play"
    assert await tool.handler({"query": "Кино"}) == {"content": [{"type": "text", "text": "Включаю: Кино — Песня 1."}]}
    bad = await music_tools(make(found=0)[0], "telegram")[0].handler({"query": "x"})
    assert bad["is_error"] is True and "ничего не нашлось" in bad["content"][0]["text"]
    assert "music_play" in integrations_hint(Settings(), tunes=True) and "music_play" not in integrations_hint(Settings())


async def test_scenario_step_turns_music_on(tmp_path):
    step = parse_step({"music": "моя волна"})
    assert step.args == {"query": "моя волна"} and step.text == "музыка: моя волна"
    there = parse_step({"music": {"query": "джаз", "where": "station", "source": "yandex"}})
    assert there.args == {"query": "джаз", "where": "station", "source": "yandex"} and there.text == "музыка: джаз на Станции"
    assert parse_step({"media": "stop"}).text == "выключить музыку"
    for bad in ({"music": ""}, {"music": {"query": "x", "where": "kitchen"}}, {"music": {"query": "x", "volume": 3}},
                {"music": {"where": "pc"}}):
        with pytest.raises(ScenarioError):
            parse_step(bad)
    vault = tmp_path / "vault"
    (vault / "50_Scenarios").mkdir(parents=True)
    (vault / "50_Scenarios" / "Вечер.md").write_text("---\nsteps:\n  - music: джаз\n  - say: Приятного вечера\n---\n", encoding="utf-8")
    m, d, src = make()
    s = Scenarios(vault, Store(tmp_path / "j.db"), tunes=m)
    assert await s.execute(s.get("Вечер"), "voice") == "Приятного вечера."      # «Включаю: …» сценарий не говорит
    await settle()
    assert src["youtube"].asked == [("джаз", 5)] and len(d.runs) == 1
    s.tunes = make(found=0)[0]
    assert "музыка: джаз — по запросу «джаз» ничего не нашлось" in await s.execute(s.get("Вечер"), "voice")
    s.tunes = None
    assert "музыка не подключена" in await s.execute(s.get("Вечер"), "voice")


# --- голос: музыка молчит, пока ассистент слушает и говорит ------------------------------------------

class Speech:
    enabled, tts_rate, local = True, 16000, True

    def __init__(self, heard=""):
        self.heard = heard

    async def recognize(self, audio, fmt="oggopus", sample_rate=None, hint=""):
        return self.heard

    async def synthesize(self, text):
        return b"\0\0" * 1600                                # 0,1 с звука


class Quiet:
    """Вместо проигрывателя: записывает, когда его просили замолчать и продолжить."""

    def __init__(self, state="playing"):
        self.state, self.log = state, []

    def hold(self, *limit):
        self.log.append(("hold", *limit))

    def release(self):
        self.log.append(("release",))


class Talker:
    async def respond(self, user, text, channel="voice"):
        yield "Хорошо."


def voice_app(player, speech, monkeypatch, gaps=(0.01, 0.05, 0.03)):
    for name, value in zip(("QUIET_GAP", "FOLLOWUP_GAP", "LISTEN_GAP"), gaps):
        monkeypatch.setattr(voice, name, value)
    app = FastAPI()
    svc = VoiceService(Talker(), speech, wake=WakeWords(["клод"]), player=player)

    @app.websocket("/ws")
    async def ep(ws: WebSocket):
        await ws.accept()
        await VoiceSession(ws, svc).run()

    return TestClient(app), svc


def turn(ws, speech, heard):
    speech.heard = heard
    ws.send_json({"type": "passive"})
    ws.send_bytes(b"\0\0" * 1600)
    events = []
    while True:
        msg = ws.receive()
        if msg.get("bytes") is None:
            events.append(json.loads(msg["text"]))
            if events[-1]["type"] == "done":
                return events


def wait_for(player, n):
    import time
    for _ in range(100):
        if len(player.log) >= n:
            return
        time.sleep(0.01)


def test_voice_hushes_music_for_the_answer_and_tells_the_client_not_to_listen_on(monkeypatch):
    player, speech = Quiet(), Speech()
    client, _ = voice_app(player, speech, monkeypatch)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "followup": True})
        assert turn(ws, speech, "просто разговор в комнате")[-1] == {"type": "done", "turn": 1}
        assert player.log == []                                  # к ассистенту не обращались — музыка играет
        done = turn(ws, speech, "Клод, который час?")[-1]
        assert done == {"type": "done", "turn": 2, "followup": False}   # под музыку продолжение не слушаем
        assert player.log[0] == ("hold",)
        wait_for(player, 2)
        assert player.log == [("hold",), ("release",)]           # после ответа музыка продолжает
        player.state = "paused"                                  # пользователь поставил паузу — обычный разговор
        assert turn(ws, speech, "Клод, который час?")[-1] == {"type": "done", "turn": 3}
        wait_for(player, 4)
        assert turn(ws, speech, "Клод!")[-2:] == [{"type": "wake", "turn": 4}, {"type": "done", "turn": 4}]
        assert player.log[-1] == ("hold",)                       # позвали по имени — тихо, пока скажут команду
        wait_for(player, 6)
        assert player.log[-1] == ("release",)


def test_voice_keeps_music_quiet_longer_for_clients_that_listen_on(monkeypatch):
    player, speech = Quiet(), Speech()
    client, _ = voice_app(player, speech, monkeypatch, gaps=(0.01, 0.6, 0.03))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                                        # клиент о followup не объявлял
        done = turn(ws, speech, "Клод, который час?")[-1]
        assert done == {"type": "done", "turn": 1, "followup": False}
        import time
        time.sleep(0.3)                                          # ответ доигран, но клиент ещё слушает продолжение
        assert player.log == [("hold",)]
        wait_for(player, 2)
        assert player.log == [("hold",), ("release",)]


def test_voice_without_music_changes_nothing(monkeypatch):
    speech = Speech()
    client, _ = voice_app(None, speech, monkeypatch)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert turn(ws, speech, "Клод, который час?")[-1] == {"type": "done", "turn": 1}
