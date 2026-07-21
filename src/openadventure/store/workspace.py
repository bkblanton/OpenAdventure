"""Workspace layout: sources and campaigns on disk."""

from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from openadventure.store import snapshots
from openadventure.store.eventlog import EventLog

Mode = Literal["gm", "assistant"]

# An ingested book's declared type, recorded in its manifest at ingestion. A
# ``source`` book is rules/reference (rulebook, monster manual, setting guide);
# a ``module`` book is a published adventure. A campaign attaches each kind in
# its own bucket (``sources``/``system_source`` vs ``modules``), so a book can
# only be attached where its type belongs. Books ingested before types were
# recorded carry no type and attach as either. The type value equals the ingest
# flag that sets it (``--source``/``--module``).
BookType = Literal["source", "module"]

# How each type is named to the player in messages.
_TYPE_LABEL = {"source": "rules source", "module": "adventure module"}


class BookTypeMismatch(ValueError):
    """Raised when an ingested book is attached in the wrong bucket: a book
    ingested as an adventure module attached as a rules source, or vice versa.
    Untyped books (no ``type`` in the manifest, ingested before types existed)
    attach as either and never raise this."""

    def __init__(self, slug: str, declared: BookType, want: BookType):
        self.slug = slug
        self.declared = declared
        self.want = want
        # The ingest flag that declares a type is spelled the same as the type.
        super().__init__(
            f"{slug!r} was ingested as a {_TYPE_LABEL[declared]}, so it can't be attached as a "
            f"{_TYPE_LABEL[want]}. Re-ingest it with --{want} to use it that way."
        )


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "campaign"


def titleize(slug: str) -> str:
    """Human-readable title from a module slug, e.g. 'death-house' -> 'Death House'."""
    words = re.split(r"[-_]+", slug.strip())
    return " ".join(word.capitalize() for word in words if word) or slug


