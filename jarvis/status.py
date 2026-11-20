"""Сводка «что ассистент умеет и что из этого сейчас работает» — для экрана «Функции» в приложении.

Состояния функции: ready — работает; setup — нужен ключ или настройка; off — выключена в .env;
down — настроена, но не отвечает. У функции есть фразы для пробы: fast уходит быстрой модели
(тот же путь, что у голоса, доли цента за ход), agent — агенту (несколько центов за ход).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import printing, registry
from .speech import SpeechError

log = logging.getLogger("jarvis.status")
PROBE_TIMEOUT = 1.5   # свои серверы речи стоят на этой же машине
HOME_TIMEOUT = 4.0
CHECK_PHRASE = "Проверка связи. Меня хорошо слышно?"


def words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def feature(id: str, title: str, state: str, note: str = "", hint: str = "", tries=()) -> dict:
    return {"id": id, "title": title, "state": state, "note": note, "hint": hint, "tries": list(tries)}


def try_(text: str, via: str = "fast", effect: str = "") -> dict:
    return {"text": text, "via": via, "effect": effect}


class Status:
    def __init__(self, settings, store, speech, home, tunes, scenarios, scheduler, voice, notifier, web=None,
                 client: httpx.AsyncClient | None = None, env=os.getenv, clock=time.time, backup=None):
        self.backup = backup
        self.s, self.store, self.speech, self.home, self.tunes = settings, store, speech, home, tunes
        self.scenarios, self.scheduler, self.voice, self.notifier, self.web = scenarios, scheduler, voice, notifier, web
        self._client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT)
        self._env, self._clock = env, clock

    async def _alive(self, url: str) -> bool:
        """Сервер отвечает: любой ответ по HTTP, даже 404, значит, что он запущен."""
        parts = urlsplit(url)
        try:
            await self._client.get(f"{parts.scheme}://{parts.netloc}/health")
        except httpx.HTTPError:
            return False
        return True

    async def _voice(self) -> dict:
        name, sp = self.s.assistant_name, self.speech
        tries = [try_("Который час?", effect="ответит вслух в этом окне")]
        aloud = f"Вслух: «{name}, который час?» — когда включён микрофон."
        if not sp.enabled:
            return feature("voice", "Голос", "setup", "Распознавание и синтез речи не настроены.",
                           "Задайте JARVIS_STT_URL и JARVIS_TTS_URL (свои серверы) или YANDEX_SPEECHKIT_API_KEY.")
        mics = len(self.voice.sessions) if self.voice else 0
        heard = f"микрофонов на связи: {mics}" if mics else "микрофон не подключён — включите «Слушать имя»"
        if not getattr(sp, "local", False):
            return feature("voice", "Голос", "ready", f"Yandex SpeechKit; {heard}. {aloud}", tries=tries)
        stt, tts = await asyncio.gather(self._alive(sp.stt_url), self._alive(sp.tts_url))
        if not (stt and tts):
            dead = " и ".join(n for n, ok in (("распознавание речи", stt), ("синтез речи", tts)) if not ok)
            return feature("voice", "Голос", "down", f"Не отвечает {dead}.",
                           "Запустите службы jarvis-stt и jarvis-tts (на этом ПК — кнопками ниже).")
        voice = f", голос {sp.tts_voice}" if sp.tts_voice else ""
        return feature("voice", "Голос", "ready", f"Свои серверы речи отвечают{voice}; {heard}. {aloud}", tries=tries)

    async def _home(self) -> dict:
        if not self.home or not self.home.enabled:
            return feature("home", "Умный дом", "setup", "Home Assistant не подключён.",
                           "Впишите HASS_URL и HASS_TOKEN в .env.")
        try:
            got = await asyncio.wait_for(self.home.context(), HOME_TIMEOUT)
        except (asyncio.TimeoutError, Exception):  # сводка не должна падать из-за чужого сервера
            got = None
        if got is None:
            return feature("home", "Умный дом", "down", "Home Assistant не отвечает.",
                           "Проверьте, что Home Assistant запущен и токен действует.")
        return feature("home", "Умный дом", "ready",
                       "Home Assistant на связи. Устройства, команды и добавление новых — на вкладке «Дом».", tries=[
            try_("Какие устройства есть дома?", effect="только перечислит, ничего не включит")])

    def _features(self) -> list[dict]:
        s, out = self.s, []
        has_key = bool(self._env("ANTHROPIC_API_KEY"))
        out.append(feature(
            "agent", "Разговор и поручения", "ready" if has_key else "setup",
            f"Агент — {s.model}, быстрая модель — {s.router_model}.", "" if has_key else "Впишите ANTHROPIC_API_KEY в .env.",
            [try_("Что ты умеешь? Ответь в пяти пунктах.", "agent")] if has_key else []))

        planned = len(self.scheduler.active()) if self.scheduler else 0
        out.append(feature("schedule", "Таймеры, напоминания, будильник", "ready", f"Запланировано: {planned}.", tries=[
            try_("Поставь таймер на одну минуту", effect="через минуту зазвенит; выключается кнопкой «Стоп» или словом «стоп»"),
            try_("Что у меня запланировано?")]))

        if s.pc_control:
            out.append(feature("pc", "Компьютер: громкость, плееры, сайты", "ready", tries=[
                try_("Какая сейчас громкость?"),
                try_("Открой сайт wikipedia.org", effect="откроется вкладка в браузере")]))
        else:
            out.append(feature("pc", "Компьютер: громкость, плееры, сайты", "off", hint="JARVIS_PC_CONTROL=1 в .env."))

        tunes = self.tunes
        if tunes is not None and tunes.available:
            where = [f"на ПК: {', '.join(tunes.sources)}"] if tunes.sources else []
            where += ["на Яндекс Станции"] if tunes.station else []
            waits = "" if s.yandex_music_token else " Яндекс Музыка ждёт YANDEX_MUSIC_TOKEN."
            out.append(feature("music", "Музыка", "ready", f"Играет {' и '.join(where)}.{waits}", tries=[
                try_("Включи спокойную музыку", effect="заиграет на ПК, где запущено ядро"),
                try_("Останови музыку")] if tunes.sources else []))
        else:
            out.append(feature("music", "Музыка", "off", hint="JARVIS_PC_CONTROL=1 (музыка на ПК) или Home Assistant со Станцией."))

        scenes = self.scenarios.load() if self.scenarios else []
        broken = sum(1 for sc in scenes if sc.error)
        out.append(feature(
            "scenarios", "Сценарии", "ready",
            f"Сценариев: {len(scenes)}" + (f", с ошибкой: {broken}" if broken else "") + ". Запуск — на вкладке «Сценарии».",
            tries=[try_("Какие у меня есть сценарии?")]))

        projects = len(registry.load_projects(s.vault))
        out.append(feature("memory", "Память и проекты", "ready", f"Заметки в vault, проектов: {projects}.", tries=[
            try_("Какие у меня проекты и что по ним открыто?", "agent")]))

        if s.open_roots:
            out.append(feature("files", "Файлы на ПК", "ready", f"Папок для поиска: {len(s.open_roots)}.", tries=[
                try_("Найди файл roadmap", "agent", "покажет найденное; откроет, если попросить")]))
        else:
            out.append(feature("files", "Файлы на ПК", "off", hint="Перечислите папки в JARVIS_OPEN_ROOTS."))

        if not s.browser:
            out.append(feature("browser", "Интернет через браузер", "off", hint="JARVIS_BROWSER=1 в .env."))
        elif self.web is None or not self.web.available:
            out.append(feature("browser", "Интернет через браузер", "setup", "Playwright MCP не найден.",
                               f"Установите его в {s.browser_dir} (README, раздел про браузер)."))
        else:
            out.append(feature("browser", "Интернет через браузер", "ready", "Помощник с браузером ищет и читает сайты.", tries=[
                try_("Найди в интернете, сколько лететь от Земли до Марса", "agent",
                     "откроется окно Chrome; дороже обычного хода")]))

        if s.screen:
            out.append(feature("screen", "Экран", "ready", tries=[
                try_("Что у меня на экране?", "agent", "система спросит разрешение на снимок")]))
        else:
            out.append(feature("screen", "Экран", "off", hint="JARVIS_SCREEN=1 в .env."))

        if printing.available() and s.open_roots:
            out.append(feature("print", "Печать", "ready",
                               "Скажите агенту «распечатай файл …» — перед печатью он спросит подтверждение."))
        else:
            out.append(feature("print", "Печать", "setup", hint="Нужны CUPS (команды lp, lpstat) и папки в JARVIS_OPEN_ROOTS."))

        if s.notion_token:
            out.append(feature("notion", "Notion", "ready", tries=[try_("Что есть в Notion по слову «план»?", "agent")]))
        else:
            out.append(feature("notion", "Notion", "setup", hint="Впишите NOTION_TOKEN в .env."))

        if s.telegram_token and s.telegram_allowed:
            out.append(feature("telegram", "Telegram", "ready", "Бот запущен: напишите ему или отправьте голосовое."))
        else:
            out.append(feature("telegram", "Telegram", "setup", hint="Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_IDS."))

        if self.notifier is not None and self.notifier.ntfy:
            out.append(feature("push", "Уведомления на телефон", "ready", "Через ntfy: подтверждения, напоминания, будильник."))
        else:
            out.append(feature("push", "Уведомления на телефон", "setup", hint="Задайте NTFY_URL и NTFY_TOPIC."))
        if self.backup is not None and self.backup.enabled:
            out.append(feature("backup", "Резервная копия", "ready", self.backup.summary()))
        else:
            out.append(feature("backup", "Резервная копия", "off",
                               hint="Укажите папку в JARVIS_BACKUP_DIR — лучше на другом диске."))
        return out

    async def collect(self) -> dict:
        voice, home = await asyncio.gather(self._voice(), self._home())
        rest = self._features()
        features = [rest[0], voice, *rest[1:4], home, *rest[4:]]
        midnight = datetime.fromtimestamp(self._clock()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        spent, turns = self.store.spent(midnight)
        return {
            "name": self.s.assistant_name,
            "wake": list(self.voice.wake.phrases) if self.voice and self.voice.wake else [],
            "mics": len(self.voice.sessions) if self.voice else 0,
            "spent_today": {"usd": spent, "turns": turns},  # агент и быстрая модель вместе
            "features": features,
        }

    async def speech_check(self) -> dict:
        """Синтез фразы и её распознавание обратно: проверяет оба сервера речи без микрофона и колонок."""
        sp, out = self.speech, {"ok": False, "said": CHECK_PHRASE, "heard": "", "tts_s": None, "stt_s": None, "error": ""}
        if not sp.enabled:
            return {**out, "error": "Речь не настроена."}
        try:
            start = time.perf_counter()
            pcm = await sp.synthesize(CHECK_PHRASE)
            out["tts_s"] = round(time.perf_counter() - start, 2)
            start = time.perf_counter()
            out["heard"] = await sp.recognize(pcm, fmt="lpcm", sample_rate=sp.tts_rate)
            out["stt_s"] = round(time.perf_counter() - start, 2)
        except SpeechError as e:
            return {**out, "error": str(e)}
        out["ok"] = words(out["heard"]) == words(CHECK_PHRASE)
        return out


WATCHED = ("voice", "home")  # то, что сводка опрашивает вживую


class Watch:
    """Сторож: раз в interval секунд смотрит сводку и сообщает, когда функция перестала отвечать и когда вернулась.

    Сообщает после второго неудачного опроса подряд: перезапуск службы на полминуты — не поломка.
    """
    def __init__(self, status: Status, notifier, bus=None, interval: float = 300.0, sleep=asyncio.sleep):
        self.status, self.notifier, self.bus, self.interval, self._sleep = status, notifier, bus, interval, sleep
        self.fails: dict[str, int] = {}  # функция → сколько опросов подряд она не отвечает

    async def check(self) -> list[str]:
        said = []
        for f in (await self.status.collect())["features"]:
            if f["id"] not in WATCHED:
                continue
            before = self.fails.get(f["id"], 0)
            if f["state"] == "down":
                self.fails[f["id"]] = before + 1
                if before == 1:
                    said.append(f"⚠️ {f['title']}: {f['note']} {f['hint']}".strip())
            else:
                self.fails.pop(f["id"], None)
                if before >= 2:
                    said.append(f"✅ {f['title']}: снова работает.")
        for text in said:
            if self.bus is not None:  # приложение покажет полосу, даже если уведомления не настроены
                self.bus.publish({"type": "health", "text": text})
            if self.notifier is not None and self.notifier.available:
                await self.notifier.send(text, title="Jarvis: состояние")
        return said

    async def run(self) -> None:
        while True:
            await self._sleep(self.interval)
            try:
                await self.check()
            except Exception:
                log.exception("сторож: опрос не удался")
