import logging

import disnake
from disnake.ext import commands

from fuzzworkbot.config import load_config
from fuzzworkbot.db import make_session_factory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EXTENSIONS = [
    "fuzzworkbot.cogs.verification",
    "fuzzworkbot.cogs.giveaways",
    "fuzzworkbot.cogs.role_reactions",
]


def build_bot() -> commands.InteractionBot:
    config = load_config()

    # Members intent is privileged: must also be enabled for this bot in the
    # Discord Developer Portal, it's used to resolve members for role add/remove.
    intents = disnake.Intents.default()
    intents.members = True

    bot = commands.InteractionBot(intents=intents)
    bot.config = config
    bot.session_factory = make_session_factory(config.database_url)

    for extension in EXTENSIONS:
        bot.load_extension(extension)

    return bot


def main():
    bot = build_bot()
    bot.run(bot.config.discord_bot_token)


if __name__ == "__main__":
    main()
