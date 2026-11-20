"""Музыка: найти и включить на компьютере ядра (YouTube, Яндекс Музыка) или попросить Яндекс Станцию.

На компьютере играет `ffplay` без окна: только звук. Очередь, пауза и «дальше» — свои (класс Player),
поэтому ассистент может заставить музыку замолчать, пока слушает и отвечает, и продолжить с того же
места. Пауза — это остановка ffplay и новый запуск с нужной секунды: остановленный процесс сетевой
поток не удерживает.

Станции уходит одна команда «включи музыку …» через службу Home Assistant `media_player.play_media`.
Это единственное место, где ядро вызывает службу HA напрямую, а не инструмент Assist, поэтому текст
команды собирается здесь и всегда начинается со слов «включи музыку»: другую команду Алисе так не
передать.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass

import httpx

from . import pc

log = logging.getLogger("jarvis.music")

PLACES = ("pc", "station")
SOURCES = ("youtube", "yandex")
SOURCE_NAMES = {"youtube": "YouTube", "yandex": "Яндекс Музыка"}
QUEUE = {"youtube": 5, "yandex": 20}  # сколько треков ставить в очередь по одному запросу
SEARCH_TIMEOUT = 20.0
FAIL_FAST = 3.0      # ffplay вышел с ошибкой раньше — поток не открылся
MAX_FAILS = 3        # столько треков подряд не открылось — дальше не пробуем
HOLD_MAX = 120.0     # музыка молчит ради ассистента не дольше: если о ней забыли, продолжит сама
STATION_TIMEOUT = 10.0
WAVE = "user:onyourwave"


class MusicError(Exception):
    pass


@dataclass
class Track:
    title: str
    artist: str = ""
    duration: float | None = None  # секунды; None — прямой эфир, перемотки нет
    source: str = ""
    ref: str = ""                  # id видео или трека: по нему источник выдаёт адрес потока
    url: str = ""                  # адрес потока; живёт недолго, запрашивается перед воспроизведением

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


def spoken(label: str) -> str:
    """Название для озвучки: без скобок и хвостов; длинное (обычно английское с YouTube) не читаем вовсе."""
    short = re.split(r"\s*[(\[|/]", label)[0].strip(" -—:.")
    return f"Включаю: {short}." if short and len(short) <= 60 else "Включаю музыку."


def words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower().replace("ё", "е")))


# --- источники ------------------------------------------------------------------------------

class YouTube:
    """Поиск и звуковой поток через yt-dlp. Аккаунт не нужен."""

    name = "youtube"

    def __init__(self, run=pc._run, ytdlp=pc.find_ytdlp):
        self._run, self._ytdlp = run, ytdlp

    @property
    def available(self) -> bool:
        return bool(self._ytdlp())

    async def _call(self, *args: str) -> str:
        binary = self._ytdlp()
        if not binary:
            raise MusicError("не найдена программа yt-dlp")
        try:
            return await self._run(binary, "--no-warnings", *args, timeout=SEARCH_TIMEOUT)
        except pc.PCError as e:
            raise MusicError(f"YouTube не ответил: {e}") from e

    async def search(self, query: str, limit: int) -> list[Track]:
        if not query:
            raise MusicError("что включить? Нужен исполнитель, песня или жанр")
        out = await self._call("--flat-playlist", "--print", "%(id)s\t%(title)s\t%(duration)s", f"ytsearch{limit}:{query}")
        tracks = []
        for line in out.splitlines():
            found = re.fullmatch(r"([\w-]{11})\t([^\t]*)\t([^\t]*)", line)
            if found:
                secs = float(found[3]) if re.fullmatch(r"\d+(\.\d+)?", found[3]) else None
                tracks.append(Track(found[2].strip() or "без названия", duration=secs, source=self.name, ref=found[1]))
        return tracks

    async def resolve(self, track: Track) -> str:
        out = await self._call("-f", "bestaudio[ext=m4a]/bestaudio", "-g", "https://www.youtube.com/watch?v=" + track.ref)
        url = next((line for line in out.splitlines() if line.startswith("http")), "")
        if not url:
            raise MusicError(f"YouTube не дал звук для «{track.title}»")
        return url


class Yandex:
    """Яндекс Музыка через неофициальную библиотеку yandex-music: нужен токен аккаунта с подпиской."""

    name = "yandex"

    def __init__(self, token: str, factory=None):
        self.token, self._factory, self._client = token, factory, None

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def client(self):
        if self._client is None:
            if self._factory is None:
                from yandex_music import ClientAsync

                self._factory = ClientAsync
            self._client = await self._factory(self.token).init()
        return self._client

    def _track(self, t) -> Track | None:
        if t is None or getattr(t, "available", True) is False:
            return None
        ms = getattr(t, "duration_ms", None)
        return Track(t.title or "без названия", ", ".join(a.name for a in (t.artists or []) if a.name),
                     ms / 1000 if ms else None, self.name, str(t.track_id))

    async def _found(self, client, query: str) -> list:
        """Что играть по запросу: «моя волна», «любимое» либо лучший результат поиска и то, что с ним связано."""
        q = words(query)
        if q in ("", "волна", "моя волна", "мою волну"):
            return [s.track for s in (await client.rotor_station_tracks(WAVE)).sequence]
        if q in ("любимое", "мое любимое", "мне нравится", "мои лайки", "любимые треки"):
            likes = await client.users_likes_tracks()
            return await client.tracks([t.track_id for t in likes.tracks[:QUEUE[self.name]]])
        res = await client.search(query)
        best, tracks = (res.best, res.tracks.results if res.tracks else []) if res else (None, [])
        kind, item = (best.type, best.result) if best else ("", None)
        if kind == "artist":
            return (await client.artists_tracks(item.id, page_size=QUEUE[self.name])).tracks
        if kind == "album":
            album = await client.albums_with_tracks(item.id)
            return [t for volume in album.volumes or [] for t in volume]
        if kind == "playlist":
            shorts = (await item.fetch_tracks_async())[:QUEUE[self.name]]
            return await client.tracks([s.track_id for s in shorts])
        if kind == "track":
            return [item, *(t for t in tracks if t.track_id != item.track_id)]
        return tracks

    async def search(self, query: str, limit: int) -> list[Track]:
        try:
            found = await self._found(await self.client(), query)
        except MusicError:
            raise
        except Exception as e:  # библиотека неофициальная: сеть, токен, перемены у Яндекса
            log.warning("Яндекс Музыка: %r", e)
            raise MusicError(f"Яндекс Музыка не ответила: {str(e)[:150] or type(e).__name__}") from e
        return [t for t in map(self._track, found[:limit * 2]) if t][:limit]

    async def resolve(self, track: Track) -> str:
        try:
            infos = await (await self.client()).tracks_download_info(track.ref)
            mp3 = [i for i in infos if i.codec == "mp3"] or infos
            return await max(mp3, key=lambda i: i.bitrate_in_kbps or 0).get_direct_link_async()
        except Exception as e:
            log.warning("Яндекс Музыка: %r", e)
            raise MusicError(f"Яндекс Музыка не отдала трек «{track.label}»") from e


# --- проигрыватель --------------------------------------------------------------------------

async def ffplay(url: str, offset: float):
    args = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", *(["-ss", f"{offset:.1f}"] if offset > 0 else [])]
    try:
        return await asyncio.create_subprocess_exec(*args, url, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise MusicError("не найдена программа ffplay") from e


class Player:
    """Очередь треков и один процесс ffplay. state: stopped, playing или paused."""

    def __init__(self, resolve, spawn=ffplay, clock=time.monotonic):
        self._resolve, self._spawn, self.clock = resolve, spawn, clock
        self.queue: list[Track] = []
        self.index = 0
        self.state = "stopped"
        self.offset = 0.0                    # с какой секунды трека продолжать
        self.error = ""
        self._started: float | None = None   # когда запущен ffplay
        self._proc = None
        self._gen = 0                        # растёт при каждом вмешательстве: старая задача видит это и выходит
        self._task: asyncio.Task | None = None
        self._warm: asyncio.Task | None = None   # адрес потока запрашивается заранее, пока ассистент говорит
        self._held = False                   # молчим ради ассистента; release() продолжит
        self._hold_timer: asyncio.TimerHandle | None = None

    @property
    def active(self) -> bool:
        return self.state != "stopped"

    @property
    def current(self) -> Track | None:
        return self.queue[self.index] if self.active and self.index < len(self.queue) else None

    def _halt(self) -> None:
        self._gen += 1
        proc, self._proc, self._started = self._proc, None, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    def _keep_position(self) -> None:
        if self._started is not None:
            self.offset += self.clock() - self._started

    def _launch(self) -> None:
        self.state = "playing"
        self._halt()
        loop = asyncio.get_running_loop()
        if self._held:  # заиграет, когда ассистент договорит; адрес потока к тому времени уже будет
            self._warm = loop.create_task(self._prefetch(self.queue[self.index]))
        else:
            self._task = loop.create_task(self._play(self._gen))

    async def _prefetch(self, track: Track) -> None:
        if not track.url:
            with contextlib.suppress(MusicError):
                track.url = await self._resolve(track)

    async def _play(self, gen: int) -> None:
        try:
            await self._loop(gen)
        except Exception:  # неожиданный сбой не должен оставить «играет» навсегда
            log.exception("музыка: проигрыватель остановлен из-за ошибки")
            if gen == self._gen:
                self.state = "stopped"

    async def _loop(self, gen: int) -> None:
        fails, retried = 0, False
        while gen == self._gen:
            track = self.queue[self.index]
            code, played, problem = 1, 0.0, ""
            try:
                if self._warm is not None:
                    warm, self._warm = self._warm, None
                    with contextlib.suppress(Exception):
                        await warm
                if not track.url:
                    track.url = await self._resolve(track)
                if gen != self._gen:
                    return
                proc = await self._spawn(track.url, self.offset if track.duration else 0.0)
                if gen != self._gen:  # пока запускался, всё поменялось
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    return
                self._proc, self._started = proc, self.clock()
                code = await proc.wait()
                if gen != self._gen:
                    return            # остановили мы сами: пауза, другой трек, стоп
                played = self.clock() - self._started
            except (MusicError, OSError) as e:
                log.warning("музыка: %s", e)
                problem, retried = str(e), True  # адреса или программы нет вовсе — повторять нечего
            self._proc, self._started = None, None
            if code != 0 and played < FAIL_FAST:
                if not retried:       # адрес потока мог устареть: один раз запрашиваем заново
                    track.url, retried = "", True
                    continue
                fails += 1
                self.error = problem or f"не удалось проиграть «{track.label}»"
            else:
                fails = 0
            retried, self.offset = False, 0.0
            if self.index + 1 >= len(self.queue) or fails >= MAX_FAILS:
                self.state = "stopped"
                return
            self.index += 1

    # --- управление -------------------------------------------------------------------------
    def play(self, tracks: list[Track]) -> None:
        self.queue, self.index, self.offset, self.error = list(tracks), 0, 0.0, ""
        self._launch()

    def stop(self) -> None:
        self._halt()
        self.state, self.queue, self.index, self.offset = "stopped", [], 0, 0.0

    def status(self) -> str:
        track = self.current
        if track is None:
            return "Музыка не играет." + (f" Последняя ошибка: {self.error}." if self.error else "")
        place = f" ({self.index + 1} из {len(self.queue)})" if len(self.queue) > 1 else ""
        return ("Играет: " if self.state == "playing" else "На паузе: ") + track.label + place

    def control(self, action: str) -> str:
        """pause, play, toggle, next, previous, stop — для активного проигрывателя."""
        if action == "toggle":
            action = "pause" if self.state == "playing" else "play"
        if action == "stop":
            self.stop()
            return "Музыка остановлена."
        if not self.active:
            return "Музыка не играет."
        if action == "pause":
            if self.state == "playing":
                self._keep_position()
                self._halt()
                self.state = "paused"
            return "Пауза: " + self.current.label
        if action == "play":
            if self.state == "paused":
                self._launch()
            return "Играет: " + self.current.label
        step = {"next": 1, "previous": -1}.get(action)
        if step is None:
            raise MusicError(f"неизвестное действие: {action}")
        if self.index + step >= len(self.queue):
            self.stop()
            return "Это был последний трек в очереди, музыка остановлена."
        self.index, self.offset = max(0, self.index + step), 0.0
        self._launch()
        return "Играет: " + self.current.label

    # --- тишина ради ассистента -------------------------------------------------------------
    def hold(self, limit: float = HOLD_MAX) -> None:
        """Замолчать, пока ассистент слушает и говорит. Включённое в это время заиграет после release()."""
        if self._hold_timer:
            self._hold_timer.cancel()
        self._hold_timer = asyncio.get_running_loop().call_later(limit, self.release)
        if self._held:
            return
        self._held = True
        if self.state == "playing":
            self._keep_position()
            self._halt()

    def release(self) -> None:
        if self._hold_timer:
            self._hold_timer.cancel()
            self._hold_timer = None
        if self._held:
            self._held = False
            if self.state == "playing":
                self._launch()


# --- Яндекс Станция -------------------------------------------------------------------------

class Station:
    """Команда «включи музыку …» Яндекс Станции через Home Assistant (интеграция YandexStation)."""

    def __init__(self, url: str, token: str, entity: str = "", client: httpx.AsyncClient | None = None):
        self.url, self.token, self.entity = url.rstrip("/"), token, entity
        self.available = bool(url and token)
        self._client = client or httpx.AsyncClient()

    async def _request(self, method: str, path: str, **kw):
        try:
            r = await self._client.request(method, self.url + path, timeout=STATION_TIMEOUT,
                                           headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.HTTPError as e:
            raise MusicError(f"Home Assistant недоступен: {e}") from e
        if r.status_code >= 300:
            raise MusicError(f"Home Assistant вернул {r.status_code}")
        return r.json()

    async def find(self) -> str:
        """Плеер Станции: из JARVIS_STATION_ENTITY либо первый media_player.yandex_station_*."""
        if not self.entity:
            states = await self._request("GET", "/api/states")
            self.entity = next((s["entity_id"] for s in states
                                if str(s.get("entity_id", "")).startswith("media_player.yandex_station")), "")
            if not self.entity:
                raise MusicError("в Home Assistant нет Яндекс Станции (media_player.yandex_station_…)")
        return self.entity

    @staticmethod
    def command(query: str) -> str:
        """Только музыка: знаки убираются, а начало фиксировано — «включи свет» так не передать."""
        q = words(query)[:100].strip()
        if q in ("волна", "моя волна", "мою волну"):
            return "включи мою волну"
        return f"включи музыку {q}" if q else "включи музыку"

    async def play(self, query: str) -> str:
        command = self.command(query)
        await self._request("POST", "/api/services/media_player/play_media", json={
            "entity_id": await self.find(), "media_content_id": command, "media_content_type": "command"})
        return command


# --- всё вместе -----------------------------------------------------------------------------

class Music:
    def __init__(self, sources=(), station: Station | None = None, default: str = "", spawn=ffplay,
                 clock=time.monotonic):
        self.sources = {s.name: s for s in sources if s.available}
        self.station = station if station is not None and station.available else None
        self.default = default if default in self.sources else next(iter(self.sources), "")
        self.player = Player(self._resolve, spawn, clock)

    @property
    def available(self) -> bool:
        return bool(self.sources or self.station)

    async def _resolve(self, track: Track) -> str:
        return await self.sources[track.source].resolve(track)

    def tool(self) -> dict:
        """Описание инструмента music_play под то, что подключено на этой машине."""
        text = ("Включить музыку. query — что включить: исполнитель, песня, альбом, жанр или настроение, "
                "словами пользователя.")
        props: dict = {"query": {"type": "string"}}
        if self.sources and self.station:
            text += " where: pc — на компьютере (по умолчанию), station — на Яндекс Станции, если её назвали."
            props["where"] = {"type": "string", "enum": list(PLACES)}
        elif self.station:
            text += " Музыка играет на Яндекс Станции."
        if len(self.sources) > 1:
            text += (f" source: {', '.join(f'{n} — {SOURCE_NAMES[n]}' for n in self.sources)}; по умолчанию "
                     f"{self.default}, другой — только если пользователь его назвал.")
            props["source"] = {"type": "string", "enum": list(self.sources)}
        if "yandex" in self.sources:
            text += " Яндекс Музыка знает запросы «моя волна» и «моё любимое»."
        return {"name": "music_play", "description": text,
                "input_schema": {"type": "object", "properties": props, "required": ["query"]}}

    async def play(self, args: dict, channel: str = "") -> str:
        """Найти и включить. Возвращает фразу, которую остаётся произнести."""
        query = " ".join(str(args.get("query") or "").split())[:200]
        where = args.get("where") or ("pc" if self.sources else "station")
        if where == "station":
            if self.station is None:
                raise MusicError("Яндекс Станция не подключена")
            command = await self.station.play(query)
            return f"Попросил Станцию: «{command}»."
        if where != "pc" or not self.sources:
            raise MusicError("музыка на компьютере не подключена" if where == "pc" else f"where: {', '.join(PLACES)}")
        name = args.get("source") or self.default
        if name not in self.sources:
            raise MusicError(f"источник «{name}» не подключён; есть: {', '.join(self.sources)}")
        tracks = await self.sources[name].search(query, QUEUE[name])
        if not tracks:
            raise MusicError(f"по запросу «{query}» ничего не нашлось")
        self.player.play(tracks)
        return spoken(tracks[0].label)
