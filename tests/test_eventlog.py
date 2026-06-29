"""Event log: append, seq, tail, torn-line recovery."""

from openadventure.store.eventlog import EventLog


def test_append_assigns_increasing_seq(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = log.append("user_message", {"text": "hi"})
    b = log.append("gm_message", {"text": "hello"})
    assert (a.seq, b.seq) == (1, 2)
    assert log.last_seq == 2


def test_roundtrip(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    log.append("roll", {"expression": "d20", "total": 14})
    log.append("user_message", {"text": "I attack"})

    reopened = EventLog(path)
    entries = reopened.read_all()
    assert [e.type for e in entries] == ["roll", "user_message"]
    assert entries[0].data["total"] == 14
    assert reopened.last_seq == 2
    assert reopened.append("gm_message").seq == 3


def test_tail_and_read_since(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(10):
        log.append("note", {"i": i})
    assert [e.data["i"] for e in log.tail(3)] == [7, 8, 9]
    assert [e.data["i"] for e in log.read_since(8)] == [8, 9]


def test_torn_final_line_is_skipped(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    log.append("user_message", {"text": "before crash"})
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "ts": "2026-01-01T00:00:00+00:00", "type": "dm_mess')  # torn

    reopened = EventLog(path)
    entries = reopened.read_all()
    assert len(entries) == 1
    assert entries[0].data["text"] == "before crash"
    # next append continues after the surviving entry
    assert reopened.append("session_end").seq == 2


def test_empty_log(tmp_path):
    log = EventLog(tmp_path / "missing.jsonl")
    assert log.read_all() == []
    assert log.last_seq == 0


def test_truncate_removes_archives_and_resets_seq(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(6):
        log.append("note", {"i": i})
    archive = tmp_path / "archive" / "undone.jsonl"

    removed = log.truncate_to(3, archive=archive)
    assert [e.data["i"] for e in removed] == [3, 4, 5]
    assert [e.data["i"] for e in log.read_all()] == [0, 1, 2]
    assert log.last_seq == 3
    assert log.append("note", {"i": "next"}).seq == 4

    archived = EventLog(archive).read_all()
    assert [e.data["i"] for e in archived] == [3, 4, 5]

    # survives reopen
    assert EventLog(tmp_path / "log.jsonl").last_seq == 4


def test_truncate_noop_past_end(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("note")
    assert log.truncate_to(5) == []
    assert log.last_seq == 1


def test_truncate_to_zero_empties(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("note")
    log.append("note")
    removed = log.truncate_to(0)
    assert len(removed) == 2
    assert log.read_all() == []
    assert log.append("note").seq == 1


def test_refresh_rescans(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    log.append("note")
    path.unlink()  # external replacement (e.g. restart archived the log)
    log.refresh()
    assert log.last_seq == 0
    assert log.append("note").seq == 1
