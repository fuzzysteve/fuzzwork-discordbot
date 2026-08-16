import os
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    guild_id: int
    validated_role_id: int
    database_url: str
    esi_user_agent: str
    sde_database_url: str


def load_config() -> Config:
    database_url = _require("DATABASE_URL")

    # Fuzzwork's own live EVE SDE database (types/ships/map data) — an existing,
    # actively-updated MySQL DB used by many fuzzwork.co.uk tools, on the same MySQL
    # server as this bot's own DB. The discordbot user has been granted read-only
    # access to it, so by default just point DATABASE_URL's same host/user/password at
    # the "eve" schema instead — no separate credential needed. Override with
    # SDE_DATABASE_URL if the SDE ever lives somewhere else.
    # str(url) masks the password ("***") by default — must render explicitly.
    default_sde_url = make_url(database_url).set(database="eve").render_as_string(hide_password=False)

    return Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        guild_id=int(_require("GUILD_ID")),
        validated_role_id=int(_require("VALIDATED_ROLE_ID")),
        database_url=database_url,
        esi_user_agent=_require("ESI_USER_AGENT"),
        sde_database_url=os.environ.get("SDE_DATABASE_URL", default_sde_url),
    )
