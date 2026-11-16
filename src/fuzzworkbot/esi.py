from datetime import timedelta

from requests_cache import CachedSession

_session: CachedSession | None = None


def get_session(user_agent: str) -> CachedSession:
    global _session
    if _session is None:
        _session = CachedSession(
            "bot_cache",
            use_cache_dir=True,
            cache_control=True,
            expire_after=timedelta(days=1),
            # Only cache successes: a cached error would keep failing /authme for that
            # character for the whole expire_after window.
            allowable_methods=["GET"],
            allowable_codes=[200],
            match_headers=True,
            stale_if_error=True,
            headers={"User-Agent": user_agent},
        )
    return _session


ESI_TIMEOUT_SECONDS = 10


def get_character_name(eve_id: int, user_agent: str) -> str:
    """Blocking — call it via asyncio.to_thread from async code."""
    session = get_session(user_agent)
    url = f"https://esi.evetech.net/latest/characters/{eve_id}/?datasource=tranquility"
    response = session.get(url, timeout=ESI_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()["name"]
