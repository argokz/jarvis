"""Компьютер, где запущено ядро: громкость (PipeWire, wpctl), плееры (MPRIS по D-Bus, gdbus),
открыть сайт или видео YouTube в браузере по умолчанию.

MPRIS поддерживают браузеры и почти все плееры, поэтому «пауза» и «следующий трек» работают и для
YouTube или Яндекс Музыки во вкладке. Включается переменной JARVIS_PC_CONTROL — ядро на сервере без
экрана и звука этих инструментов не получит.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from . import files

SINK = "@DEFAULT_AUDIO_SINK@"
MPRIS = "org.mpris.MediaPlayer2."
STEP = 10
TIMEOUT = 3.0
SEARCH_TIMEOUT = 15.0  # поиск видео идёт в интернет

VOLUME_ACTIONS = ("get", "set", "up", "down", "mute", "unmute")
MEDIA_ACTIONS = {"play": "Play", "pause": "Pause", "toggle": "PlayPause", "next": "Next", "previous": "Previous",
                 "stop": "Stop"}
STATUS = {"Playing": "играет", "Paused": "на паузе", "Stopped": "остановлен"}

# Описания для обеих моделей: быстрый роутер получает их как есть, агент — через свой MCP-сервер.
VOLUME_TOOL = {
    "name": "pc_volume",
    "description": "Громкость звука на компьютере. action: get — узнать, set — установить value процентов, "
                   "up / down — прибавить или убавить на value процентов (по умолчанию 10), mute / unmute — "
                   "выключить или вернуть звук.",
    "input_schema": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": list(VOLUME_ACTIONS)},
                       "value": {"type": "integer", "minimum": 0, "maximum": 100}},
        "required": ["action"],
    },
}
MEDIA_TOOL = {
    "name": "pc_media",
    "description": "Воспроизведение на компьютере: музыка, которую включил ассистент, браузер или плеер. "
                   "action: status — что играет, pause, play — продолжить, toggle, next — следующий трек, "
                   "previous — предыдущий, stop — выключить совсем.",
    "input_schema": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["status", *MEDIA_ACTIONS]}},
        "required": ["action"],
    },
}
OPEN_TOOL = {
    "name": "pc_open_url",
    "description": "Открыть сайт в браузере на компьютере. url — адрес с http:// или https://.",
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
}
YOUTUBE_TOOL = {
    "name": "pc_youtube",
    "description": "Найти видео на YouTube и открыть его в браузере на компьютере. query — что искать: "
                   "название, исполнитель, тема. Возвращает название открытого видео.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}
NOTIFY_TOOL = {
    "name": "pc_notify",
    "description": "Показать уведомление на экране компьютера. text — текст, title — заголовок (необязательно).",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "title": {"type": "string"}},
                     "required": ["text"]},
}
TOOLS = [VOLUME_TOOL, MEDIA_TOOL, OPEN_TOOL, YOUTUBE_TOOL, NOTIFY_TOOL]


class PCError(Exception):
    pass


async def _run(*args: str, timeout: float = TIMEOUT) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise PCError(f"не найдена программа {args[0]}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise PCError(f"{args[0]} не ответил вовремя") from e
    if proc.returncode:
        raise PCError(f"{args[0]}: {err.decode(errors='replace').strip()[:200] or proc.returncode}")
    return out.decode(errors="replace")


def needs_answer(name: str, args: dict) -> bool:
    """По результату вызова модель должна ещё ответить: спросили состояние или нашли видео."""
    return name == "pc_youtube" or args.get("action") in ("get", "status")


def clean_url(url: str) -> str:
    """Только http(s): file://, javascript: и свои схемы приложений браузеру не передаём."""
    url = url.strip()
    if "://" not in url and re.fullmatch(r"[\w.-]+\.[a-zA-Zа-яА-Я]{2,}(/\S*)?", url):
        url = "https://" + url  # «habr.com» без схемы
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or re.search(r"[\s\x00-\x1f]", url):
        raise PCError("открыть можно только адрес сайта, начинающийся с http:// или https://")
    return url


def find_ytdlp() -> str | None:
    """yt-dlp из PATH или из ~/.local/bin (в PATH сервиса systemd этой папки нет)."""
    local = Path.home() / ".local" / "bin" / "yt-dlp"
    return shutil.which("yt-dlp") or (str(local) if local.exists() else None)


