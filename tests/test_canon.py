"""Canon store: op application (idempotent, drift-free), open/archive split, and
the capped open render vs the full search render."""

from openadventure.store import canon
from openadventure.store.canon import Canon, CanonEntry


def _add(entry_id, category, text, **kw):
    return {"op": "add", "id": entry_id, "category": category, "text": text, **kw}


# --- load / save ---------------------------------------------------------


def test_load_absent_returns_empty(campaign):
    assert campaign.canon_path.is_file() is False
    c = canon.load(campaign)
    assert c.entries == []
    assert c.through_seq == 0


def test_save_load_round_trip(campaign):
    c = Canon(through_seq=42, entries=[CanonEntry(id="t1", category="threads", text="who?")])
    canon.save(campaign, c)
    loaded = canon.load(campaign)
    assert loaded.through_seq == 42
    assert loaded.find("t1").text == "who?"


def test_load_tolerates_garbage(campaign):
    campaign.canon_path.parent.mkdir(parents=True, exist_ok=True)
    campaign.canon_path.write_text("not json at all", encoding="utf-8")
    assert canon.load(campaign).entries == []


def test_load_preserves_unknown_fields_and_categories(campaign):
    # an older build reading a newer canon must not crash or drop data
    campaign.canon_path.parent.mkdir(parents=True, exist_ok=True)
    campaign.canon_path.write_text(
        '{"version": 2, "through_seq": 1, "future_top": "x",'
        ' "entries": [{"id": "f1", "category": "factions", "text": "the Cult",'
        ' "future_field": 7}]}',
        encoding="utf-8",
    )
    c = canon.load(campaign)
    entry = c.find("f1")
    assert entry.category == "factions"  # unknown category survives
    # unknown category still renders (last) rather than failing
    assert "the Cult" in canon.render_full(c, include_hidden=True)


# --- apply_ops -----------------------------------------------------------


def test_add_creates_entry_with_stamps():
    c, warns = canon.apply_ops(
        canon.empty(), [_add("t1", "threads", "who poisoned the duke?")], at_seq=120
    )
    assert warns == []
    e = c.find("t1")
    assert e.category == "threads"
    assert e.status == "open"
    assert e.created_seq == 120
    assert e.updated_seq == 120
    assert e.is_open


def test_add_is_idempotent_by_id():
    ops = [_add("t1", "threads", "first")]
    c, _ = canon.apply_ops(canon.empty(), ops, at_seq=10)
    # reprocessing the same span (interrupted-pass recovery) must not duplicate
    c2, warns = canon.apply_ops(c, [_add("t1", "threads", "second")], at_seq=11)
    assert warns == []
    assert len(c2.entries) == 1
    assert c2.find("t1").text == "second"
    assert c2.find("t1").updated_seq == 11


def test_stale_op_does_not_clobber_newer_edit():
    # A GM note_canon at seq 50 rewrote the entry while a background compaction
    # was folding the transcript against an older snapshot. The chronicler's op
    # (processing seq 20) must not overwrite the fresher text/status.
    c, _ = canon.apply_ops(canon.empty(), [_add("t1", "threads", "GM's fresh take")], at_seq=50)
    c.find("t1").status = "hostile"
    c, warns = canon.apply_ops(
        c,
        [{"op": "update", "id": "t1", "text": "stale chronicler text", "status": "open"}],
        at_seq=20,
    )
    e = c.find("t1")
    assert e.text == "GM's fresh take"  # scalar overwrite skipped
    assert e.status == "hostile"
    assert e.updated_seq == 50  # not rolled back to 20
    assert any("skipped stale" in w for w in warns)


def test_stale_op_still_merges_facts():
    # facts_add is a union, never lossy, so it applies even when scalars are stale.
    c, _ = canon.apply_ops(
        canon.empty(),
        [_add("vance", "promises", "Captain Vance", facts_add=["owes passage"])],
        at_seq=50,
    )
    c, warns = canon.apply_ops(
        c,
        [{"op": "update", "id": "vance", "text": "stale", "facts_add": ["sailed north"]}],
        at_seq=20,
    )
    e = c.find("vance")
    assert e.text == "Captain Vance"  # scalar skipped
    assert e.facts == ["owes passage", "sailed north"]  # union still merged
    assert any("skipped stale" in w for w in warns)


