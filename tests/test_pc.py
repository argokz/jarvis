from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from jarvis import pc
from jarvis.agent import Brain, Reply, integrations_hint, pc_tools
from jarvis.config import Settings
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.store import Store

CHROME, VLC = "org.mpris.MediaPlayer2.chromium.instance42", "org.mpris.MediaPlayer2.vlc"


class Shell:
    """Поддельные wpctl и gdbus: помнит громкость и состояние плееров."""

    def __init__(self, volume=0.3, muted=False, players=None):
        self.volume, self.muted, self.players, self.cmds = volume, muted, players or {}, []

    async def __call__(self, *a):
        self.cmds.append(a)
        if a[:2] == ("wpctl", "get-volume"):
            return f"Volume: {self.volume:.2f}" + (" [MUTED]" if self.muted else "") + "\n"
        if a[:2] == ("wpctl", "set-volume"):
            assert a[2:5] == ("-l", "1.0", pc.SINK)
            v, rel = a[5].rstrip("+-"), a[5][-1]
            delta = int(v.rstrip("%")) / 100
            self.volume = min(1.0, max(0.0, self.volume + delta if rel == "+" else self.volume - delta if rel == "-" else delta))
            return ""
        if a[:2] == ("wpctl", "set-mute"):
            self.muted = a[3] == "1"
            return ""
        assert a[:3] == ("gdbus", "call", "--session")
        dest, method = a[4], a[8]
        if method.endswith("ListNames"):
            return "(['org.freedesktop.DBus', ':1.5', " + ", ".join(f"'{n}'" for n in self.players) + "],)"
        if dest not in self.players:
            raise pc.PCError("gdbus: нет такого имени")
        status, meta = self.players[dest]
        if method.endswith("Properties.Get"):
            return f"(<'{status}'>,)" if a[10] == "PlaybackStatus" else f"(<{meta}>,)"
        action = method.rsplit(".", 1)[1]
        new = {"Play": "Playing", "Pause": "Paused", "PlayPause": "Paused" if status == "Playing" else "Playing"}
        self.players[dest] = (new.get(action, status), meta)
        return "()"


META = "{'mpris:trackid': <objectpath '/1'>, 'xesam:title': <'Группа крови'>, 'xesam:artist': <['Кино']>}"
QUOTED = """{'xesam:title': <"Don't Stop Me Now">, 'xesam:artist': <['Queen']>}"""


# --- громкость ----------------------------------------------------------------------------------

async def test_volume_get_set_step_and_mute():
    sh = Shell(volume=0.29)
    box = pc.PC(sh)
    assert await box.volume("get") == "Громкость 29%"
    assert await box.volume("set", 40) == "Громкость 40%"
    assert await box.volume("up") == "Громкость 50%"          # шаг по умолчанию — 10
    assert await box.volume("down", 25) == "Громкость 25%"
    assert await box.volume("set", 250) == "Громкость 100%"   # выше 100% не поднимаем
    assert await box.volume("mute") == "Громкость 100%, звук выключен"
    assert await box.volume("unmute") == "Громкость 100%"
    assert ("wpctl", "set-volume", "-l", "1.0", pc.SINK, "25%-") in sh.cmds


async def test_volume_rejects_bad_input():
    box = pc.PC(Shell())
    for action, value in (("louder", None), ("set", None), ("set", "громко")):
        with pytest.raises(pc.PCError):
            await box.volume(action, value)


# --- плееры -------------------------------------------------------------------------------------

async def test_media_status_reads_track_from_metadata():
    box = pc.PC(Shell(players={CHROME: ("Playing", META), VLC: ("Paused", QUOTED)}))
    assert await box.media("status") == "chromium: играет, Кино — Группа крови\nvlc: на паузе, Queen — Don't Stop Me Now"


async def test_media_pause_play_next_pick_the_right_player():
    sh = Shell(players={CHROME: ("Playing", META), VLC: ("Paused", "{}")})
    box = pc.PC(sh)
    assert await box.media("next") == "Выполнено: chromium"       # следующий — у того, что играет
    assert await box.media("play") == "Уже играет: chromium: играет, Кино — Группа крови"
    assert await box.media("pause") == "Выполнено: chromium"
    assert sh.players[CHROME][0] == "Paused" and sh.players[VLC][0] == "Paused"
    assert await box.media("pause") == "Сейчас ничего не играет."
    assert await box.media("play") == "Выполнено: chromium" and sh.players[CHROME][0] == "Playing"


async def test_media_without_players_is_an_error():
    box = pc.PC(Shell())
    with pytest.raises(pc.PCError, match="нет запущенных плееров"):
        await box.media("pause")
    with pytest.raises(pc.PCError, match="action"):
        await box.media("rewind")


