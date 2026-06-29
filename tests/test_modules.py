"""Multi-module campaign arc: reconcile, active-scoping, and transitions.

Modules are ingested books in the shared source store now; a campaign attaches
them explicitly (no auto-discovery), so the tests ingest into the store and then
attach the slugs."""

import random
import shutil

from openadventure.engine.tools import build_registry
from openadventure.engine.tools.registry import ToolContext
from openadventure.ingest import pipeline
from openadventure.store.workspace import ModuleRef, titleize

MODULE_A = """\
# Death House

## Rose and Thorn

Read-aloud: Two children claim there is a monster in our house.

## Nursery

A crib sits against the wall of the nursery.
"""

MODULE_B = """\
# Barovia

## Village Square

Strahd von Zarovich rules this cursed valley from Castle Ravenloft.

## Vistani Camp

A Vistani fortune-teller named Madam Eva waits by the fire.
"""


def _ingest(workspace, name, text, tmp_path):
    source = tmp_path / f"{name}.md"
    source.write_text(text, encoding="utf-8")
    pipeline.ingest(source, workspace.book_dir(name))


def _attach(campaign, *slugs, active=None):
    """Attach the given ingested books as modules, in order, and persist."""
    meta = campaign.load_meta()
    meta.modules = [ModuleRef(slug=s, title=titleize(s), order=i) for i, s in enumerate(slugs)]
    meta.active_module = active or (slugs[0] if slugs else None)
    for module in meta.modules:
        if module.slug == meta.active_module:
            module.status = "active"
    campaign.save_meta(meta)
    return meta


def _ctx(workspace, campaign, meta) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        campaign=campaign,
        meta=meta,
        log=campaign.open_log(),
        rng=random.Random(7),
    )


# --- reconcile --------------------------------------------------------------


def test_sync_activates_first_unfinished_when_none_active(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    meta = campaign.load_meta()
    meta.modules = [
        ModuleRef(slug="death-house", title="Death House", order=0),
        ModuleRef(slug="barovia", title="Barovia", order=1),
    ]
    meta.active_module = None

    changed = campaign.sync_modules(meta, {"death-house", "barovia"})
    assert changed
    assert [m.slug for m in meta.modules] == ["death-house", "barovia"]
    assert [m.order for m in meta.modules] == [0, 1]
    assert meta.active_module == "death-house"
    assert campaign.active_module(meta).status == "active"
    # idempotent second pass
    assert not campaign.sync_modules(meta, {"death-house", "barovia"})


def test_sync_prunes_modules_whose_source_is_gone(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    meta = _attach(campaign, "death-house", "barovia", active="death-house")
    # consistent: nothing to do
    assert not campaign.sync_modules(meta, {"death-house", "barovia"})

    # the active module's source disappears from the store -> pruned, survivor activated
    shutil.rmtree(workspace.book_dir("death-house"))
    assert campaign.sync_modules(meta, set(workspace.list_books()))
    assert [m.slug for m in meta.modules] == ["barovia"]
    assert meta.active_module == "barovia"


# --- attach / detach --------------------------------------------------------


def test_add_and_remove_module(make_session, workspace, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    session = make_session(script=[])

    assert session.add_module("death-house") == "death-house"
    assert session.add_module("barovia") == "barovia"
    assert [m.slug for m in session.meta.modules] == ["death-house", "barovia"]
    assert session.meta.active_module == "death-house"  # first attached becomes active
    assert "search_campaign" in session.tools

    assert session.remove_module("death-house") is True
    assert [m.slug for m in session.meta.modules] == ["barovia"]
    assert session.meta.active_module == "barovia"  # survivor re-activated
    assert session.campaign.load_meta().active_module == "barovia"  # persisted


# --- context scoping --------------------------------------------------------


def test_overview_lists_section_paths_for_active_module_only(
    make_session, workspace, campaign, tmp_path
):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    _attach(campaign, "death-house", "barovia", active="death-house")

    session = make_session()
    overview = session.campaign_arc_overview()
    assert overview is not None
    # both modules are named in the arc list...
    assert "death-house" in overview and "barovia" in overview
    # ...but only the active module's section paths are dumped
    assert "death-house/nursery.md" in overview
    assert "barovia/village-square.md" not in overview
    # sections are listed in the source's reading order, not filename order: "Rose
    # and Thorn" comes before "Nursery" in the module though it sorts after it
    assert overview.index("rose-and-thorn") < overview.index("nursery")
    # each path carries its breadcrumb, so the GM sees a section's place in the tree
    assert "Rose and Thorn" in overview


# --- search scoping ---------------------------------------------------------


def test_search_campaign_defaults_to_active_module(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    meta = _attach(campaign, "death-house", "barovia", active="death-house")
    registry = build_registry(workspace, campaign, meta)
    ctx = _ctx(workspace, campaign, meta)

    # a term that only exists in the non-active module is not found by default
    miss = registry.dispatch(ctx, "search_campaign", {"query": "Strahd"})
    assert miss.summary == "0 results"
    assert "scope='all'" in miss.content

    # scope='all' reaches every module
    hit = registry.dispatch(ctx, "search_campaign", {"query": "Strahd", "scope": "all"})
    assert hit.ok and "barovia/" in hit.content

    # the active module is searched without opting in
    active = registry.dispatch(ctx, "search_campaign", {"query": "monster"})
    assert active.ok and "death-house/" in active.content


# --- transition -------------------------------------------------------------


def test_complete_module_advances_and_records(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    meta = _attach(campaign, "death-house", "barovia", active="death-house")
    registry = build_registry(workspace, campaign, meta)
    assert "complete_module" in registry  # registered with >1 module
    ctx = _ctx(workspace, campaign, meta)

    outcome = registry.dispatch(
        ctx,
        "complete_module",
        {"handoff_note": "The house collapsed; the party flees toward Barovia."},
    )
    assert outcome.ok
    assert meta.active_module == "barovia"
    statuses = {m.slug: m.status for m in meta.modules}
    assert statuses == {"death-house": "completed", "barovia": "active"}

    # persisted to disk
    assert campaign.load_meta().active_module == "barovia"
    # a transition event is emitted for the frontend
    assert outcome.events and outcome.events[0].type == "module_transition"
    assert outcome.events[0].completed == "death-house"
    assert outcome.events[0].active == "barovia"
    # logged + handoff note recorded as a durable quest note
    assert any(e.type == "module_transition" for e in campaign.open_log().read_all())
    quest = (campaign.notes_dir / "quest.jsonl").read_text(encoding="utf-8")
    assert "module handoff: Death House" in quest and "flees toward Barovia" in quest


def test_complete_module_finishes_arc_when_no_next(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    _ingest(workspace, "barovia", MODULE_B, tmp_path)
    meta = _attach(campaign, "death-house", "barovia", active="barovia")
    # mark everything but the active one done so there is no next module
    for module in meta.modules:
        module.status = "completed"
    meta.active_module = "barovia"
    next(m for m in meta.modules if m.slug == "barovia").status = "active"
    ctx = _ctx(workspace, campaign, meta)
    registry = build_registry(workspace, campaign, meta)

    outcome = registry.dispatch(ctx, "complete_module", {})
    assert outcome.ok
    assert meta.active_module is None
    assert outcome.events[0].active is None
    assert "arc is finished" in outcome.content


def test_complete_module_not_registered_for_single_module(workspace, campaign, tmp_path):
    _ingest(workspace, "death-house", MODULE_A, tmp_path)
    meta = _attach(campaign, "death-house")
    registry = build_registry(workspace, campaign, meta)
    assert "search_campaign" in registry
    assert "complete_module" not in registry
