"""Notion через REST API: поиск, чтение, новая страница и «дописать в конец».

Нужен токен внутренней интеграции (notion.so/profile/integrations). Интеграция видит только те
страницы, к которым её подключили в Notion (··· → Connections). Удалять и перезаписывать не умеет
намеренно — это добавляющий доступ, как 00_Inbox в vault.
"""
from __future__ import annotations

import re

import httpx

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
MAX_TEXT = 2000      # лимит Notion на один rich_text-объект
MAX_BLOCKS = 100     # лимит блоков на один запрос
READ_REQUESTS = 15   # потолок запросов на чтение одной страницы (пагинация + вложенные блоки)
READ_CHARS = 12000

PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
          "numbered_list_item": "1. ", "quote": "> "}
HEADINGS = {"# ": "heading_1", "## ": "heading_2", "### ": "heading_3"}


class NotionError(Exception):
    pass


def page_id(ref: str) -> str:
    """Принимает id или ссылку на страницу, возвращает id в виде 8-4-4-4-12."""
    tail = ref.strip().split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].replace("-", "")[-32:]
    if not re.fullmatch(r"[0-9a-fA-F]{32}", tail):
        raise NotionError(f"не похоже на id или ссылку страницы Notion: {ref[:80]}")
    t = tail.lower()
    return f"{t[:8]}-{t[8:12]}-{t[12:16]}-{t[16:20]}-{t[20:]}"


def _plain(rich: list | None) -> str:
    return "".join(x.get("plain_text", "") for x in rich or [])


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[i:i + MAX_TEXT]}} for i in range(0, len(text), MAX_TEXT)]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text), **extra}}


def to_blocks(text: str) -> list[dict]:
    """Простой текст → блоки: «# » заголовки, «- » список, «- [ ] » задачи, остальное — абзацы."""
    blocks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        todo = re.match(r"[-*] \[( |x|X)\] (.*)", line)
        head = next((p for p in HEADINGS if line.startswith(p)), None)
        if todo:
            blocks.append(_block("to_do", todo[2], checked=todo[1] != " "))
        elif head:
            blocks.append(_block(HEADINGS[head], line[len(head):]))
        elif line[:2] in ("- ", "* "):
            blocks.append(_block("bulleted_list_item", line[2:]))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


def block_text(b: dict) -> str:
    kind = b.get("type", "")
    data = b.get(kind) or {}
    if kind == "to_do":
        return f"- [{'x' if data.get('checked') else ' '}] {_plain(data.get('rich_text'))}"
    if kind in ("child_page", "child_database"):
        return f"[вложенная страница: {data.get('title', '')}, id {b.get('id', '')}]"
    text = _plain(data.get("rich_text"))
    return PREFIX.get(kind, "") + text if text else ""


def title_of(obj: dict) -> str:
    if obj.get("object") == "database":
        return _plain(obj.get("title"))
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title"))
    return ""


class Notion:
    def __init__(self, token: str, parent: str = "", client: httpx.AsyncClient | None = None):
        self.token, self.parent = token, parent
        self._client = client or httpx.AsyncClient(timeout=30)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": VERSION}
        try:
            r = await self._client.request(method, API + path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise NotionError(f"Notion недоступен: {e}") from e
        if r.status_code != 200:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = r.text[:200]
            raise NotionError(f"Notion вернул {r.status_code}: {detail}")
        return r.json()

    async def search(self, query: str, limit: int = 10) -> str:
        data = await self._call("POST", "/search", json={"query": query, "page_size": limit})
        lines = [f"{title_of(o) or '(без названия)'} — {o.get('object')} — id {o.get('id')} — {o.get('url', '')}"
                 for o in data.get("results", [])]
        return "\n".join(lines) or "Ничего не найдено. Интеграция видит только подключённые к ней страницы."

    async def _children(self, block_id: str, depth: int, budget: list[int]) -> list[str]:
        lines: list[str] = []
        cursor = None
        while budget[0] > 0:
            budget[0] -= 1
            params = {"page_size": MAX_BLOCKS, **({"start_cursor": cursor} if cursor else {})}
            data = await self._call("GET", f"/blocks/{block_id}/children", params=params)
            for b in data.get("results", []):
                line = block_text(b)
                if line:
                    lines.append("  " * depth + line)
                nested = b.get("has_children") and b.get("type") not in ("child_page", "child_database")
                if nested and depth < 2:
                    lines += await self._children(b["id"], depth + 1, budget)
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                break
        return lines

    async def read(self, ref: str) -> str:
        pid = page_id(ref)
        page = await self._call("GET", f"/pages/{pid}")
        budget = [READ_REQUESTS]
        body = "\n".join(await self._children(pid, 0, budget))
        cut = "\n[…страница длиннее, показано начало]" if budget[0] == 0 or len(body) > READ_CHARS else ""
        return f"# {title_of(page) or '(без названия)'}\n{page.get('url', '')}\n\n{body[:READ_CHARS]}{cut}"

    async def append(self, ref: str, text: str) -> str:
        pid = page_id(ref)
        blocks = to_blocks(text)
        if not blocks:
            raise NotionError("нечего добавлять: текст пуст")
        for i in range(0, len(blocks), MAX_BLOCKS):
            await self._call("PATCH", f"/blocks/{pid}/children", json={"children": blocks[i:i + MAX_BLOCKS]})
        return f"Добавлено блоков: {len(blocks)}"

    async def create(self, title: str, text: str = "", parent: str = "") -> str:
        if not (parent or self.parent):
            raise NotionError("не задана родительская страница (NOTION_PARENT_PAGE)")
        blocks = to_blocks(text)
        page = await self._call("POST", "/pages", json={
            "parent": {"page_id": page_id(parent or self.parent)},
            "properties": {"title": {"title": _rich(title)}},
            "children": blocks[:MAX_BLOCKS],
        })
        for i in range(MAX_BLOCKS, len(blocks), MAX_BLOCKS):
            await self._call("PATCH", f"/blocks/{page['id']}/children", json={"children": blocks[i:i + MAX_BLOCKS]})
        return f"Создана страница «{title}»: {page.get('url', '')} (id {page.get('id')})"
