import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from jarvis.agent import Brain, integrations_hint
from jarvis.config import Settings
from jarvis.notion import Notion, NotionError, block_text, page_id, to_blocks
from jarvis.router import VoiceRouter
from jarvis.store import Store

PID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
DASHED = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
OFF = dict(hass_url="", hass_token="", hass_mcp_path="/api/mcp/assist", notion_token="", notion_parent="", github_token="",
           open_roots=(), pc_control=False)  # тесты не должны зависеть от локального .env


def settings(tmp_path, **kw) -> Settings:
    return replace(Settings(), vault=(tmp_path / "vault").resolve(), db=tmp_path / "j.db", **{**OFF, **kw})


def brain(tmp_path, **kw) -> Brain:
    s = settings(tmp_path, **kw)
    return Brain(s, Store(s.db))


# --- Home Assistant -------------------------------------------------------

def test_home_mcp_attached_only_with_url_and_token(tmp_path):
    o = brain(tmp_path, hass_url="http://ha:8123", hass_token="t")._options("u", "api", None)
    assert o.mcp_servers["home"] == {"type": "http", "url": "http://ha:8123/api/mcp/assist",
                                     "headers": {"Authorization": "Bearer t"}}
    assert "mcp__home__*" in o.allowed_tools and "mcp__home__" in o.system_prompt

    o = brain(tmp_path, hass_url="http://ha:8123")._options("u", "api", None)
    assert "home" not in o.mcp_servers and "mcp__home__*" not in o.allowed_tools
    assert "mcp__home__" not in o.system_prompt and "Notion" not in o.system_prompt


def test_router_tells_haiku_to_delegate_home_and_notion(tmp_path):
    def system(**kw):
        s = settings(tmp_path, **kw)
        return VoiceRouter(s, None, None, None).system()

    plain = system()
    assert "умным домом" not in plain and "Notion" not in plain
    both = system(hass_url="http://ha", hass_token="t", notion_token="n")
    assert "управлению умным домом" in both and "страницам Notion" in both
    assert "включить, выключить" in both


# --- Notion ---------------------------------------------------------------

def test_page_id_from_id_and_url():
    assert page_id(PID) == DASHED and page_id(DASHED) == DASHED
    assert page_id(f"https://www.notion.so/team/Cafe-Plan-{PID}?pvs=4#abc") == DASHED
    with pytest.raises(NotionError):
        page_id("https://www.notion.so/team/just-a-title")


def test_blocks_roundtrip():
    blocks = to_blocks("# Заголовок\n\nтекст\n- пункт\n- [ ] дело\n- [x] готово\n" + "я" * 2500)
    assert [b["type"] for b in blocks] == ["heading_1", "paragraph", "bulleted_list_item", "to_do", "to_do", "paragraph"]
    assert blocks[3]["to_do"]["checked"] is False and blocks[4]["to_do"]["checked"] is True
    assert [len(x["text"]["content"]) for x in blocks[5]["paragraph"]["rich_text"]] == [2000, 500]
    todo = {"type": "to_do", "to_do": {"checked": True, "rich_text": [{"plain_text": "готово"}]}}
    assert block_text(todo) == "- [x] готово"
    assert block_text({"type": "divider", "divider": {}}) == ""


def notion(handler, parent="") -> Notion:
    return Notion("secret", parent, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_search_and_auth_headers():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(auth=req.headers["authorization"], ver=req.headers["notion-version"],
                    url=str(req.url), body=json.loads(req.content))
        return httpx.Response(200, json={"results": [
            {"object": "page", "id": DASHED, "url": "https://notion.so/x",
             "properties": {"Name": {"type": "title", "title": [{"plain_text": "Список покупок"}]}}},
            {"object": "database", "id": "db1", "title": [{"plain_text": "Задачи"}]}]})

    out = asyncio.run(notion(handler).search("покупки"))
    assert seen["auth"] == "Bearer secret" and seen["ver"] == "2022-06-28"
    assert seen["url"].endswith("/v1/search") and seen["body"]["query"] == "покупки"
    assert f"Список покупок — page — id {DASHED}" in out and "Задачи — database" in out


def test_read_follows_pagination_and_nested_blocks():
    def para(text, **kw):
        return {"id": text, "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}, **kw}

    def handler(req: httpx.Request):
        path, cursor = req.url.path, req.url.params.get("start_cursor")
        if path == f"/v1/pages/{DASHED}":
            return httpx.Response(200, json={"url": "https://notion.so/p", "properties": {
                "title": {"type": "title", "title": [{"plain_text": "План"}]}}})
        if path == f"/v1/blocks/{DASHED}/children" and not cursor:
            return httpx.Response(200, json={"results": [para("один", has_children=True)],
                                             "has_more": True, "next_cursor": "c2"})
        if path == f"/v1/blocks/{DASHED}/children":
            return httpx.Response(200, json={"results": [para("три")], "has_more": False})
        if path == "/v1/blocks/один/children":
            return httpx.Response(200, json={"results": [para("два")], "has_more": False})
        return httpx.Response(404, json={"message": "нет такого"})

    out = asyncio.run(notion(handler).read(PID))
    assert out.startswith("# План\nhttps://notion.so/p\n\n")
    assert out.endswith("один\n  два\nтри")


def test_append_and_create():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"id": "new-id", "url": "https://notion.so/new"})

    n = notion(handler, parent=PID)
    assert asyncio.run(n.append(PID, "- [ ] молоко\nхлеб")) == "Добавлено блоков: 2"
    method, path, body = calls[0]
    assert (method, path) == ("PATCH", f"/v1/blocks/{DASHED}/children")
    assert [b["type"] for b in body["children"]] == ["to_do", "paragraph"]

    out = asyncio.run(n.create("Идеи", "раз\n" * 150))
    assert "https://notion.so/new" in out
    method, path, body = calls[1]
    assert (method, path) == ("POST", "/v1/pages") and body["parent"] == {"page_id": DASHED}
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "Идеи"
    assert len(body["children"]) == 100 and len(calls[2][2]["children"]) == 50  # остаток — вторым запросом
    assert calls[2][1] == "/v1/blocks/new-id/children"


def test_errors_are_notion_errors():
    n = notion(lambda req: httpx.Response(404, json={"message": "Could not find page"}))
    with pytest.raises(NotionError, match="404: Could not find page"):
        asyncio.run(n.read(PID))
    with pytest.raises(NotionError, match="NOTION_PARENT_PAGE"):
        asyncio.run(n.create("x"))
    with pytest.raises(NotionError, match="пуст"):
        asyncio.run(n.append(PID, "  \n"))


def test_notion_tools_follow_settings(tmp_path):
    assert integrations_hint(settings(tmp_path)) == ""
    assert "notion_create" not in integrations_hint(settings(tmp_path, notion_token="n"))
    assert "notion_create" in integrations_hint(settings(tmp_path, notion_token="n", notion_parent=PID))
    assert brain(tmp_path).notion is None
    b = brain(tmp_path, notion_token="n", notion_parent=PID)
    assert b.notion.enabled and b._tools_server() is not None
