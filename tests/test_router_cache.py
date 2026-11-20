"""Кэш запросов быстрой модели и учёт её расхода: начало запроса не должно меняться от хода к ходу."""
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from jarvis import pricing
from jarvis.agent import Reply
from jarvis.config import Settings
from jarvis.notify import Notifier
from jarvis.router import HISTORY_ROWS, VoiceRouter, stamp
from jarvis.store import Store


class Stream:
    def __init__(self, text, usage):
        self.text, self.usage = text, usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            yield NS(type="text", text=self.text)
        return gen()

    async def get_final_message(self):
        return NS(stop_reason="end_turn", content=[], usage=self.usage)


class Client:
    """Отвечает «ответ N» и сообщает счётчики токенов, как настоящий API."""
    def __init__(self, usage=None):
        self.calls, self.usage = [], usage
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return Stream(f"ответ {len(self.calls)}", self.usage)


class Brain:
    async def ask(self, user, text, channel="api"):
        return Reply("Ответ агента.")


@pytest.fixture
def made(tmp_path):
    def make(usage=None, model="claude-haiku-5-5"):
        s = replace(Settings(), vault=(tmp_path / "vault").resolve(), hass_url="", hass_token="", notion_token="",
                    open_roots=(), pc_control=False, router_model=model)
        client, store = Client(usage), Store(tmp_path / "j.db")
        return VoiceRouter(s, Brain(), store, Notifier(), client=client), client, store
    return make


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_stable_part_of_the_request_has_its_own_cache_point(made):
    r, client, _ = made()
    await say(r, "привет")
    kw = client.calls[0]
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}   # инструменты и подсказка — отдельно от истории
    assert kw["cache_control"] == {"type": "ephemeral"}                # вторая точка идёт по концу истории
    assert time.strftime("%Y-%m-%d") not in kw["system"][0]["text"]     # время — только в реплике, не в подсказке


async def test_next_turn_starts_with_exactly_what_the_previous_one_sent(made):
    r, client, _ = made()
    for text in ("раз", "два", "три"):
        await say(r, text)
    first, second, third = (c["messages"] for c in client.calls)
    assert second[:len(first)] == first and third[:len(second)] == second   # иначе кэш промахивается на каждом ходе
    assert second[len(first)] == {"role": "assistant", "content": "ответ 1"}
    assert all(c["system"] == client.calls[0]["system"] and c["tools"] == client.calls[0]["tools"] for c in client.calls)


async def test_user_lines_keep_the_time_they_were_said_at(made):
    r, client, store = made()
    store.log("owner", "telegram", "user", payload="вчерашний вопрос", ts=1_795_000_000.0)
    store.log("owner", "telegram", "assistant", payload={"text": "вчерашний ответ"}, ts=1_795_000_001.0)
    await say(r, "сегодня")
    old, _, new = client.calls[0]["messages"]
    assert old["content"] == stamp(1_795_000_000.0, "вчерашний вопрос")   # время записи, а не время запроса
    assert new["content"].endswith("] сегодня") and new["content"][:12] != old["content"][:12]


async def test_history_window_grows_and_only_then_moves(made):
    r, client, store = made()
    for i in range(HISTORY_ROWS):                      # журнал уже длиннее окна
        store.log("owner", "voice", "user" if i % 2 == 0 else "assistant", payload=f"старое {i}")
    starts = []
    for i in range(HISTORY_ROWS + 2):
        await say(r, f"вопрос {i}")
        starts.append(client.calls[-1]["messages"][0]["content"])
    # пока окно не выросло вдвое, история начинается с одной и той же записи — её начало читается из кэша
    assert len(set(starts[:HISTORY_ROWS // 2])) == 1
    assert len(set(starts)) == 2                        # один сдвиг за всё время, а не на каждом ходе
    assert all(len(c["messages"]) <= HISTORY_ROWS * 2 + 1 for c in client.calls)


async def test_turn_cost_and_tokens_go_to_the_journal(made):
    usage = NS(input_tokens=10, output_tokens=20, cache_read_input_tokens=9000, cache_creation_input_tokens=400)
    r, _, store = made(usage)
    await say(r, "привет")
    row = store.recent(1)[0]
    assert row["kind"] == "assistant" and '"cost_usd": 0.00015' in row["payload"]
    assert '"cache_read_input_tokens": 9000' in row["payload"] and row["payload"].index("cost_usd") < row["payload"].index("text")
    assert store.spent(0) == (0.0002, 1)                # расход быстрой модели виден в сумме за день


async def test_unknown_model_logs_tokens_without_inventing_a_price(made):
    usage = NS(input_tokens=10, output_tokens=20, cache_read_input_tokens=None, cache_creation_input_tokens=None)
    r, _, store = made(usage, model="some-new-model")
    await say(r, "привет")
    payload = store.recent(1)[0]["payload"]
    assert '"cost_usd": null' in payload and '"input_tokens": 10' in payload and store.spent(0) == (0.0, 0)


def test_price_counts_cache_reads_cheap_and_cache_writes_dear():
    million = 1_000_000
    assert pricing.cost("claude-haiku-5-5", {"input_tokens": million}) == 0.10
    assert pricing.cost("claude-haiku-5-5", {"cache_read_input_tokens": million}) == 0.01
    assert pricing.cost("claude-haiku-5-5", {"cache_creation_input_tokens": million}) == 0.125
    assert pricing.cost("claude-sonnet-5-5", {"output_tokens": million}) == 10.0
    assert pricing.cost("claude-haiku-5-5", {}) is None and pricing.cost("nope", {"input_tokens": 1}) is None


def test_journal_is_merged_into_the_database_file_and_indexed(tmp_path):
    store = Store(tmp_path / "j.db")
    assert store.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 100
    assert "journal_ts" in [r[1] for r in store.db.execute("PRAGMA index_list(journal)")]
    for i in range(400):
        store.log("owner", "app", "user", payload="я" * 2000)
    assert (tmp_path / "j.db").stat().st_size > 500_000   # данные в самом файле базы, а не только в -wal
