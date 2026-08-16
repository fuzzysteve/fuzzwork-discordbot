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
            allowable_methods=["GET", "POST"],
            allowable_codes=[200, 400],
            ignored_parameters=["api_key"],
            match_headers=True,
            stale_if_error=True,
            headers={"User-Agent": user_agent},
        )
    return _session


def get_character_name(eve_id: int, user_agent: str) -> str:
    session = get_session(user_agent)
    url = f"https://esi.evetech.net/latest/characters/{eve_id}/?datasource=tranquility"
    response = session.get(url)
    response.raise_for_status()
    return response.json()["name"]
