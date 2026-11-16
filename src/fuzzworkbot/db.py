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


def now_utc() -> datetime.datetime:
    """Naive datetime representing the current instant in UTC. Every DATETIME column
    in this schema is naive and is always interpreted as UTC — never mix in
    datetime.now() (server-local time) or other timezones."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class UserLookup(Base):
    __tablename__ = "userlookup"
    __table_args__ = (UniqueConstraint("discordid", name="uq_userlookup_discordid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Unique: one EVE character per Discord account. On the live table this is the
    # hand-added uq_userlookup_discordid key (the older non-unique `discordid` index is
    # still there too, redundant but harmless).
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
    # "Used" is decided by assigned_at, not by either of the two columns below — a code
    # can be claimed by a Discord winner (assigned_to_discord_id set) or handed out
    # manually to someone with no Discord ID (assigned_note set instead), so neither
    # column alone is a reliable used/unused signal on its own.
    assigned_to_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assigned_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    prize: Mapped["Prize"] = relationship(back_populates="codes")


# Giveaway.status values.
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FINISHED = "finished"

# Giveaway.entry_mode values. Giveaways posted before the Enter button existed were
# entered by reacting; the live column's server default keeps those rows "reaction".
ENTRY_REACTION = "reaction"
ENTRY_BUTTON = "button"


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
    winner_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Set to ENTRY_BUTTON when the bot posts the giveaway (cogs/giveaways.py _post_one).
    entry_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ENTRY_REACTION, server_default=ENTRY_REACTION
    )
    # `winner_discord_id` and `code_id` columns still exist in MySQL (dropping them
    # live was judged too risky to force through) but are unused now that a giveaway
    # can have multiple winners — see GiveawayWinner instead. Do not resurrect them.

    prize: Mapped["Prize"] = relationship()


class GiveawayWinner(Base):
    __tablename__ = "giveaway_winners"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(ForeignKey("giveaways.id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Null when the prize ran out of codes at draw time (shouldn't happen given
    # creation-time capacity checks, but handled defensively) — the win is still
    # recorded, just flagged for an admin to deliver a code manually.
    code_id: Mapped[int | None] = mapped_column(ForeignKey("giveaway_codes.id"), nullable=True)
    drawn_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    giveaway: Mapped["Giveaway"] = relationship()


class QueuedGiveaway(Base):
    """Upcoming prizes for `giveaway-cli giveaway random` (the systemd timer), used in
    id order instead of a random pick. Each run takes the first entry that can run right
    now and deletes it in the same transaction that inserts the Giveaway, so each entry is
    used exactly once. Entries that can't run yet stay queued. Queuing reserves no codes;
    capacity is re-checked when the run actually uses an entry. Replaces the single-row
    `next_giveaway` table."""

    __tablename__ = "giveaway_queue"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prize_id: Mapped[int] = mapped_column(ForeignKey("prizes.id"), nullable=False)
    # Null = use whatever --winners the random run was invoked with.
    winner_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_by_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    prize: Mapped["Prize"] = relationship()


class GiveawayEntry(Base):
    """One row per member who pressed a button giveaway's Enter button."""

    __tablename__ = "giveaway_entries"
    __table_args__ = (
        UniqueConstraint("giveaway_id", "discord_id", name="uq_giveaway_entries_giveaway_user"),
        MYSQL_TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(ForeignKey("giveaways.id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entered_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class RoleMenu(Base):
    """A role-menu message the bot posted itself via /rolemenu create, so it can re-render
    the embed listing that message's role_reactions rows. Menus set up with the CLI on
    someone else's message have no row here; they still work, just without the list."""

    __tablename__ = "role_menus"
    __table_args__ = MYSQL_TABLE_ARGS

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


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

    # pool_pre_ping: a pooled connection MySQL has dropped (server restart, 2013 "Lost
    # connection") is detected and replaced on checkout instead of failing the first
    # query on it — that failure is what killed the giveaway poller in Nov 2026.
    engine = create_engine(database_url, pool_recycle=3600, pool_pre_ping=True, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
