"""Shared instructions for opening a new campaign at the table."""

from __future__ import annotations

CAMPAIGN_KICKOFF_PREFIX = "[START OF CAMPAIGN."
CAMPAIGN_KICKOFF_INSTRUCTION = (
    f"{CAMPAIGN_KICKOFF_PREFIX} Out-of-character setup note to you, the GM. The players "
    "have just sat down. They may have imported one or more characters already, so inspect "
    "the current party before responding. Open the campaign:\n"
    "1. Welcome the table in a sentence or two and set up the premise. If an adventure "
    "module is attached (check the campaign context), draw the opening hook from it. Read "
    "its introduction or background first with search_campaign/read_campaign so you frame "
    "it faithfully, and share only what the players would know going in, never module "
    "secrets. If there's a premise but no module, open from the premise. If there's "
    "neither, briefly ask what kind of adventure they want.\n"
    "2. Then help them finish gathering the party. If there are no player characters yet, "
    "ask how they'd like to bring them in and lay out the options: roll up new characters "
    "together with you (you'll guide them through the rules), use pre-generated characters "
    "if the module or a source provides any (check with search_campaign/search_rules first, "
    "and only offer this if they truly exist), or import an existing sheet from a file with "
    "the /import command. If characters are already present, welcome them by name and ask "
    "whether the players want to add anyone else or are ready to continue.\n"
    "3. Do NOT create any sheets, begin the action, or advance a scene this turn. Keep this "
    "a warm, concise opening, then hand control to the players and wait for their answer.]"
)
