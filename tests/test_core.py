from pathlib import Path

import pytest

from jarvis import registry
from jarvis.agent import check_path, slugify
from jarvis.store import Store
from jarvis.telegram_bot import chunks


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = (tmp_path / "vault").resolve()
    registry.ensure_vault(v)
    (v / "10_Projects" / "clinic-portal.md").write_text(
        "---\nid: clinic-portal\nstatus: active\nstack: [vue3, fastapi]\nsensitive: true\n"
        "last_touched: 2026-11-14\n---\n## Открытые задачи\n- [ ] раз\n- [x] два\n- [ ] три\n", encoding="utf-8")
    (v / "10_Projects" / "broken.md").write_text("---\nid: [oops\n---\n", encoding="utf-8")
    return v


def test_vault_structure_and_idempotence(vault):
    assert (vault / "90_Agent" / "policies.md").exists()
    assert (vault / "90_Agent" / "memories").is_dir()
    (vault / "90_Agent" / "policies.md").write_text("моё", encoding="utf-8")
    registry.ensure_vault(vault)
    assert (vault / "90_Agent" / "policies.md").read_text(encoding="utf-8") == "моё"


def test_registry(vault):
    ps = {p["id"]: p for p in registry.load_projects(vault)}
    assert "my-project" not in ps  # _template.md пропущен
    assert ps["clinic-portal"]["_open_tasks"] == 2
    assert ps["clinic-portal"]["last_touched"] == "2026-11-14"
    assert ps["broken"]["status"] == "error"
    s = registry.summary(list(ps.values()))
    assert "clinic-portal [SENSITIVE]" in s
    assert "clinic-portal" in registry.details(list(ps.values()), "clinic")
    assert "Ничего" in registry.details(list(ps.values()), "nope")


@pytest.mark.parametrize("tool,inp,ok", [
    ("Read", {"file_path": "10_Projects/a.md"}, True),
    ("Read", {"file_path": "../secret.env"}, False),
    ("Read", {"file_path": "/etc/passwd"}, False),
    ("Write", {"file_path": "00_Inbox/x.md"}, True),
    ("Write", {"file_path": "90_Agent/policies.md"}, False),
    ("Edit", {"file_path": ".obsidian/app.json"}, False),
    ("Write", {}, False),
    ("Grep", {"pattern": "x"}, True),
    ("Grep", {"pattern": "x", "path": "/"}, False),
    ("Glob", {"pattern": "**/*.md", "path": "10_Projects"}, True),
])
def test_check_path(vault, tool, inp, ok):
    assert (check_path(vault, tool, inp) is None) is ok


def test_symlink_escape(vault, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    (vault / "link.md").symlink_to(tmp_path / "outside.txt")
    assert check_path(vault, "Read", {"file_path": "link.md"}) is not None


def test_store(tmp_path):
    s = Store(tmp_path / "d" / "j.db")
    assert s.get_session("owner") is None
    s.set_session("owner", "a"); s.set_session("owner", "b")
    assert s.get_session("owner") == "b"
    s.log("owner", "telegram", "tool", "Read", {"file_path": "x"})
    assert s.recent()[0]["name"] == "Read"
    s.drop_session("owner")
    assert s.get_session("owner") is None


def test_helpers():
    assert slugify("Идея: дашборд проектов!") == "идея-дашборд-проектов"
    assert slugify("!!!") == "note"
    parts = list(chunks("a" * 3000 + "\n" + "b" * 3000))
    assert len(parts) == 2 and all(len(p) <= 4000 for p in parts)
