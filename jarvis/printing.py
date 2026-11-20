"""Печать файла на принтере CUPS — только из разрешённых папок и только после подтверждения.

PDF, картинки и текст уходят в `lp` как есть; документы и таблицы сначала превращаются в PDF через
LibreOffice (`soffice --headless`). Перед печатью пользователь видит файл, принтер, число страниц и
копий и отвечает «да» или «нет»: бумагу и чужой принтер назад не вернёшь.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import files

DIRECT = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".txt"})
OFFICE = frozenset({".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"})
IMAGES = frozenset({".png", ".jpg", ".jpeg", ".gif"})
MAX_BYTES = 100 * 1024 * 1024
MAX_COPIES = 20
TIMEOUT = 15.0
CONVERT_TIMEOUT = 120.0

TOOL = {
    "name": "print_file",
    "description": "Напечатать файл на принтере. path — полный путь из find_files. copies — число копий (по "
                   "умолчанию 1). pages — какие страницы, например «1-3,5»; пусто — все. Пользователь увидит "
                   "принтер и число страниц и должен подтвердить. Печатаются PDF, картинки, текст, документы и "
                   "таблицы (docx, xlsx, odt и подобные).",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "copies": {"type": "integer", "minimum": 1, "maximum": MAX_COPIES},
                       "pages": {"type": "string"}},
        "required": ["path"],
    },
}


class PrintError(Exception):
    pass


async def _run(*args: str, timeout: float = TIMEOUT) -> str:
    """Команда с английскими сообщениями: вывод lpstat разбирается по словам."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError as e:
        raise PrintError(f"не найдена программа {args[0]}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise PrintError(f"{args[0]} не ответил вовремя") from e
    if proc.returncode:
        raise PrintError(f"{args[0]}: {err.decode(errors='replace').strip()[:200] or proc.returncode}")
    return out.decode(errors="replace")


def available() -> bool:
    return bool(shutil.which("lp") and shutil.which("lpstat"))


class Printer:
    def __init__(self, roots: tuple[Path, ...], confirm=None, run=_run, default: str = ""):
        self.roots, self.confirm, self._run, self.default = roots, confirm, run, default

    async def target(self) -> tuple[str, str]:
        """Принтер и предупреждение о его состоянии (пусто — готов)."""
        name = self.default
        if not name:
            found = re.search(r"destination:\s*(\S+)", await self._run("lpstat", "-d"))
            if not found:
                raise PrintError("принтер по умолчанию не задан: выберите его в настройках системы или в JARVIS_PRINTER")
            name = found[1]
        try:
            state = await self._run("lpstat", "-p", name)
        except PrintError as e:
            raise PrintError(f"принтер «{name}» не найден") from e
        off = "disabled" in state or "Unplugged" in state
        return name, "принтер сейчас выключен или не подключён: задание встанет в очередь" if off else ""

    async def _pdf(self, path: Path, tmp: str) -> Path:
        """Документ или таблица → PDF. Свой профиль LibreOffice: открытое окно программы конвертации не мешает."""
        await self._run("soffice", "--headless", f"-env:UserInstallation=file://{tmp}/profile", "--convert-to", "pdf",
                        "--outdir", tmp, str(path), timeout=CONVERT_TIMEOUT)
        out = Path(tmp) / (path.stem + ".pdf")
        if not out.is_file():
            raise PrintError(f"LibreOffice не смог превратить «{path.name}» в PDF")
        return out

    async def _pages(self, path: Path) -> int | None:
        if path.suffix.lower() in IMAGES:
            return 1
        if path.suffix.lower() != ".pdf":
            return None
        try:
            found = re.search(r"^Pages:\s*(\d+)", await self._run("pdfinfo", str(path)), re.M)
        except PrintError:
            return None  # без pdfinfo число страниц просто не покажем
        return int(found[1]) if found else None

    async def print(self, args: dict, channel: str = "") -> str:
        path = Path(str(args.get("path") or "")).expanduser()
        if not path.is_absolute() or not files.inside(path, self.roots):
            raise PrintError("путь вне разрешённых папок")
        if not path.is_file():
            raise PrintError("файл не найден")
        ext = path.suffix.lower()
        if ext not in DIRECT | OFFICE:
            raise PrintError(f"файлы «{ext or 'без расширения'}» печатать не умею")
        if path.stat().st_size > MAX_BYTES:
            raise PrintError("файл слишком большой для печати")
        try:
            copies = int(args.get("copies") or 1)
        except (TypeError, ValueError) as e:
            raise PrintError("copies должно быть целым числом") from e
        if not 1 <= copies <= MAX_COPIES:
            raise PrintError(f"copies — от 1 до {MAX_COPIES}")
        pages = re.sub(r"\s+", "", str(args.get("pages") or ""))
        if pages and not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", pages):
            raise PrintError("pages нужно как «1-3,5»")
        if self.confirm is None:
            raise PrintError("печать требует подтверждения, а оно не настроено")
        printer, warning = await self.target()
        with tempfile.TemporaryDirectory(prefix="jarvis-print-") as tmp:
            ready = await self._pdf(path.resolve(), tmp) if ext in OFFICE else path.resolve()
            count = await self._pages(ready)
            lines = [f"Принтер: {printer}" + (f" ({warning})" if warning else ""),
                     "Страниц: " + (pages if pages else str(count) if count else "неизвестно"),
                     *([f"Копий: {copies}"] if copies > 1 else []), f"Файл: {path}"]
            if not await self.confirm.ask(f"Напечатать «{path.name}»?", "\n".join(lines), channel=channel):
                raise PrintError("пользователь не подтвердил печать")
            await self._run("lp", "-d", printer, "-n", str(copies), *(["-P", pages] if pages else []),
                            "-t", path.name, "--", str(ready))
        size = f", страниц: {count}" if count and not pages else f", страницы {pages}" if pages else ""
        return (f"Отправил на печать: {path.name}{size}, принтер {printer}."
                + (" Но " + warning + "." if warning else ""))