def test_facts_only_stale_op_emits_no_warning():
    # No scalar fields requested -> nothing was skipped -> no spurious warning.
    c, _ = canon.apply_ops(canon.empty(), [_add("vance", "promises", "Vance")], at_seq=50)
    c, warns = canon.apply_ops(
        c, [{"op": "update", "id": "vance", "facts_add": ["sailed north"]}], at_seq=20
    )
    assert c.find("vance").facts == ["sailed north"]
    assert warns == []


def test_reprocessing_same_span_still_converges():
    # Guard uses strict `>`: re-applying the same span (interrupted-pass recovery)
    # carries the seq the entry already holds, so it is not treated as stale.
    c, _ = canon.apply_ops(canon.empty(), [_add("t1", "threads", "first")], at_seq=30)
    c, warns = canon.apply_ops(c, [{"op": "update", "id": "t1", "text": "second"}], at_seq=30)
    assert c.find("t1").text == "second"
    assert warns == []


def test_update_patches_and_merges_facts():
    c, _ = canon.apply_ops(
        canon.empty(),
        [_add("vance", "promises", "Captain Vance", facts_add=["owes the party passage"])],
        at_seq=5,
    )
    c, _ = canon.apply_ops(
        c,
        [
            {
                "op": "update",
                "id": "vance",
                "status": "hostile",
                "facts_add": ["owes the party passage", "betrayed the party"],
            }
        ],
        at_seq=9,
    )
    e = c.find("vance")
    assert e.status == "hostile"
    assert e.is_open  # "hostile" is not a closing status
    assert e.facts == ["owes the party passage", "betrayed the party"]  # deduped


def test_resolve_closes_and_archives():
    c, _ = canon.apply_ops(canon.empty(), [_add("t3", "threads", "who?")], at_seq=120)
    c, _ = canon.apply_ops(c, [{"op": "resolve", "id": "t3"}], at_seq=151)
    e = c.find("t3")
    assert e.status == "resolved"
    assert e.closed_seq == 151
    assert not e.is_open
    assert c.open_entries() == []
    assert [x.id for x in c.archived_entries()] == ["t3"]


def test_resolve_custom_terminal_status():
    c, _ = canon.apply_ops(canon.empty(), [_add("s1", "seeds", "the raven")], at_seq=40)
    c, _ = canon.apply_ops(c, [{"op": "resolve", "id": "s1", "status": "paid"}], at_seq=90)
    assert c.find("s1").status == "paid"
    assert not c.find("s1").is_open


def test_malformed_ops_dropped_with_warnings():
    c, warns = canon.apply_ops(
        canon.empty(),
        [
            "garbage",  # not a dict
            {"op": "frobnicate", "id": "x"},  # bad op kind
            {"op": "add"},  # missing id
            _add("y", "nonsense_category", "text"),  # unknown category
            {"op": "update", "id": "missing"},  # update of unknown id
        ],
        at_seq=1,
    )
    assert c.entries == []
    assert len(warns) == 5


def test_apply_ops_does_not_mutate_input():
    base, _ = canon.apply_ops(canon.empty(), [_add("t1", "threads", "x")], at_seq=1)
    _ = canon.apply_ops(base, [{"op": "update", "id": "t1", "text": "y"}], at_seq=2)
    assert base.find("t1").text == "x"  # original untouched


# --- render_open: ranking, caps, visibility -----------------------------


def test_render_open_groups_and_tags_ids():
    c, _ = canon.apply_ops(
        canon.empty(),
        [_add("t1", "threads", "find the relic"), _add("w1", "world", "the Mayor rules the town")],
        at_seq=1,
    )
    md, dropped = canon.render_open(c, include_hidden=True)
    assert dropped == []
    assert "### Threads" in md and "### World" in md
    assert "[t1]" in md and "find the relic" in md


def test_render_open_excludes_hidden_unless_requested():
    c, _ = canon.apply_ops(
        canon.empty(),
        [_add("s1", "seeds", "the spy", visibility="hidden")],
        at_seq=1,
    )
    md_table, _ = canon.render_open(c, include_hidden=False)
    assert "the spy" not in md_table
    md_gm, _ = canon.render_open(c, include_hidden=True)
    assert "the spy" in md_gm and "(GM-only)" in md_gm


def test_render_open_excludes_archived():
    c, _ = canon.apply_ops(canon.empty(), [_add("t1", "threads", "done thing")], at_seq=1)
    c, _ = canon.apply_ops(c, [{"op": "resolve", "id": "t1"}], at_seq=2)
    md, _ = canon.render_open(c, include_hidden=True)
    assert md == ""


