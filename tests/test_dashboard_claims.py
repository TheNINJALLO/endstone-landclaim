import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from endstone_landclaim.web_dashboard import app as dashboard  # noqa: E402


PUBLIC_URL = "https://landclaim.oninetwork.com"


def initialize_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE claims (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, dimension TEXT NOT NULL,
            x1 INTEGER, y1 INTEGER, z1 INTEGER, x2 INTEGER, y2 INTEGER, z2 INTEGER,
            parent_claim TEXT, is_child INTEGER DEFAULT 0,
            flags_block_break INTEGER DEFAULT 1,
            flags_block_place INTEGER DEFAULT 1,
            flags_interact INTEGER DEFAULT 1,
            flags_perimeter_particles INTEGER DEFAULT 1,
            flags_kill_passive_mobs INTEGER DEFAULT 0,
            flags_kill_hostile_mobs INTEGER DEFAULT 0,
            flags_ignore_named_mobs INTEGER DEFAULT 1
        );
        CREATE TABLE claim_owners (claim_key TEXT, uuid TEXT);
        CREATE TABLE pending_names (claim_key TEXT, player_name TEXT);
        CREATE TABLE player_mapping (
            name_lower TEXT PRIMARY KEY, uuid TEXT, display_name TEXT, last_seen REAL
        );
        """
    )
    connection.commit()
    connection.close()


def make_client(tmp_path):
    dashboard.configure(
        data_folder=str(tmp_path),
        public_url=PUBLIC_URL,
        allowed_discord_ids=[],
        master_password="test-password",
    )
    initialize_database(tmp_path / "landclaims.db")
    dashboard.app.config.update(TESTING=True)
    client = dashboard.app.test_client()
    response = client.post(
        "/auth/password", json={"password": "test-password"}, base_url=PUBLIC_URL
    )
    assert response.status_code == 200
    return client


def create_claim(client, name, **overrides):
    payload = {
        "name": name,
        "dimension": "overworld",
        "x1": 0,
        "y1": -64,
        "z1": 0,
        "x2": 100,
        "y2": 320,
        "z2": 100,
        "flags": {"block_break": False, "kill_hostile_mobs": True},
    }
    payload.update(overrides)
    return client.post("/api/claims", json=payload, base_url=PUBLIC_URL)


def test_dashboard_rejects_zero_volume_claim(tmp_path):
    client = make_client(tmp_path)

    response = create_claim(client, "Flat", x2=0)

    assert response.status_code == 400


def test_dashboard_rename_preserves_fields_and_updates_children(tmp_path):
    client = make_client(tmp_path)
    assert create_claim(client, "Parent").status_code == 201
    assert create_claim(
        client,
        "Child",
        x1=10,
        z1=10,
        x2=20,
        z2=20,
        is_child=True,
        parent_claim="parent",
    ).status_code == 201

    response = client.put(
        "/api/claims/parent", json={"name": "Renamed Parent"}, base_url=PUBLIC_URL
    )

    assert response.status_code == 200
    connection = sqlite3.connect(tmp_path / "landclaims.db")
    connection.row_factory = sqlite3.Row
    renamed = connection.execute(
        "SELECT * FROM claims WHERE key = 'renamed parent'"
    ).fetchone()
    child = connection.execute("SELECT * FROM claims WHERE key = 'child'").fetchone()
    connection.close()
    assert renamed["x2"] == 100
    assert renamed["flags_block_break"] == 0
    assert renamed["flags_kill_hostile_mobs"] == 1
    assert child["parent_claim"] == "renamed parent"

    blocked_delete = client.delete(
        "/api/claims/renamed%20parent", base_url=PUBLIC_URL
    )
    assert blocked_delete.status_code == 409
