"""Поиск и открытие файлов на компьютере — только внутри разрешённых папок (JARVIS_OPEN_ROOTS)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$RECYCLE.BIN", "AppData"}


def inside(path: Path, roots: tuple[Path, ...]) -> bool:
    p = path.resolve()
    return any(p.is_relative_to(r) for r in roots)


def find(query: str, roots: tuple[Path, ...], limit: int = 15, budget_s: float = 3.0) -> list[Path]:
    """Файлы, в имени которых есть все слова запроса (без учёта регистра). Свежие — первыми."""
    words = [w for w in query.lower().replace("_", " ").split() if w]
    if not words:
        return []
    deadline = time.monotonic() + budget_s
    hits: list[Path] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for name in filenames:
                low = name.lower().replace("_", " ")
                if all(w in low for w in words):
                    hits.append(Path(dirpath) / name)
            if time.monotonic() > deadline or len(hits) > 500:
                break

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(hits, key=mtime, reverse=True)[:limit]


def open_path(path: Path) -> None:
    """Открывает файл программой по умолчанию."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
