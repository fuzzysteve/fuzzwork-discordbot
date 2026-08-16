import os
from dataclasses import dataclass

from dotenv import load_dotenv

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


def load_config() -> Config:
    return Config(
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        guild_id=int(_require("GUILD_ID")),
        validated_role_id=int(_require("VALIDATED_ROLE_ID")),
        database_url=_require("DATABASE_URL"),
        esi_user_agent=_require("ESI_USER_AGENT"),
    )
