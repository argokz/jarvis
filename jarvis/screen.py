"""Снимок экрана для агента: «что у меня на экране?», «прочитай, что тут написано».

Снимок делает рабочий стол через портал (см. screen_portal.py) и, если считает нужным, спрашивает
разрешение у человека за компьютером. Файл читается и сразу удаляется: на экране бывают пароли и
переписка, хранить его незачем. Картинка уменьшается через ffmpeg — так она дешевле и быстрее для
модели; без ffmpeg уходит как есть.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
import tempfile
from pathlib import Path

HELPER = Path(__file__).with_name("screen_portal.py")
SYSTEM_PYTHON = "/usr/bin/python3"  # в нём есть PyGObject; в окружении ядра его нет
WAIT = 30                           # столько ждём ответа на вопрос о разрешении
MAX_SIDE = 1568                     # больше модель всё равно уменьшит сама
MAX_BYTES = 4 * 1024 * 1024

TOOL = {
    "name": "screen_look",
    "description": "Сделать снимок экрана компьютера и посмотреть на него. Только когда пользователь прямо просит "
                   "посмотреть, прочитать или объяснить то, что у него на экране.",
    "input_schema": {"type": "object", "properties": {}},
}


class ScreenError(Exception):
    pass


async def _run(*args: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise ScreenError(f"не найдена программа {args[0]}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise ScreenError(f"{Path(args[0]).name} не ответил вовремя") from e
    return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


class Screen:
    def __init__(self, run=_run, python: str = SYSTEM_PYTHON, ffmpeg=lambda: shutil.which("ffmpeg")):
        self._run, self._python, self._ffmpeg = run, python, ffmpeg

    async def capture(self) -> tuple[bytes, str]:
        """Снимок экрана: (байты, тип). Файл снимка на диске не остаётся."""
        code, out, err = await self._run(self._python, "-I", str(HELPER), str(WAIT), timeout=WAIT + 10)
        if code == 2:
            raise ScreenError("снимок экрана не разрешён: на компьютере ответили «нет»")
        if code == 3:
            raise ScreenError("на экране компьютера появился вопрос о разрешении на снимок, но на него никто не ответил")
        if code != 0 or not out:
            raise ScreenError(err[:200] or "не удалось сделать снимок экрана")
        shot = Path(out)
        try:
            return await self._shrink(shot)
        finally:
            with contextlib.suppress(OSError):
                shot.unlink()

    async def _shrink(self, shot: Path) -> tuple[bytes, str]:
        binary = self._ffmpeg()
        if binary:
            with tempfile.TemporaryDirectory(prefix="jarvis-screen-") as tmp:
                small = Path(tmp) / "screen.jpg"
                scale = f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease"
                code, _, _ = await self._run(binary, "-v", "error", "-y", "-i", str(shot), "-vf", scale,
                                             "-q:v", "3", str(small), timeout=20)
                if code == 0 and small.is_file():
                    return small.read_bytes(), "image/jpeg"
        data = shot.read_bytes()
        if len(data) > MAX_BYTES:
            raise ScreenError("снимок слишком большой, а ffmpeg для уменьшения не найден")
        return data, "image/png"

    async def look(self) -> dict:
        """Результат инструмента агента: картинка в формате MCP."""
        data, mime = await self.capture()
        return {"content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}]}
