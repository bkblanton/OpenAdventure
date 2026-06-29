"""AI-generated "Previously, on…" recap."""

from openadventure.engine.session import GameSession
from openadventure.providers.fake import FakeProvider
from openadventure.store import canon
from tests.conftest import collect
from tests.test_agent_loop import text_turn


def _recap_input(provider) -> str:
    """The user text the recap model was given on the last call."""
    return "\n".join(b.text for m in provider.calls[-1].messages for b in m.content)


def test_first_gm_message_only_turn(make_session):
    session = make_session(script=[])
    assert session.first_gm_message_if_only_turn() is None  # nothing yet

    session.log.append("user_message", {"text": "hi"})
    session.log.append("gm_message", {"text": "Welcome to the dungeon."})
    assert session.first_gm_message_if_only_turn() == "Welcome to the dungeon."

    session.log.append("user_message", {"text": "onward"})
    session.log.append("gm_message", {"text": "You proceed."})
    assert session.first_gm_message_if_only_turn() is None  # more than one turn


def _seed_canon_with_secret(campaign):
    c, _ = canon.apply_ops(
        canon.empty(),
        [
            {"op": "add", "id": "chapel", "category": "threads", "text": "Find the chapel key"},
            {
                "op": "add",
                "id": "baron",
                "category": "world",
                "text": "The baron is a vampire",
                "visibility": "hidden",
            },
        ],
        at_seq=1,
    )
    canon.save(campaign, c)


async def test_recap_omits_hidden_canon_for_players(make_session):
    # gm mode: the recap is read to the players, so hidden canon must not reach
    # the recap model at all.
    provider = FakeProvider(script=[text_turn("You meet the baron."), text_turn("recap")])
    session = make_session(provider=provider)
    await collect(session.handle_input("hello"))
    _seed_canon_with_secret(session.campaign)
    await session.recap()

    user_text = _recap_input(provider)
    assert "Find the chapel key" in user_text  # open canon is fed in
    assert "The baron is a vampire" not in user_text  # hidden canon is withheld


async def test_recap_includes_hidden_canon_for_gm(config, workspace):
    # assistant mode: the recap is for the GM behind the screen, so hidden canon
    # is fair game.
    campaign = workspace.create_campaign("GM Notes", mode="assistant")
    provider = FakeProvider(script=[text_turn("You meet the baron."), text_turn("recap")])
    session = GameSession(config, workspace, campaign, provider, session_seed=1)
    await collect(session.handle_input("hello"))
    _seed_canon_with_secret(campaign)
    await session.recap()

    assert "The baron is a vampire" in _recap_input(provider)


# --- AI "Previously, on…" recap -------------------------------------------


async def test_ai_recap_focuses_on_recent_play(make_session):
    provider = FakeProvider(
        script=[
            text_turn("You enter the crypt."),  # the play turn
            text_turn("Previously, you cracked open the crypt door."),  # the recap
        ]
    )
    session = make_session(provider=provider)
    await collect(session.handle_input("I open the crypt door"))

    before = session.session_usage.output_tokens
    text = await session.recap()

    assert text == "Previously, you cracked open the crypt door."
    assert session.session_usage.output_tokens == before + 20  # usage accrued

    call = provider.calls[-1]
    assert call.tools == []  # recap is a tool-free completion
    assert call.settings.thinking is False
    assert call.settings.max_tokens == 450  # medium verbosity ceiling
    system_text = "\n".join(block.text for block in call.system)
    assert "Previously, on" in system_text
    assert "for the players" in system_text  # spoiler-safe framing in gm mode
    user_text = "\n".join(block.text for msg in call.messages for block in msg.content)
    assert "I open the crypt door" in user_text
    assert "You enter the crypt." in user_text


async def test_ai_recap_verbosity_sizes_output(make_session):
    provider = FakeProvider(script=[text_turn("ok"), text_turn("short")])
    session = make_session(provider=provider)
    session.set_override("verbosity", "low")
    await collect(session.handle_input("look around"))

    await session.recap()

    call = provider.calls[-1]
    assert call.settings.max_tokens == 250
    system_text = "\n".join(block.text for block in call.system)
    assert "essentials" in system_text


async def test_ai_recap_none_when_nothing_happened(make_session):
    provider = FakeProvider(script=[])
    session = make_session(provider=provider)

    assert await session.recap() is None
    assert provider.calls == []  # no model call when there's no play to recap


async def test_ai_recap_none_without_provider(config, workspace, campaign):
    session = GameSession(config, workspace, campaign, None, session_seed=1)
    session.log.append("user_message", {"text": "hi"})
    session.log.append("gm_message", {"text": "hello"})

    assert await session.recap() is None
