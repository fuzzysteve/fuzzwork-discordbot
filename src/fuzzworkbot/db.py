import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# utf8mb4_general_ci (MariaDB/MySQL's default ci collation) has no real collation
# weights for characters outside the Basic Multilingual Plane — which is most emoji —
# so it treats every such emoji as equal to every other one in comparisons. Emoji are
# opaque tokens here, not sortable human text, so force exact binary comparison on
# MySQL specifically (sqlite, used in tests, already compares TEXT as binary by default).
EMOJI_TYPE = String(64).with_variant(mysql.VARCHAR(64, charset="utf8mb4", collation="utf8mb4_bin"), "mysql")

# Emoji (and any other non-Latin text) need 4-byte-per-char storage — MySQL's plain
# "utf8" charset is really utf8mb3 and silently can't hold them, so every table with a
# text column that might see emoji/unicode is pinned to utf8mb4 explicitly rather than
# trusting the database's default charset (which was latin1 on the live discordbot DB).
MYSQL_TABLE_ARGS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"}


class Base(DeclarativeBase):
    pass


class UserLookup(Base):
    __tablename__ = "userlookup"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discordid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eveid: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Prize(Base):
    __tablename__ = "prizes"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    codes: Mapped[list["GiveawayCode"]] = relationship(back_populates="prize")


class GiveawayCode(Base):
    __tablename__ = "giveaway_codes"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prize_id: Mapped[int] = mapped_column(ForeignKey("prizes.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_to_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    prize: Mapped["Prize"] = relationship(back_populates="codes")


# Giveaway.status values.
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FINISHED = "finished"


class Giveaway(Base):
    __tablename__ = "giveaways"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prize_id: Mapped[int] = mapped_column(ForeignKey("prizes.id"), nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    starts_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    winner_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    code_id: Mapped[int | None] = mapped_column(ForeignKey("giveaway_codes.id"), nullable=True)

    prize: Mapped["Prize"] = relationship()


class RoleReaction(Base):
    __tablename__ = "role_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "emoji", name="uq_role_reactions_message_emoji"),
        MYSQL_TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(EMOJI_TYPE, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args = {}
    if make_url(database_url).get_backend_name() == "mysql":
        # Without this the connection negotiates utf8mb3 by default on this server,
        # which (like latin1) can't hold 4-byte characters such as most emoji.
        connect_args["charset"] = "utf8mb4"

    engine = create_engine(database_url, pool_recycle=3600, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