async def test_real_commands_report_missing_program_and_failure():
    with pytest.raises(pc.PCError, match="не найдена программа"):
        await pc._run("jarvis-no-such-program")
    with pytest.raises(pc.PCError):
        await pc._run("false")
    assert (await pc._run("echo", "ok")).strip() == "ok"


# --- браузер ------------------------------------------------------------------------------------

def test_open_url_allows_only_web_addresses():
    opened = []
    box = pc.PC(Shell(), opener=opened.append)
    assert box.open_url(" https://habr.com/ru/ ") == "Открыл https://habr.com/ru/"
    assert box.open_url("habr.com") == "Открыл https://habr.com"          # схему дописываем сами
    assert opened == ["https://habr.com/ru/", "https://habr.com"]
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "/home/user/a.sh", "tg://resolve?domain=x",
                "https://", "https://a.com/x y", "", "--version"):
        with pytest.raises(pc.PCError):
            box.open_url(bad)
    assert len(opened) == 2


def test_opened_program_is_detached_from_the_core_service():
    from jarvis import files
    plain = files.open_command("https://a.com", env={}, which=lambda n: "/usr/bin/" + n)
    assert plain == ["xdg-open", "https://a.com"]
    unit = files.open_command("https://a.com", env={"INVOCATION_ID": "abc"}, which=lambda n: "/usr/bin/" + n)
    assert unit == ["systemd-run", "--user", "--scope", "--quiet", "--", "xdg-open", "https://a.com"]
    assert files.open_command("/f.pdf", env={"INVOCATION_ID": "abc"}, which=lambda n: None) == ["xdg-open", "/f.pdf"]


class Search:
    """Поддельный yt-dlp."""

    def __init__(self, out="", fail=False):
        self.out, self.fail, self.cmds = out, fail, []

    async def __call__(self, *a, timeout=None):
        self.cmds.append((a, timeout))
        if self.fail:
            raise pc.PCError("yt-dlp не ответил вовремя")
        return self.out


async def test_youtube_opens_first_found_video():
    opened, search = [], Search("xKpzH5bxYsk\tВиктор Цой - Группа Крови\tfoleywarlocks\n")
    box = pc.PC(search, opener=opened.append, ytdlp=lambda: "/bin/yt-dlp")
    assert await box.youtube("  кино   группа крови ") == "Открыл на YouTube: Виктор Цой - Группа Крови (канал foleywarlocks)"
    assert opened == ["https://www.youtube.com/watch?v=xKpzH5bxYsk"]
    args, timeout = search.cmds[0]
    assert args[0] == "/bin/yt-dlp" and args[-1] == "ytsearch1:кино группа крови" and timeout == pc.SEARCH_TIMEOUT


async def test_youtube_falls_back_to_search_page():
    for box_kw in (dict(run=Search(fail=True), ytdlp=lambda: "/bin/yt-dlp"),   # поиск упал
                   dict(run=Search("ERROR: что-то не так"), ytdlp=lambda: "/bin/yt-dlp"),  # ответ без id
                   dict(run=Search(), ytdlp=lambda: None)):                    # yt-dlp не установлен
        opened = []
        text = await pc.PC(opener=opened.append, **box_kw).youtube("кино & цой")
        assert opened == ["https://www.youtube.com/results?search_query=%D0%BA%D0%B8%D0%BD%D0%BE+%26+%D1%86%D0%BE%D0%B9"]
        assert "страницу поиска" in text
    with pytest.raises(pc.PCError, match="пустой запрос"):
        await pc.PC(Search(), opener=opened.append).youtube("   ")


def test_only_questions_and_youtube_need_a_follow_up_answer():
    assert pc.needs_answer("pc_volume", {"action": "get"}) and pc.needs_answer("pc_media", {"action": "status"})
    assert pc.needs_answer("pc_youtube", {"query": "x"})
    assert not pc.needs_answer("pc_volume", {"action": "up"}) and not pc.needs_answer("pc_open_url", {"url": "x"})


# --- подключение к моделям ----------------------------------------------------------------------

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


class FakeBrain:
    def __init__(self):
        self.notes = []

    def journal_note(self, line):
        self.notes.append(line)

    async def ask(self, user, text, channel="api"):
        return Reply("Ответ агента.")


def settings(tmp_path, **kw) -> Settings:
    return replace(Settings(), vault=(tmp_path / "vault").resolve(), db=tmp_path / "j.db", hass_url="", hass_token="",
                   notion_token="", notion_parent="", github_token="", open_roots=(), **kw)


def router(tmp_path, client, shell, **kw):
    s = settings(tmp_path, pc_control=True)
    store = Store(s.db)
    return VoiceRouter(s, FakeBrain(), store, Notifier(), client=client, computer=pc.PC(shell, **kw)), store


