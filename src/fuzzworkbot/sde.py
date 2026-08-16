"""Read-only lookups against Fuzzwork's live EVE Online SDE MySQL database (item/ship
types and the universe map) — a separate, much larger, independently-maintained
database from this bot's own MySQL DB (see config.sde_database_url)."""

from sqlalchemy import Engine, create_engine, text

SHIP_CATEGORY_ID = 6

# Curated dogma attributes shown for ships, grouped the way a fitting tool would.
# There are ~100+ dogma attributes per ship; most are internal/irrelevant, so this
# is a deliberate subset rather than a dump of dgmTypeAttributes.
SHIP_ATTRIBUTE_GROUPS: list[tuple[str, list[str]]] = [
    ("Slots", ["hiSlots", "medSlots", "lowSlots", "rigSlots"]),
    ("Hardpoints", ["turretSlotsLeft", "launcherSlotsLeft"]),
    ("Fitting", ["cpuOutput", "powerOutput"]),
    ("Tank", ["shieldCapacity", "armorHP", "hp"]),
    ("Capacitor", ["capacitorCapacity"]),
    ("Propulsion", ["maxVelocity", "baseWarpSpeed", "agility"]),
    ("Targeting", ["maxTargetRange", "maxLockedTargets", "scanResolution", "signatureRadius"]),
    ("Drones", ["droneCapacity", "droneBandwidth"]),
]

# (embed field label, result dict key) for the system-detail embed. x/y/z and
# celestialIndex/orbitIndex are deliberately left out — not useful without the rest of
# the position/orbit math, and this is a summary card, not a raw table dump.
SYSTEM_DETAIL_FIELDS: list[tuple[str, str]] = [
    ("Item ID", "itemID"),
    ("Type ID", "typeID"),
    ("Group ID", "groupID"),
    ("Solar System", "solarSystemName"),
    ("Constellation", "constellationName"),
    ("Region", "regionName"),
    ("Orbit ID", "orbitID"),
    ("Radius", "radius"),
    ("Security", "security"),
]

_engine: Engine | None = None


def get_engine(sde_database_url: str) -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(sde_database_url, pool_recycle=3600)
    return _engine


def _ranked_name_search(engine: Engine, table: str, column: str, where: str, query: str, limit: int) -> list[str]:
    """Prefix matches first (shortest name first), then substring matches to fill any
    remaining slots — a plain substring search buries an exact-ish match like "Rifter"
    under 25 alphabetically-earlier items that merely contain the query somewhere.

    table/column/where are always fixed literals from this module, never user input —
    only `query` (bound as a parameter below) comes from the Discord user."""
    with engine.connect() as conn:
        prefix_rows = conn.execute(
            text(
                f"SELECT {column} AS name FROM {table} WHERE {where} AND {column} LIKE :prefix "
                f"ORDER BY LENGTH({column}), {column} LIMIT :limit"
            ),
            {"prefix": f"{query}%", "limit": limit},
        ).fetchall()
        names = [row._mapping["name"] for row in prefix_rows]

        if len(names) < limit:
            substring_rows = conn.execute(
                text(
                    f"SELECT {column} AS name FROM {table} WHERE {where} AND {column} LIKE :substring "
                    f"AND {column} NOT LIKE :prefix ORDER BY LENGTH({column}), {column} LIMIT :limit"
                ),
                {"substring": f"%{query}%", "prefix": f"{query}%", "limit": limit - len(names)},
            ).fetchall()
            names.extend(row._mapping["name"] for row in substring_rows)

    return names


def search_type_names(sde_database_url: str, query: str, limit: int = 25) -> list[str]:
    engine = get_engine(sde_database_url)
    return _ranked_name_search(engine, "invTypes", "typeName", "published = 1", query, limit)


