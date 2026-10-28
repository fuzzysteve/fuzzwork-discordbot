"""sde.py against a tiny hand-built SDE in SQLite. Only the columns the queries touch
exist; IDs/values are illustrative, not real SDE data."""

import pytest
from sqlalchemy import create_engine, text

from fuzzworkbot import sde

SCHEMA = [
    "CREATE TABLE invCategories (categoryID INTEGER PRIMARY KEY, categoryName TEXT)",
    "CREATE TABLE invGroups (groupID INTEGER PRIMARY KEY, categoryID INTEGER, groupName TEXT)",
    "CREATE TABLE invTypes (typeID INTEGER PRIMARY KEY, groupID INTEGER, typeName TEXT, description TEXT, "
    "mass REAL, volume REAL, capacity REAL, published INTEGER)",
    "CREATE TABLE dgmAttributeTypes (attributeID INTEGER PRIMARY KEY, attributeName TEXT)",
    "CREATE TABLE dgmTypeAttributes (typeID INTEGER, attributeID INTEGER, valueInt INTEGER, valueFloat REAL)",
    "CREATE TABLE mapRegions (regionID INTEGER PRIMARY KEY, regionName TEXT)",
    "CREATE TABLE mapConstellations (constellationID INTEGER PRIMARY KEY, constellationName TEXT)",
    "CREATE TABLE mapSolarSystems (solarSystemID INTEGER PRIMARY KEY, solarSystemName TEXT)",
    "CREATE TABLE mapDenormalize (itemID INTEGER PRIMARY KEY, typeID INTEGER, groupID INTEGER, solarSystemID INTEGER, "
    "constellationID INTEGER, regionID INTEGER, orbitID INTEGER, radius REAL, itemName TEXT, security REAL)",
    "CREATE TABLE mapJumps (stargateID INTEGER, destinationID INTEGER)",
]

DATA = [
    "INSERT INTO invCategories VALUES (6, 'Ship'), (4, 'Material')",
    "INSERT INTO invGroups VALUES (25, 6, 'Frigate'), (18, 4, 'Mineral'), (5, 2, 'Solar System'), "
    "(10, 2, 'Stargate'), (7, 2, 'Planet')",
    "INSERT INTO invTypes VALUES "
    "(587, 25, 'Rifter', 'A frigate.', 1067000, 27289, 140, 1), "
    "(1, 25, 'Amarr Rifter Trainer', '', 1, 1, 1, 1), "
    "(2, 25, 'Rifter Blueprint Old', '', 1, 1, 1, 0), "
    "(34, 18, 'Tritanium', 'Mineral.', 0, 0.01, 0, 1)",
    "INSERT INTO dgmAttributeTypes VALUES (14, 'hiSlots'), (37, 'maxVelocity')",
    "INSERT INTO dgmTypeAttributes VALUES (587, 14, 4, NULL), (587, 37, NULL, 355.0)",
    "INSERT INTO mapRegions VALUES (10000002, 'The Forge')",
    "INSERT INTO mapConstellations VALUES (20000020, 'Kimotoro')",
    "INSERT INTO mapSolarSystems VALUES (30000142, 'Jita'), (30000144, 'Perimeter')",
    "INSERT INTO mapDenormalize VALUES "
    "(30000142, 5, 5, 30000142, 20000020, 10000002, NULL, 1.0e13, 'Jita', 0.9459), "
    "(30000144, 5, 5, 30000144, 20000020, 10000002, NULL, 1.0e13, 'Perimeter', 0.95), "
    "(40009077, 7, 7, 30000142, 20000020, 10000002, NULL, 1.0, 'Jita IV', NULL), "
    "(50001248, 16, 10, 30000142, 20000020, 10000002, NULL, 1.0, 'Stargate (Perimeter)', NULL), "
    "(50001249, 16, 10, 30000144, 20000020, 10000002, NULL, 1.0, 'Stargate (Jita)', NULL)",
    "INSERT INTO mapJumps VALUES (50001248, 50001249), (50001249, 50001248)",
]


@pytest.fixture
def sde_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'sde.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for statement in SCHEMA + DATA:
            conn.execute(text(statement))
    engine.dispose()
    monkeypatch.setattr(sde, "_engine", None)  # get_engine() caches one module-wide
    yield url
    if sde._engine is not None:
        sde._engine.dispose()


def test_type_search_ranks_prefix_matches_first(sde_url):
    # Substring match "Amarr Rifter Trainer" must come after the prefix match, and the
    # unpublished type must never appear.
    assert sde.search_type_names(sde_url, "rift") == ["Rifter", "Amarr Rifter Trainer"]


def test_type_search_respects_limit_without_duplicates(sde_url):
    assert sde.search_type_names(sde_url, "Rifter", limit=1) == ["Rifter"]
    assert sde.search_type_names(sde_url, "r", limit=10).count("Rifter") == 1


def test_lookup_ship_includes_attributes(sde_url):
    result = sde.lookup_type(sde_url, "Rifter")
    assert result["groupName"] == "Frigate" and result["categoryName"] == "Ship"
    assert result["ship_attributes"] == {"hiSlots": 4, "maxVelocity": 355.0}


def test_lookup_non_ship_has_no_attributes(sde_url):
    assert sde.lookup_type(sde_url, "Tritanium")["ship_attributes"] is None


@pytest.mark.parametrize("name", ["Rifter Blueprint Old", "Nonexistent"])
def test_lookup_type_misses(sde_url, name):
    assert sde.lookup_type(sde_url, name) is None


def test_system_search_only_returns_systems(sde_url):
    assert sde.search_system_names(sde_url, "Jita") == ["Jita"]


def test_lookup_system(sde_url):
    result = sde.lookup_system(sde_url, "Jita")
    assert result["solarSystemID"] == 30000142
    assert (result["constellationName"], result["regionName"]) == ("Kimotoro", "The Forge")
    assert sde.lookup_system(sde_url, "Jita IV") is None


def test_system_neighbours_via_stargates(sde_url):
    assert sde.get_system_neighbours(sde_url, 30000142) == ["Perimeter"]


def test_system_contents_exclude_the_system_itself(sde_url):
    contents = sde.get_system_contents(sde_url, 30000142)
    assert [(c["groupName"], c["itemName"]) for c in contents] == [
        ("Planet", "Jita IV"),
        ("Stargate", "Stargate (Perimeter)"),
    ]