def use(name, args):
    return NS(type="tool_use", id="tu_1", name=name, input=args)


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_router_changes_volume_itself_in_one_model_call(tmp_path):
    sh = Shell(volume=0.3)
    client = Client((["Делаю тише."], "tool_use", [use("pc_volume", {"action": "down"})]))
    r, store = router(tmp_path, client, sh)
    assert await say(r, "сделай потише") == "Делаю тише."
    assert sh.volume == pytest.approx(0.2) and len(client.calls) == 1
    kw = client.calls[0]
    assert [t["name"] for t in kw["tools"]] == ["delegate", "pc_volume", "pc_media", "pc_open_url", "pc_youtube",
                                              "pc_notify"]
    assert "pc_volume" in kw["system"][0]["text"] and "Hass" not in kw["system"][0]["text"]  # дома нет — про колонку не упоминаем
    tool = [e for e in store.recent() if e["kind"] == "tool"]
    assert tool[0]["name"] == "pc_volume"


async def test_router_answers_what_is_playing_from_tool_result(tmp_path):
    sh = Shell(players={CHROME: ("Playing", META)})
    client = Client(([], "tool_use", [use("pc_media", {"action": "status"})]),
                    (["Играет Кино, Группа крови."], "end_turn", []))
    r, _ = router(tmp_path, client, sh)
    assert await say(r, "что играет?") == "Играет Кино, Группа крови."
    assert client.calls[1]["messages"][-1]["content"][0]["content"] == "chromium: играет, Кино — Группа крови"


async def test_router_explains_when_nothing_to_pause(tmp_path):
    client = Client((["Ставлю на паузу."], "tool_use", [use("pc_media", {"action": "pause"})]),
                    (["Сейчас на компьютере ничего не играет."], "end_turn", []))
    r, _ = router(tmp_path, client, Shell())
    assert await say(r, "пауза") == "Ставлю на паузу. Сейчас на компьютере ничего не играет."
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_router_plays_youtube_names_the_video_and_journals_it(tmp_path):
    opened, search = [], Search("xKpzH5bxYsk\tГруппа Крови\tКино\n")
    client = Client((["Сейчас найду."], "tool_use", [use("pc_youtube", {"query": "кино группа крови"})]),
                    (["Включил «Группу крови»."], "end_turn", []))
    r, _ = router(tmp_path, client, search, opener=opened.append, ytdlp=lambda: "/bin/yt-dlp")
    assert await say(r, "включи группу крови на ютубе") == "Сейчас найду. Включил «Группу крови»."
    assert opened == ["https://www.youtube.com/watch?v=xKpzH5bxYsk"]
    assert r.brain.notes == ["браузер, pc_youtube: кино группа крови (voice)"]
    assert "Группа Крови" in client.calls[1]["messages"][-1]["content"][0]["content"]


async def test_router_opens_site_and_refuses_non_web_address(tmp_path):
    opened = []
    client = Client((["Открываю Хабр."], "tool_use", [use("pc_open_url", {"url": "https://habr.com"})]))
    r, _ = router(tmp_path, client, Shell(), opener=opened.append)
    assert await say(r, "открой хабр") == "Открываю Хабр." and opened == ["https://habr.com"]
    client = Client(([], "tool_use", [use("pc_open_url", {"url": "file:///etc/passwd"})]),
                    (["Такой адрес открыть не могу."], "end_turn", []))
    r, _ = router(tmp_path, client, Shell(), opener=opened.append)
    assert await say(r, "открой файл") == "Такой адрес открыть не могу." and len(opened) == 1


def test_tools_are_off_by_default_and_reach_the_agent_when_enabled(tmp_path):
    off = settings(tmp_path, pc_control=False)
    assert "pc_volume" not in integrations_hint(off)
    assert "pc_volume" not in VoiceRouter(off, None, None, None).system()
    on = settings(tmp_path, pc_control=True)
    assert "pc_volume" in integrations_hint(on)
    b = Brain(on, Store(on.db))
    assert "pc_volume" in b._options("owner", "api", None).system_prompt and b._tools_server() is not None


async def test_agent_tools_run_the_same_commands_and_report_errors():
    sh = Shell(volume=0.3)
    volume, media, *rest = pc_tools(pc.PC(sh))
    assert [t.name for t in (volume, media, *rest)] == ["pc_volume", "pc_media", "pc_open_url", "pc_youtube",
                                                       "pc_notify"]
    assert volume.input_schema["properties"]["action"]["enum"] == list(pc.VOLUME_ACTIONS)
    ok = await volume.handler({"action": "set", "value": 55})
    assert ok == {"content": [{"type": "text", "text": "Громкость 55%"}]} and sh.volume == pytest.approx(0.55)
    bad = await media.handler({"action": "pause"})
    assert bad["is_error"] is True and "нет запущенных плееров" in bad["content"][0]["text"]


def test_pc_control_flag(monkeypatch):
    for raw, want in (("", False), ("0", False), ("1", True), ("true", True), ("Yes", True)):
        monkeypatch.setenv("JARVIS_PC_CONTROL", raw)
        assert Settings().pc_control is want