def lookup_type(sde_database_url: str, name: str) -> dict | None:
    engine = get_engine(sde_database_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT t.typeID, t.typeName, t.description, t.mass, t.volume, t.capacity,
                       g.groupName, g.categoryID, c.categoryName
                FROM invTypes t
                LEFT JOIN invGroups g ON g.groupID = t.groupID
                LEFT JOIN invCategories c ON c.categoryID = g.categoryID
                WHERE t.published = 1 AND t.typeName = :name
                LIMIT 1
                """
            ),
            {"name": name},
        ).fetchone()
        if row is None:
            return None
        result = dict(row._mapping)

        if result["categoryID"] == SHIP_CATEGORY_ID:
            attr_rows = conn.execute(
                text(
                    "SELECT a.attributeName, t.valueFloat, t.valueInt "
                    "FROM dgmTypeAttributes t JOIN dgmAttributeTypes a ON a.attributeID = t.attributeID "
                    "WHERE t.typeID = :type_id"
                ),
                {"type_id": result["typeID"]},
            ).fetchall()
            attrs = {}
            for attr_row in attr_rows:
                mapping = attr_row._mapping
                attrs[mapping["attributeName"]] = mapping["valueFloat"] if mapping["valueFloat"] is not None else mapping["valueInt"]
            result["ship_attributes"] = attrs
        else:
            result["ship_attributes"] = None

    return result


def search_system_names(sde_database_url: str, query: str, limit: int = 25) -> list[str]:
    engine = get_engine(sde_database_url)
    return _ranked_name_search(engine, "mapDenormalize", "itemName", "typeID = 5", query, limit)


def lookup_system(sde_database_url: str, name: str) -> dict | None:
    engine = get_engine(sde_database_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT m.itemID, m.typeID, m.groupID, m.orbitID, m.radius, m.itemName, m.security,
                       m.solarSystemID, sys.solarSystemName,
                       m.constellationID, con.constellationName,
                       m.regionID, reg.regionName
                FROM mapDenormalize m
                LEFT JOIN mapSolarSystems sys ON sys.solarSystemID = m.solarSystemID
                LEFT JOIN mapConstellations con ON con.constellationID = m.constellationID
                LEFT JOIN mapRegions reg ON reg.regionID = m.regionID
                WHERE m.typeID = 5 AND m.itemName = :name
                LIMIT 1
                """
            ),
            {"name": name},
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def get_system_neighbours(sde_database_url: str, solar_system_id: int) -> list[str]:
    """Systems one stargate jump away, via mapJumps (stargateID -> destinationID, the
    itemID of the stargate on the far end — resolved back to its solar system's name).
    Empty for systems with no stargates (most wormhole/J-space systems)."""
    engine = get_engine(sde_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT dest_sys.solarSystemName
                FROM mapDenormalize gate
                JOIN invGroups g ON g.groupID = gate.groupID
                JOIN mapJumps j ON j.stargateID = gate.itemID
                JOIN mapDenormalize dest_gate ON dest_gate.itemID = j.destinationID
                JOIN mapSolarSystems dest_sys ON dest_sys.solarSystemID = dest_gate.solarSystemID
                WHERE gate.solarSystemID = :solar_system_id AND g.groupName = 'Stargate'
                ORDER BY dest_sys.solarSystemName
                """
            ),
            {"solar_system_id": solar_system_id},
        ).fetchall()
    return [row._mapping["solarSystemName"] for row in rows]


def get_system_contents(sde_database_url: str, solar_system_id: int) -> list[dict]:
    """Every other mapDenormalize row belonging to this system — sun, planets, moons,
    stations, stargates, belts, ... — the actual "contents" of the system, as opposed
    to the single summary row lookup_system() returns for the system itself."""
    engine = get_engine(sde_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT m.itemName, m.itemID, g.groupName
                FROM mapDenormalize m
                LEFT JOIN invGroups g ON g.groupID = m.groupID
                WHERE m.solarSystemID = :solar_system_id AND m.itemID != :solar_system_id
                ORDER BY g.groupName, m.itemName
                """
            ),
            {"solar_system_id": solar_system_id},
        ).fetchall()
    return [dict(row._mapping) for row in rows]
