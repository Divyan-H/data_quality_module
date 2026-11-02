"""Run workspaces (src/storage/run_store.py): state, events, deletion, concurrency."""
import json
import threading

import pytest

from src.storage.run_store import RunNotFound, RunStore, append_event


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs")


def test_create_load_and_list(store):
    a = store.create("first", {"phases": {}, "inputs": []})
    b = store.create("second", {"phases": {}, "inputs": []})
    assert store.load(a)["name"] == "first"
    assert {s["id"] for s in store.list()} == {a, b}
    assert (store.path(a) / "inputs").is_dir() and (store.path(a) / "jobs").is_dir()


def test_list_is_newest_first_and_skips_foreign_folders(store, tmp_path):
    a = store.create("old", {})
    (store.root / "not-a-run").mkdir()
    (store.root / "broken").mkdir()
    (store.root / "broken" / "state.json").write_text("{half", encoding="utf8")
    b = store.create("new", {})
    store.update(a, lambda s: s.update(created_at="2000-01-01T00:00:00+00:00"))
    assert [s["id"] for s in store.list()] == [b, a]


def test_update_is_saved_and_stamped(store):
    rid = store.create("r", {"n": 0})
    before = store.load(rid)["updated_at"]
    store.update(rid, lambda s: s.update(n=5))
    assert store.load(rid)["n"] == 5
    assert store.load(rid)["updated_at"] >= before


def test_an_error_in_the_block_saves_nothing(store):
    rid = store.create("r", {"n": 0})
    with pytest.raises(RuntimeError):
        with store.edit(rid) as s:
            s["n"] = 99
            raise RuntimeError("boom")
    assert store.load(rid)["n"] == 0


def test_concurrent_edits_do_not_lose_updates(store):
    rid = store.create("r", {"n": 0})

    def bump():
        for _ in range(25):
            store.update(rid, lambda s: s.update(n=s["n"] + 1))

    threads = [threading.Thread(target=bump) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert store.load(rid)["n"] == 150


def test_unknown_and_malformed_ids_are_both_just_unknown_runs(store):
    for bad in ("0123456789abcdef", "../../etc", "not-an-id", ""):
        with pytest.raises(RunNotFound):
            store.path(bad)


def test_delete_removes_the_folder(store):
    rid = store.create("r", {})
    store.delete(rid)
    assert not (store.root / rid).exists()
    with pytest.raises(RunNotFound):
        store.load(rid)


def test_events_are_read_with_a_cursor(store):
    rid = store.create("r", {})
    assert store.events(rid, 0) == ([], 0)
    for i in range(5):
        store.append_event(rid, {"type": "log", "message": f"m{i}"})
    first, cursor = store.events(rid, 0, limit=3)
    assert [e["message"] for e in first] == ["m0", "m1", "m2"] and cursor == 3
    rest, cursor = store.events(rid, cursor)
    assert [e["message"] for e in rest] == ["m3", "m4"] and cursor == 5
    assert store.events(rid, cursor) == ([], 5)


def test_an_unreadable_event_line_does_not_break_reading(store):
    rid = store.create("r", {})
    store.append_event(rid, {"type": "log", "message": "ok"})
    with (store.path(rid) / "events.jsonl").open("a", encoding="utf8") as fh:
        fh.write("not json\n")
    events, cursor = store.events(rid, 0)
    assert len(events) == 2 and cursor == 2 and events[1]["level"] == "warn"


def test_two_writers_append_whole_lines(store):
    rid = store.create("r", {})
    folder = store.path(rid)

    def write(tag):
        for i in range(60):
            append_event(folder, {"type": "log", "message": f"{tag}-{i}"})

    threads = [threading.Thread(target=write, args=(t,)) for t in "abcd"]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = (folder / "events.jsonl").read_text(encoding="utf8").splitlines()
    assert len(lines) == 240 and all(json.loads(x)["type"] == "log" for x in lines)
