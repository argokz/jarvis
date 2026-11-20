import base64
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis import pc, printing, screen
from jarvis.agent import integrations_hint
from jarvis.config import Settings
from jarvis.printing import PrintError, Printer
from jarvis.scenarios import parse_step
from jarvis.screen import Screen, ScreenError


class Confirm:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    async def ask(self, title, details="", channel="", **kw):
        self.asked.append((title, details, channel))
        return self.answer


class Cups:
    """Поддельные lpstat, lp, pdfinfo и soffice."""

    def __init__(self, default="ML-1430", state="printer ML-1430 is idle.  enabled since Wed", pages=12):
        self.default, self.state, self.pages, self.cmds = default, state, pages, []

    async def __call__(self, *a, timeout=None):
        self.cmds.append(a)
        if a[:2] == ("lpstat", "-d"):
            return f"system default destination: {self.default}\n" if self.default else "no system default destination\n"
        if a[:2] == ("lpstat", "-p"):
            if a[2] not in self.state:
                raise PrintError("lpstat: Invalid destination name")
            return self.state
        if a[0] == "pdfinfo":
            return f"Title: x\nPages:          {self.pages}\n"
        if a[0] == "soffice":
            out = Path(a[a.index("--outdir") + 1]) / (Path(a[-1]).stem + ".pdf")
            if self.pages:
                out.write_bytes(b"%PDF-1.4")
            return ""
        assert a[0] == "lp"
        return "request id is ML-1430-7 (1 file(s))\n"


@pytest.fixture
def docs(tmp_path):
    root = (tmp_path / "docs").resolve()
    root.mkdir()
    for name in ("Договор.pdf", "Скан.png", "Смета.xlsx", "Архив.zip", "Заметка.txt"):
        (root / name).write_bytes(b"x")
    return root


# --- печать --------------------------------------------------------------------------------------

async def test_pdf_is_printed_after_confirmation(docs):
    cups, confirm = Cups(), Confirm()
    out = await Printer((docs,), confirm, cups).print({"path": str(docs / "Договор.pdf")}, "telegram")
    assert out == "Отправил на печать: Договор.pdf, страниц: 12, принтер ML-1430."
    assert confirm.asked == [("Напечатать «Договор.pdf»?",
                              f"Принтер: ML-1430\nСтраниц: 12\nФайл: {docs / 'Договор.pdf'}", "telegram")]
    assert cups.cmds[-1] == ("lp", "-d", "ML-1430", "-n", "1", "-t", "Договор.pdf", "--", str(docs / "Договор.pdf"))


async def test_copies_pages_and_a_printer_that_is_off(docs):
    cups = Cups(state="printer ML-1430 disabled since Wed -\n\tUnplugged or turned off")
    confirm = Confirm()
    out = await Printer((docs,), confirm, cups).print({"path": str(docs / "Договор.pdf"), "copies": 2, "pages": "1-3, 5"})
    assert out == ("Отправил на печать: Договор.pdf, страницы 1-3,5, принтер ML-1430. Но принтер сейчас выключен "
                   "или не подключён: задание встанет в очередь.")
    assert "выключен" in confirm.asked[0][1] and "Страниц: 1-3,5\nКопий: 2" in confirm.asked[0][1]
    assert cups.cmds[-1][:8] == ("lp", "-d", "ML-1430", "-n", "2", "-P", "1-3,5", "-t")
    # принтер из настроек — системный по умолчанию не спрашиваем
    cups = Cups(state="printer Office is idle.")
    await Printer((docs,), Confirm(), cups, default="Office").print({"path": str(docs / "Скан.png")})
    assert ("lpstat", "-d") not in cups.cmds and cups.cmds[-1][2] == "Office"


async def test_spreadsheet_goes_through_libreoffice_and_picture_is_one_page(docs):
    cups, confirm = Cups(pages=3), Confirm()
    out = await Printer((docs,), confirm, cups).print({"path": str(docs / "Смета.xlsx")})
    assert out == "Отправил на печать: Смета.xlsx, страниц: 3, принтер ML-1430."
    office = next(c for c in cups.cmds if c[0] == "soffice")
    assert office[1] == "--headless" and office[-1] == str(docs / "Смета.xlsx") and "-env:UserInstallation" in office[2]
    sent = cups.cmds[-1][-1]
    assert sent.endswith("/Смета.pdf") and not Path(sent).exists()         # временный PDF убран
    assert "Файл: " + str(docs / "Смета.xlsx") in confirm.asked[0][1]       # человеку показан исходный файл
    confirm = Confirm()
    await Printer((docs,), confirm, Cups()).print({"path": str(docs / "Скан.png")})
    assert "Страниц: 1\n" in confirm.asked[0][1]
    await Printer((docs,), confirm, Cups()).print({"path": str(docs / "Заметка.txt")})
    assert "Страниц: неизвестно" in confirm.asked[-1][1]
    with pytest.raises(PrintError, match="LibreOffice не смог"):
        await Printer((docs,), Confirm(), Cups(pages=0)).print({"path": str(docs / "Смета.xlsx")})


