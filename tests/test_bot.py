from fuzzworkbot.bot import build_bot


async def test_build_bot_loads_all_cogs_and_commands(db_url, no_task_loops):
    bot = build_bot()
    try:
        assert set(bot.cogs) == {"Verification", "Giveaways", "RoleReactions", "EveLookup"}
        assert {c.name for c in bot.slash_commands} == {"authme", "auththem", "creategiveaway", "type", "system"}
        # Every slash command is guild-scoped to GUILD_ID, not global.
        assert all(set(c.guild_ids) == {1000} for c in bot.slash_commands)
        assert bot.intents.members
    finally:
        await bot.close()