def test_render_open_per_category_cap_drops_lowest_ranked():
    # 5 threads, cap 2: the two most recently touched survive, rest dropped
    ops = [_add(f"t{i}", "threads", f"thread {i}") for i in range(5)]
    c, _ = canon.apply_ops(canon.empty(), ops, at_seq=1)
    # bump updated_seq so ordering is deterministic: t4 newest ... t0 oldest
    for i, e in enumerate(c.entries):
        e.updated_seq = i
    kept, dropped = canon.select_open(c, include_hidden=True, per_category_cap=2)
    kept_ids = {e.id for e in kept}
    assert kept_ids == {"t4", "t3"}
    assert set(dropped) == {"t2", "t1", "t0"}


def test_render_open_token_budget_keeps_at_least_one():
    ops = [_add(f"t{i}", "threads", "x" * 400) for i in range(3)]
    c, _ = canon.apply_ops(canon.empty(), ops, at_seq=1)
    kept, dropped = canon.select_open(c, include_hidden=True, budget_tokens=10)
    assert len(kept) == 1  # never drops everything
    assert len(dropped) == 2


def test_render_open_major_priority_survives_cap():
    c, _ = canon.apply_ops(
        canon.empty(),
        [
            _add("main", "threads", "the main quest", priority="major"),
            _add("side1", "threads", "a side quest"),
            _add("side2", "threads", "another side quest"),
        ],
        at_seq=1,
    )
    # make the major entry the OLDEST so only priority (not recency) can save it
    c.find("main").updated_seq = 0
    c.find("side1").updated_seq = 10
    c.find("side2").updated_seq = 11
    kept, _ = canon.select_open(c, include_hidden=True, per_category_cap=1)
    assert [e.id for e in kept] == ["main"]


# --- render_full ---------------------------------------------------------


def test_render_full_includes_archived_and_filters_by_query():
    c, _ = canon.apply_ops(
        canon.empty(),
        [_add("t1", "threads", "the poisoned duke"), _add("v1", "world", "Captain Vance")],
        at_seq=1,
    )
    c, _ = canon.apply_ops(c, [{"op": "resolve", "id": "t1"}], at_seq=2)
    full = canon.render_full(c, include_hidden=True)
    assert "poisoned duke" in full and "Captain Vance" in full  # archived still shown
    only_vance = canon.render_full(c, include_hidden=True, query="vance")
    assert "Captain Vance" in only_vance and "poisoned duke" not in only_vance


def test_render_full_is_capped_and_reports_dropped():
    # A long campaign's archive must not dump unbounded into context.
    ops = [_add(f"w{i}", "world", f"durable fact number {i} about the setting") for i in range(200)]
    c, _ = canon.apply_ops(canon.empty(), ops, at_seq=1)
    rendered = canon.render_full(c, include_hidden=True, budget_tokens=200)
    assert canon._est_tokens(rendered) <= 260  # body within budget + the short notice
    assert "more not shown" in rendered  # truncation is disclosed, not silent


def test_render_full_small_set_has_no_notice():
    c, _ = canon.apply_ops(canon.empty(), [_add("w1", "world", "a single fact")], at_seq=1)
    rendered = canon.render_full(c, include_hidden=True)
    assert "more not shown" not in rendered


# --- render_open_with_overflow (the chronicler's wider view) --------------


def test_overflow_surfaces_entries_the_gm_cap_drops():
    # 15 open threads exceed the per-category injection cap of 12; the chronicler
    # must still see the 3 it drops, so it can resolve or merge them.
    ops = [_add(f"t{i}", "threads", f"open thread number {i}") for i in range(15)]
    c, _ = canon.apply_ops(canon.empty(), ops, at_seq=1)
    injected, overflow = canon.render_open_with_overflow(c, include_hidden=True)
    assert injected and overflow
    # every open entry is reachable across the two blocks (none silently lost)
    for i in range(15):
        assert f"[t{i}]" in injected or f"[t{i}]" in overflow


def test_overflow_empty_when_everything_fits():
    c, _ = canon.apply_ops(canon.empty(), [_add("t1", "threads", "the only thread")], at_seq=1)
    injected, overflow = canon.render_open_with_overflow(c, include_hidden=True)
    assert "[t1]" in injected and overflow == ""