async def test_nothing_is_printed_without_a_yes(docs, tmp_path):
    cups = Cups()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"x")
    cases = [({"path": str(outside)}, "вне разрешённых папок"), ({"path": "Договор.pdf"}, "вне разрешённых папок"),
             ({"path": str(docs / "нет.pdf")}, "не найден"), ({"path": str(docs / "Архив.zip")}, "не умею"),
             ({"path": str(docs / "Договор.pdf"), "copies": 50}, "от 1 до 20"),
             ({"path": str(docs / "Договор.pdf"), "copies": "много"}, "целым числом"),
             ({"path": str(docs / "Договор.pdf"), "pages": "все; rm -rf"}, "pages")]
    for args, why in cases:
        with pytest.raises(PrintError, match=why):
            await Printer((docs,), Confirm(), cups).print(args)
    with pytest.raises(PrintError, match="не подтвердил"):
        await Printer((docs,), Confirm(False), cups).print({"path": str(docs / "Договор.pdf")})
    with pytest.raises(PrintError, match="подтверждения"):
        await Printer((docs,), None, cups).print({"path": str(docs / "Договор.pdf")})
    with pytest.raises(PrintError, match="по умолчанию не задан"):
        await Printer((docs,), Confirm(), Cups(default="")).print({"path": str(docs / "Договор.pdf")})
    with pytest.raises(PrintError, match="«Kitchen» не найден"):
        await Printer((docs,), Confirm(), cups, default="Kitchen").print({"path": str(docs / "Договор.pdf")})
    assert not any(c[0] == "lp" for c in cups.cmds)


# --- снимок экрана -------------------------------------------------------------------------------

def portal(tmp_path, code=0, shrink=True, size=10):
    shot = tmp_path / "Снимок экрана.png"
    shot.write_bytes(b"P" * size)
    calls = []

    async def run(*a, timeout=None):
        calls.append(a)
        if a[0] == "/usr/bin/python3":
            return code, str(shot) if code == 0 else "", "портал недоступен: нет сервиса" if code == 4 else ""
        if not shrink:
            return 1, "", "ошибка"
        Path(a[-1]).write_bytes(b"JPEG")
        return 0, "", ""

    return shot, calls, run


async def test_screenshot_is_shrunk_returned_and_not_kept(tmp_path):
    shot, calls, run = portal(tmp_path)
    eye = Screen(run, ffmpeg=lambda: "/usr/bin/ffmpeg")
    assert await eye.capture() == (b"JPEG", "image/jpeg")
    assert not shot.exists()                                           # файл снимка на диске не остаётся
    assert calls[0][:3] == ("/usr/bin/python3", "-I", str(screen.HELPER)) and screen.HELPER.is_file()
    assert calls[1][0] == "/usr/bin/ffmpeg" and "scale=" in calls[1][calls[1].index("-vf") + 1]
    shot.write_bytes(b"PNG")
    result = await eye.look()
    assert result == {"content": [{"type": "image", "data": base64.b64encode(b"JPEG").decode(), "mimeType": "image/jpeg"}]}


async def test_screenshot_without_ffmpeg_goes_as_is_unless_huge(tmp_path):
    shot, _, run = portal(tmp_path)
    assert await Screen(run, ffmpeg=lambda: None).capture() == (b"P" * 10, "image/png")
    shot, _, run = portal(tmp_path, shrink=False)                      # ffmpeg есть, но не справился
    assert await Screen(run, ffmpeg=lambda: "/usr/bin/ffmpeg").capture() == (b"P" * 10, "image/png")
    shot, _, run = portal(tmp_path, size=screen.MAX_BYTES + 1)
    with pytest.raises(ScreenError, match="слишком большой"):
        await Screen(run, ffmpeg=lambda: None).capture()
    assert not shot.exists()                                           # и при ошибке снимок удалён


async def test_screenshot_refusals_are_explained(tmp_path):
    for code, why in ((2, "не разрешён"), (3, "никто не ответил"), (4, "портал недоступен")):
        _, _, run = portal(tmp_path, code=code)
        with pytest.raises(ScreenError, match=why):
            await Screen(run).capture()


# --- уведомление, подсказки ------------------------------------------------------------------------

async def test_notification_on_screen():
    cmds = []

    async def run(*a, timeout=None):
        cmds.append(a)
        return ""

    box = pc.PC(run=run)
    assert await box.call("pc_notify", {"text": "  Чайник  вскипел ", "title": "Кухня"}) == "Показал уведомление на экране."
    assert cmds == [("notify-send", "--app-name=Jarvis", "--", "Кухня", "Чайник вскипел")]
    await box.call("pc_notify", {"text": "--urgency=critical"})       # текст с дефисом — не ключ программы
    assert cmds[-1] == ("notify-send", "--app-name=Jarvis", "--", "Jarvis", "--urgency=critical")
    with pytest.raises(pc.PCError):
        await box.call("pc_notify", {"text": " "})
    assert "pc_notify" in [t["name"] for t in pc.TOOLS] and not pc.needs_answer("pc_notify", {"text": "x"})
    step = parse_step({"notify": "Пора на встречу"})
    assert step.args == {"text": "Пора на встречу"} and step.text == "уведомление на экране: Пора на встречу"


def test_agent_is_told_about_printing_and_screen_only_when_they_are_on(monkeypatch, tmp_path):
    base = replace(Settings(), open_roots=(), screen=False)
    assert "print_file" not in integrations_hint(base) and "screen_look" not in integrations_hint(base)
    monkeypatch.setattr(printing, "available", lambda: True)
    on = replace(base, open_roots=(tmp_path,), screen=True)
    assert "print_file" in integrations_hint(on) and "screen_look" in integrations_hint(on)
    monkeypatch.setattr(printing, "available", lambda: False)
    assert "print_file" not in integrations_hint(on)