def _dedupe(items) -> list[str]:
    """Items in first-seen order, dropping blanks and duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ensure_book_type(workspace: Workspace, slug: str, want: BookType) -> None:
    """Raise :class:`BookTypeMismatch` if the ingested ``slug`` was declared a
    type other than ``want``. Untyped or not-yet-ingested books pass (the latter
    so a campaign can name a book it will ingest later)."""
    declared = workspace.book_type(slug)
    if declared is not None and declared != want:
        raise BookTypeMismatch(slug, declared, want)


ModuleStatus = Literal["pending", "active", "completed"]


class ModuleRef(BaseModel):
    """One adventure module within a campaign's arc.

    ``slug`` names an ingested book in the shared library store
    (``workspace/library/<slug>/``), so the same adventure can be a module in
    several campaigns. Only the arc state here (order, status, role) is
    per-campaign; the documents, search index, and manifest are shared. The
    party, notes, and rolling story summary are campaign-wide and carry across
    modules."""

    slug: str
    title: str
    order: int = 0
    status: ModuleStatus = "pending"
    role: str | None = None  # one-line role in the overarching arc


class CampaignMeta(BaseModel):
    name: str
    slug: str
    mode: Mode = "gm"
    tts_enabled: bool = False
    sound_effects_enabled: bool = False
    music_enabled: bool = False
    images_enabled: bool = False
    # Ingested books this campaign can search (slugs under workspace/library/). A
    # source can be a rulebook, a monster manual, or a setting guide: anything worth
    # searching during play. ``system_source`` names the one that defines the rules
    # system and seeds the character template; it should be one of ``sources``.
    sources: list[str] = Field(default_factory=list)
    system_source: str | None = None
    premise: str | None = None
    arc: str | None = None  # overarching story spanning all modules
    modules: list[ModuleRef] = Field(default_factory=list)
    active_module: str | None = None  # slug of the module currently in play
    created_at: str = ""
    setup_done: bool = False  # the setup wizard has run for this campaign
    settings: dict[str, Any] = Field(default_factory=dict)  # GenerationSettings overrides


class Campaign:
    """Path bundle + metadata for one campaign directory."""

    def __init__(self, root: Path):
        self.root = root

    # --- paths ---------------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.root / "campaign.json"

    @property
    def log_path(self) -> Path:
        return self.root / "log.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.json"

    @property
    def canon_path(self) -> Path:
        return self.root / "canon.json"

    @property
    def scene_path(self) -> Path:
        return self.root / "scene.json"

    @property
    def encounter_path(self) -> Path:
        return self.root / "encounter.json"

    @property
    def clocks_path(self) -> Path:
        return self.root / "clocks.json"

    @property
    def usage_path(self) -> Path:
        return self.root / "usage.json"

    @property
    def characters_dir(self) -> Path:
        return self.root / "characters"

    @property
    def npcs_dir(self) -> Path:
        return self.root / "npcs"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def music_dir(self) -> Path:
        return self.root / "music"

    @property
    def checkpoints_dir(self) -> Path:
        return self.root / "checkpoints"

    @property
    def originals_dir(self) -> Path:
        return self.root / "originals"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    # --- meta ----------------------------------------------------------
    def load_meta(self) -> CampaignMeta:
        data = snapshots.load_json(self.meta_path)
        if data is None:
            raise FileNotFoundError(f"no campaign.json in {self.root}")
        if data.get("mode") == "dm":  # legacy mode name, renamed to "gm"
            data["mode"] = "gm"
        if "ruleset" in data:  # legacy single ruleset, renamed to sources + system_source
            legacy = data.pop("ruleset")
            if legacy and not data.get("sources"):
                data["sources"] = [legacy]
                data.setdefault("system_source", legacy)
        settings = data.get("settings")
        if isinstance(settings, dict):
            settings.pop("images_auto", None)
            settings.pop("music_auto", None)
        return CampaignMeta.model_validate(data)

    def save_meta(self, meta: CampaignMeta) -> None:
        snapshots.save_json(self.meta_path, meta)

    def open_log(self) -> EventLog:
        return EventLog(self.log_path)

    # --- modules -------------------------------------------------------
    def sync_modules(self, meta: CampaignMeta, ingested: set[str]) -> bool:
        """Reconcile ``meta.modules`` against the shared source store.

        Modules are attached explicitly now (a ModuleRef.slug names an ingested
        book in ``workspace/library/``), so this no longer auto-discovers: it
        drops refs whose source is gone from ``ingested``, renumbers order, and
        keeps ``active_module`` valid, activating the first unfinished module
        when nothing is active. Mutates ``meta`` in place and returns whether it
        changed, so callers can persist only when needed."""
        before = meta.model_dump()
        meta.modules = [m for m in meta.modules if m.slug in ingested]
        meta.modules.sort(key=lambda m: m.order)
        for index, module in enumerate(meta.modules):
            module.order = index

        slugs = {m.slug for m in meta.modules}
        if meta.active_module not in slugs:
            meta.active_module = None
        if meta.active_module is None and meta.modules:
            nxt = next((m for m in meta.modules if m.status != "completed"), meta.modules[0])
            meta.active_module = nxt.slug
        if meta.active_module is not None:
            for module in meta.modules:
                if module.slug == meta.active_module and module.status == "pending":
                    module.status = "active"

        return meta.model_dump() != before

    def active_module(self, meta: CampaignMeta) -> ModuleRef | None:
        return next((m for m in meta.modules if m.slug == meta.active_module), None)


class Workspace:
    def __init__(self, root: Path):
        self.root = root

    @property
    def campaigns_dir(self) -> Path:
        return self.root / "campaigns"

    @property
    def library_dir(self) -> Path:
        return self.root / "library"

    @property
    def history_path(self) -> Path:
        return self.root / ".repl_history"

    def ensure(self) -> None:
        self.campaigns_dir.mkdir(parents=True, exist_ok=True)
        self.library_dir.mkdir(parents=True, exist_ok=True)

    def book_dir(self, name: str) -> Path:
        return self.library_dir / slugify(name)

    def book_type(self, name: str) -> BookType | None:
        """The declared type of an ingested book (``source`` for rules/reference
        or ``module`` for an adventure), or None for a book ingested before types
        were recorded, which attaches as either."""
        manifest = snapshots.load_json(self.book_dir(name) / "manifest.json")
        kind = (manifest or {}).get("type")
        return kind if kind in ("source", "module") else None

    def list_books(self, kind: BookType | None = None) -> list[str]:
        """Ingested book slugs (every library dir holding a manifest). With
        ``kind``, keep only books of that type plus untyped books; the latter
        are grandfathered and attach as either."""
        if not self.library_dir.is_dir():
            return []
        names = sorted(
            d.name for d in self.library_dir.iterdir() if (d / "manifest.json").is_file()
        )
        if kind is None:
            return names
        return [n for n in names if self.book_type(n) in (kind, None)]

    def list_campaigns(self) -> list[CampaignMeta]:
        if not self.campaigns_dir.is_dir():
            return []
        metas = []
        for d in sorted(self.campaigns_dir.iterdir()):
            campaign = Campaign(d)
            if campaign.meta_path.is_file():
                metas.append(campaign.load_meta())
        return metas

    def campaign(self, slug: str) -> Campaign:
        c = Campaign(self.campaigns_dir / slug)
        if not c.meta_path.is_file():
            raise FileNotFoundError(f"no campaign named {slug!r}")
        return c

    def create_campaign(
        self,
        name: str,
        *,
        mode: Mode = "gm",
        sources: list[str] | None = None,
        system_source: str | None = None,
        modules: list[str] | None = None,
        premise: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Campaign:
        self.ensure()
        slug = slugify(name)
        root = self.campaigns_dir / slug
        if root.exists():
            raise FileExistsError(f"campaign {slug!r} already exists")
        src = _dedupe(slugify(s) for s in (sources or []))
        sys_src = slugify(system_source) if system_source else (src[0] if src else None)
        mod_slugs = _dedupe(slugify(m) for m in (modules or []))
        for source_slug in src:
            ensure_book_type(self, source_slug, "source")
        for module_slug in mod_slugs:
            ensure_book_type(self, module_slug, "module")
        try:
            # Atomically reserve the campaign slug after validation. This keeps
            # bad book selections from leaving a poisoned directory and closes
            # the check-then-create race between concurrent frontends.
            root.mkdir()
        except FileExistsError:
            raise FileExistsError(f"campaign {slug!r} already exists") from None
        try:
            campaign = Campaign(root)
            for d in (
                campaign.characters_dir,
                campaign.npcs_dir,
                campaign.notes_dir,
                campaign.docs_dir,
            ):
                d.mkdir()
            module_refs = [
                ModuleRef(slug=s, title=titleize(s), order=i) for i, s in enumerate(mod_slugs)
            ]
            active = mod_slugs[0] if mod_slugs else None
            for ref in module_refs:
                if ref.slug == active:
                    ref.status = "active"
            meta = CampaignMeta(
                name=name,
                slug=slug,
                mode=mode,
                sources=src,
                system_source=sys_src,
                modules=module_refs,
                active_module=active,
                premise=premise,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                settings=settings or {},
            )
            campaign.save_meta(meta)
        except BaseException:
            # This process reserved the root above, so it is safe to remove if
            # any later initialization step fails. A retry can then use the slug.
            shutil.rmtree(root, ignore_errors=True)
            raise
        return campaign

    def delete_campaign(self, slug: str) -> None:
        """Permanently remove a campaign and its whole story (characters, notes,
        log, checkpoints). Raises :class:`FileNotFoundError` if it doesn't exist."""
        campaign = self.campaign(slug)
        shutil.rmtree(campaign.root)

    def rename_campaign(self, slug: str, new_name: str) -> Campaign:
        """Give a campaign a new display name, re-slugging it to match and moving
        its directory. Renaming to a name that slugs to the same value keeps the
        directory in place and only updates the display name. Raises
        :class:`FileNotFoundError` if the campaign is missing, or
        :class:`FileExistsError` if the new slug collides with another campaign."""
        campaign = self.campaign(slug)
        new_slug = slugify(new_name)
        if new_slug != slug:
            dest = self.campaigns_dir / new_slug
            if dest.exists():
                raise FileExistsError(f"campaign {new_slug!r} already exists")
            campaign.root.rename(dest)
            campaign = Campaign(dest)
        meta = campaign.load_meta()
        meta.name = new_name
        meta.slug = new_slug
        campaign.save_meta(meta)
        return campaign

    def fork_campaign(self, slug: str, new_name: str) -> Campaign:
        """Copy a campaign into a new one named ``new_name``, branching the whole
        story (characters, notes, log, checkpoints) at its current state. The fork
        gets its own slug and a fresh ``created_at``. Raises
        :class:`FileNotFoundError` if the source is missing, or
        :class:`FileExistsError` if the new slug collides with another campaign."""
        source = self.campaign(slug)
        new_slug = slugify(new_name)
        dest = self.campaigns_dir / new_slug
        if dest.exists():
            raise FileExistsError(f"campaign {new_slug!r} already exists")
        shutil.copytree(source.root, dest)
        forked = Campaign(dest)
        meta = forked.load_meta()
        meta.name = new_name
        meta.slug = new_slug
        meta.created_at = datetime.now(UTC).isoformat(timespec="seconds")
        forked.save_meta(meta)
        return forked
