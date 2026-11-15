"""Реестр проектов: одна заметка с YAML-frontmatter в 10_Projects = один проект."""
from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter

TEMPLATE = Path(__file__).parent / "vault_template"
PROJECTS = "10_Projects"


def ensure_vault(vault: Path) -> None:
    """Создаёт структуру vault и недостающие служебные файлы. Существующее не трогает."""
    for src in TEMPLATE.rglob("*"):
        dst = vault / src.relative_to(TEMPLATE)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists() and src.name != ".gitkeep":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)


def load_projects(vault: Path) -> list[dict]:
    """Читает frontmatter всех проектов. Файлы с именем на «_» — шаблоны, пропускаются."""
    out = []
    for path in sorted((vault / PROJECTS).glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            post = frontmatter.load(path)
        except Exception as e:  # битый YAML не должен ронять ядро
            out.append({"id": path.stem, "status": "error", "error": f"frontmatter: {e}", "_file": path.name})
            continue
        meta = {k: (v if isinstance(v, (str, int, float, bool, list, dict)) else str(v)) for k, v in post.metadata.items()}
        meta.setdefault("id", path.stem)
        meta.setdefault("status", "active")
        meta["_file"] = f"{PROJECTS}/{path.name}"
        meta["_open_tasks"] = sum(1 for line in post.content.splitlines() if line.lstrip().startswith("- [ ]"))
        out.append(meta)
    return out


def summary(projects: list[dict]) -> str:
    """Компактная сводка для системного промпта (стабильна, пока не меняются заметки)."""
    if not projects:
        return "Проектов в реестре пока нет."
    lines = []
    for p in projects:
        stack = ", ".join(p.get("stack") or []) or "—"
        flags = " [SENSITIVE]" if p.get("sensitive") else ""
        lines.append(
            f"- {p['id']}{flags}: status={p['status']}; stack={stack}; "
            f"открытых задач={p.get('_open_tasks', 0)}; last_touched={p.get('last_touched', '—')}; файл={p['_file']}"
        )
    return "\n".join(lines)


def details(projects: list[dict], flt: str = "") -> str:
    flt = flt.strip().lower()
    hit = [p for p in projects if not flt or flt in str(p["id"]).lower() or flt == str(p["status"]).lower()]
    if not hit:
        return f"Ничего не найдено по фильтру «{flt}»."
    return "\n\n".join("\n".join(f"{k}: {v}" for k, v in p.items()) for p in hit)