class PC:
    def __init__(self, run=_run, opener=files.open_path, ytdlp=find_ytdlp, player=None):
        self._run, self._open, self._ytdlp = run, opener, ytdlp
        self.player = player  # музыка ассистента (music.Player): ей команды достаются первой

    async def call(self, name: str, args: dict) -> str:
        if name == "pc_volume":
            return await self.volume(str(args.get("action", "")), args.get("value"))
        if name == "pc_media":
            return await self.media(str(args.get("action", "")))
        if name == "pc_open_url":
            return self.open_url(str(args.get("url", "")))
        if name == "pc_youtube":
            return await self.youtube(str(args.get("query", "")))
        if name == "pc_notify":
            return await self.notify(str(args.get("text", "")), str(args.get("title") or ""))
        raise PCError(f"нет такого инструмента: {name}")

    # --- громкость ----------------------------------------------------------
    async def _volume(self) -> str:
        out = await self._run("wpctl", "get-volume", SINK)
        m = re.search(r"([\d.]+)", out)
        if not m:
            raise PCError(f"не понял ответ wpctl: {out.strip()[:80]}")
        level = f"Громкость {round(float(m[1]) * 100)}%"
        return level + ", звук выключен" if "MUTED" in out else level

    async def volume(self, action: str, value=None) -> str:
        if action not in VOLUME_ACTIONS:
            raise PCError(f"action должен быть одним из: {', '.join(VOLUME_ACTIONS)}")
        if action in ("set", "up", "down"):
            if value is None and action == "set":
                raise PCError("для set нужно value — проценты от 0 до 100")
            try:
                pct = max(0, min(100, int(STEP if value is None else value)))
            except (TypeError, ValueError) as e:
                raise PCError("value должен быть целым числом процентов") from e
            arg = f"{pct}%" + {"set": "", "up": "+", "down": "-"}[action]
            await self._run("wpctl", "set-volume", "-l", "1.0", SINK, arg)  # -l 1.0: не выше 100%
        elif action in ("mute", "unmute"):
            await self._run("wpctl", "set-mute", SINK, "1" if action == "mute" else "0")
        return await self._volume()

    # --- плееры -------------------------------------------------------------
    async def _gdbus(self, dest: str, path: str, method: str, *args: str) -> str:
        return await self._run("gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                               "--method", method, *args)

    async def _prop(self, player: str, name: str) -> str:
        return await self._gdbus(player, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get",
                                 "org.mpris.MediaPlayer2.Player", name)

    async def players(self) -> list[dict]:
        names = await self._gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames")
        out = []
        for bus in sorted(set(re.findall(r"'(" + re.escape(MPRIS) + r"[^']+)'", names))):
            try:
                status = re.search(r"'(\w+)'", await self._prop(bus, "PlaybackStatus"))
                meta = await self._prop(bus, "Metadata")
            except PCError:
                continue  # плеер закрылся между запросами
            title = re.search(r"'xesam:title': <(['\"])(.*?)\1>", meta)
            artist = re.search(r"'xesam:artist': <\[(['\"])(.*?)\1", meta)
            track = " — ".join(x[2] for x in (artist, title) if x and x[2])
            out.append({"bus": bus, "app": bus.removeprefix(MPRIS).split(".")[0],
                        "status": status[1] if status else "", "track": track})
        return out

    @staticmethod
    def _describe(p: dict) -> str:
        return f"{p['app']}: {STATUS.get(p['status'], p['status'] or 'неизвестно')}" + (f", {p['track']}" if p["track"] else "")

    async def media(self, action: str) -> str:
        if action != "status" and action not in MEDIA_ACTIONS:
            raise PCError(f"action должен быть одним из: status, {', '.join(MEDIA_ACTIONS)}")
        own = self.player if self.player is not None and self.player.active else None
        if own and action != "status":
            return own.control(action)
        try:
            players = await self.players()
        except PCError:
            if not own:
                raise
            players = []
        if not players and not own:
            raise PCError("на компьютере нет запущенных плееров: ничего не играет ни в браузере, ни в приложениях")
        if action == "status":
            return "\n".join([*([own.status()] if own else []), *map(self._describe, players)])
        playing = [p for p in players if p["status"] == "Playing"]
        if action in ("pause", "stop"):
            targets = playing            # на паузу — всё, что играет
        elif action == "play":
            targets = [] if playing else [p for p in players if p["status"] == "Paused"][:1] or players[:1]
        else:
            targets = (playing or players)[:1]
        for p in targets:
            await self._gdbus(p["bus"], "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player." + MEDIA_ACTIONS[action])
        if not targets:
            return "Уже играет: " + self._describe(playing[0]) if action == "play" else "Сейчас ничего не играет."
        return "Выполнено: " + ", ".join(p["app"] for p in targets)

    # --- уведомление ----------------------------------------------------------
    async def notify(self, text: str, title: str = "") -> str:
        text, title = " ".join(text.split())[:500], " ".join(title.split())[:80]
        if not text:
            raise PCError("пустое уведомление: нужен text")
        # «--» отделяет текст от ключей программы: он может начинаться с дефиса
        await self._run("notify-send", "--app-name=Jarvis", "--", title or "Jarvis", text)
        return "Показал уведомление на экране."

    # --- браузер ------------------------------------------------------------
    def open_url(self, url: str) -> str:
        url = clean_url(url)
        self._open(url)
        return f"Открыл {url}"

    async def youtube(self, query: str) -> str:
        query = " ".join(query.split())
        if not query:
            raise PCError("пустой запрос: что искать на YouTube?")
        found = None
        binary = self._ytdlp()
        if binary:
            try:
                out = await self._run(binary, "--flat-playlist", "--no-warnings", "--print",
                                      "%(id)s\t%(title)s\t%(channel)s", "ytsearch1:" + query, timeout=SEARCH_TIMEOUT)
                found = re.match(r"([\w-]{11})\t([^\t\n]*)\t?([^\t\n]*)", out)
            except PCError:
                found = None  # поиск не сработал — ниже откроем страницу результатов
        if not found:
            self._open("https://www.youtube.com/results?search_query=" + quote_plus(query))
            return f"Точное видео найти не удалось, открыл страницу поиска YouTube по запросу «{query}»."
        self._open("https://www.youtube.com/watch?v=" + found[1])
        channel = found[3] if found[3] not in ("", "NA") else ""
        return f"Открыл на YouTube: {found[2]}" + (f" (канал {channel})" if channel else "")
