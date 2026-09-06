# =================================================================
# Admin Land Claims – Endstone (Bedrock) plugin
# =================================================================

# NOTE: Do NOT use `from __future__ import annotations` here.
# Endstone's @event_handler inspects runtime type hints on the `event`
# parameter. Keeping real class objects (not stringified annotations)
# ensures correct registration.

import json
import math
import os
import time
import uuid
import threading
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from endstone import ColorFormat, Player
from endstone.actor import Mob
from endstone.command import Command, CommandSender
from endstone.event import (
    event_handler,
    EventPriority,
    # Event classes (must be imported so runtime annotations are real types)
    BlockBreakEvent,
    BlockPlaceEvent,
    PlayerInteractEvent,
    PlayerMoveEvent,
    ActorSpawnEvent,
    PlayerJoinEvent,
)
from endstone.form import ActionForm, ModalForm, MessageForm, TextInput, Dropdown, Toggle
from endstone.plugin import Plugin

if TYPE_CHECKING:
    # Only for type-checkers; not needed at runtime
    from __main__ import AdminLandClaims


# -----------------------------
# Data model
# -----------------------------
@dataclass
class ClaimFlags:
    """Defines the set of permissions for a specific claim."""
    block_break: bool = True  # Protection is active when False
    block_place: bool = True  # Protection is active when False
    interact: bool = True     # Protection is active when False
    perimeter_particles: bool = True  # Show particles around claim perimeter
    kill_passive_mobs: bool = False  # Kill passive mobs in claim
    kill_hostile_mobs: bool = False  # Kill hostile mobs in claim
    ignore_named_mobs: bool = True   # Don't kill mobs with custom names


@dataclass
class Claim:
    """Represents a single claimed area in the world."""
    name: str                # Display name (can include spaces & caps)
    dimension: str           # Dimension key (lowercase string)
    x1: int                  # First corner X coordinate
    y1: int                  # First corner Y coordinate
    z1: int                  # First corner Z coordinate
    x2: int                  # Second corner X coordinate
    y2: int                  # Second corner Y coordinate
    z2: int                  # Second corner Z coordinate
    allowed_uuids: List[str]
    flags: ClaimFlags
    parent_claim: Optional[str] = None  # Name of parent claim if this is a child
    is_child: bool = False   # Whether this is a child claim
    pending_names: Optional[List[str]] = None  # Player names awaiting UUID resolution

    def bounds_xz(self) -> Tuple[int, int, int, int]:
        """Returns normalized boundaries (min_x, max_x, min_z, max_z)."""
        return min(self.x1, self.x2), max(self.x1, self.x2), min(self.z1, self.z2), max(self.z1, self.z2)

    def bounds_xyz(self) -> Tuple[int, int, int, int, int, int]:
        """Returns normalized boundaries (min_x, max_x, min_y, max_y, min_z, max_z)."""
        return (min(self.x1, self.x2), max(self.x1, self.x2),
                min(self.y1, self.y2), max(self.y1, self.y2),
                min(self.z1, self.z2), max(self.z1, self.z2))

    def contains(self, x: float, z: float, dim: str) -> bool:
        """Checks if (x, z, dim) is inside this claim."""
        if dim != self.dimension:
            return False
        minx, maxx, minz, maxz = self.bounds_xz()
        block_x, block_z = math.floor(x), math.floor(z)
        return (minx <= block_x <= maxx) and (minz <= block_z <= maxz)

    def contains_3d(self, x: float, y: float, z: float, dim: str) -> bool:
        """Checks if (x, y, z, dim) is inside this claim."""
        if dim != self.dimension:
            return False
        minx, maxx, miny, maxy, minz, maxz = self.bounds_xyz()
        block_x, block_y, block_z = math.floor(x), math.floor(y), math.floor(z)
        return (minx <= block_x <= maxx) and (miny <= block_y <= maxy) and (minz <= block_z <= maxz)

    def area(self) -> int:
        """Total area in blocks."""
        minx, maxx, minz, maxz = self.bounds_xz()
        return (maxx - minx + 1) * (maxz - minz + 1)

    def volume(self) -> int:
        """Total volume in blocks."""
        minx, maxx, miny, maxy, minz, maxz = self.bounds_xyz()
        return (maxx - minx + 1) * (maxy - miny + 1) * (maxz - minz + 1)

    def center_xz(self) -> Tuple[float, float]:
        """Returns the center point of the claim in X,Z coordinates."""
        minx, maxx, minz, maxz = self.bounds_xz()
        return ((minx + maxx) / 2.0, (minz + maxz) / 2.0)

    def distance_to_claim(self, other: "Claim") -> float:
        """Calculate the minimum distance between this claim and another claim."""
        if self.dimension != other.dimension:
            return float('inf')

        self_min_x, self_max_x, self_min_z, self_max_z = self.bounds_xz()
        other_min_x, other_max_x, other_min_z, other_max_z = other.bounds_xz()
        dx = max(other_min_x - self_max_x, self_min_x - other_max_x, 0)
        dz = max(other_min_z - self_max_z, self_min_z - other_max_z, 0)
        return math.hypot(dx, dz)


@dataclass
class ClaimRequest:
    """Represents a pending claim request from a player."""
    request_id: str              # Unique identifier for the request
    player_uuid: str             # UUID of the requesting player
    player_name: str             # Name of the requesting player
    claim_name: str              # Requested claim name
    dimension: str               # Dimension key (lowercase string)
    x1: int                      # First corner X coordinate
    y1: int                      # First corner Y coordinate
    z1: int                      # First corner Z coordinate
    x2: int                      # Second corner X coordinate
    y2: int                      # Second corner Y coordinate
    z2: int                      # Second corner Z coordinate
    timestamp: float             # When the request was created
    status: str = "pending"      # Status: "pending", "approved", "denied"


# -----------------------------
# Event Listener Class
# -----------------------------
class LandClaimListener:
    """
    Handles all game events for the AdminLandClaims plugin.
    Keeping listeners on a separate class is fine—what matters is
    that the `event` parameter has a *typed* annotation.
    """

    def __init__(self, plugin: "AdminLandClaims"):
        self.plugin = plugin

    @event_handler(priority=EventPriority.NORMAL, ignore_cancelled=True)
    def on_block_break(self, event: BlockBreakEvent):
        """Prevents unauthorized block breaking in claims."""
        player = event.player
        loc = event.block.location
        dim = self.plugin._player_dimension_name(player)
        claim = self.plugin._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)
        if claim and not claim.flags.block_break and self.plugin._is_protected_action(player, claim):
            event.cancel()
            # No notification for protection messages as requested

    @event_handler(priority=EventPriority.NORMAL, ignore_cancelled=True)
    def on_block_place(self, event: BlockPlaceEvent):
        """Prevents unauthorized block placing in claims."""
        player = event.player
        loc = event.block.location
        dim = self.plugin._player_dimension_name(player)
        claim = self.plugin._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)
        if claim and not claim.flags.block_place and self.plugin._is_protected_action(player, claim):
            event.cancel()
            # No notification for protection messages as requested

    @event_handler(priority=EventPriority.NORMAL, ignore_cancelled=True)
    def on_player_interact(self, event: PlayerInteractEvent):
        """Prevents unauthorized interactions in claims."""
        if not event.block:
            return
        player = event.player
        dim = self.plugin._player_dimension_name(player)
        bloc = event.block.location
        claim = self.plugin._get_smallest_containing_claim_3d(bloc.x, bloc.y, bloc.z, dim)
        if claim and not claim.flags.interact and self.plugin._is_protected_action(player, claim):
            event.cancel()
            # No notification for protection messages as requested

    @event_handler(priority=EventPriority.LOW)
    def on_player_move(self, event: PlayerMoveEvent):
        """Sends toast notifications when players enter or leave claims."""
        player = event.player

        # Short-circuit if the player hasn't changed block X/Z
        try:
            if (
                event.from_location.block_x == event.to_location.block_x
                and event.from_location.block_z == event.to_location.block_z
            ):
                return
        except AttributeError:
            # Fallback: compare floored coordinates if block_* not present
            fx, fz = int(event.from_location.x), int(event.from_location.z)
            tx, tz = int(event.to_location.x), int(event.to_location.z)
            if fx == tx and fz == tz:
                return

        dim_name = self.plugin._player_dimension_name(player)
        to_loc = event.to_location
        now = self.plugin._get_smallest_containing_claim(to_loc.x, to_loc.z, dim_name)
        key = self.plugin._player_uuid(player)
        prev_name = self.plugin._entered_claim_by_player.get(key)
        now_name = now.name if now else None

        if prev_name == now_name:
            return

        self.plugin._entered_claim_by_player[key] = now_name
        if now:
            # Send notification based on configured type
            self.plugin._send_claim_notification(player, f"Entering {now.name}", "You've entered a protected area!")
        elif prev_name is not None:
            self.plugin._send_claim_notification(player, "Entering Wilderness", "Unclaimed land.")

    @event_handler(priority=EventPriority.HIGH, ignore_cancelled=True)
    def on_actor_spawn(self, event: ActorSpawnEvent):
        """Prevent configured mobs from entering the world inside a claim."""
        try:
            actor = event.actor
            if self.plugin._mob_cleanup_enabled and self.plugin._mob_cleanup_cancel_spawns:
                claim = self.plugin._claim_containing_actor(actor)
                if claim and self.plugin._should_kill_mob(actor, claim):
                    event.cancel()
        except Exception as exc:
            self.plugin.logger.error(f"Error in actor spawn handler: {exc}")

    @event_handler(priority=EventPriority.NORMAL)
    def on_player_join(self, event: PlayerJoinEvent):
        """Resolve pending player names to UUIDs when players join."""
        try:
            player = event.player
            player_name = player.name.lower()
            player_uuid = str(player.unique_id)
            
            # Update player name mapping
            self.plugin._update_player_mapping(player_name, player_uuid, player.name)
            
            # Resolve pending names in all claims
            self.plugin._resolve_pending_name(player_name, player_uuid)
            
            self.plugin.logger.debug(f"Mapped player {player.name} to UUID {player_uuid}")
        except Exception as e:
            self.plugin.logger.error(f"Error in player join handler: {e}")


# -----------------------------
# Plugin
# -----------------------------
class AdminLandClaims(Plugin):
    api_version = "0.11"
    
    version = "10.0.7"  # Bounded, name-tag-aware mob control and stability fixes

    commands = {
        "landclaim": {"description": "Opens the land claim management UI.", "permission": "adminlandclaims.command"},
        "requestclaim": {"description": "Request a new land claim.", "permission": "adminlandclaims.command"},
        "lcadmin": {"description": "Admin commands for land claim settings.", "permission": "adminlandclaims.admin"},
        "lcdebug": {"description": "Debug commands for land claim testing.", "permission": "adminlandclaims.admin"},
    }
    permissions = {
        "adminlandclaims.command": {"description": "Allows using the land claim commands.", "default": "op"},
        "adminlandclaims.bypass":  {"description": "Bypass claim protections.", "default": "op"},
        "adminlandclaims.admin":   {"description": "Access to admin configuration commands.", "default": "op"},
    }

    # ------------------ Lifecycle ------------------
    def on_enable(self):
        self._claims: Dict[str, Claim] = {}                  # key = normalized name
        self._claim_requests: Dict[str, ClaimRequest] = {}   # key = request_id
        self._entered_claim_by_player: Dict[str, Optional[str]] = {}
        self._particle_task_id: Optional[int] = None  # For particle display task
        self._mob_killer_task_id: Optional[int] = None  # For mob killing task
        self._notification_type: str = "toast"  # Options: "toast", "tip", "chat"
        self._player_name_mapping: Dict[str, Dict] = {}  # player_name -> {uuid, display_name, last_seen}
        self._web_server_thread: Optional[threading.Thread] = None
        
        # Mob cleanup is based only on loaded actors; claim area is never iterated.
        self._mob_cleanup_enabled: bool = True
        self._mob_cleanup_cancel_spawns: bool = True
        self._mob_cleanup_interval_ticks: int = 60
        self._mob_cleanup_actors_per_tick: int = 200
        self._mob_cleanup_max_removals_per_tick: int = 25
        self._mob_cleanup_max_actor_snapshot: int = 20_000
        self._mob_sweep_actors: Optional[List[object]] = None
        self._mob_sweep_index: int = 0
        self._mob_sweep_wait_ticks: int = 0
        self._mob_sweep_removed: int = 0
        self._mob_snapshot_offset: int = 0
        self._mob_snapshot_limit_warned: bool = False
        self._mob_sweep_claims_by_dimension: Dict[str, List[Claim]] = {}
        
        # Web dashboard configuration
        self._dashboard_enabled: bool = False
        self._dashboard_port: int = 25590
        self._dashboard_host: str = "127.0.0.1"
        self._dashboard_public_url: str = "https://landclaim.oninetwork.com"
        self._discord_client_id: Optional[str] = None
        self._discord_client_secret: Optional[str] = None
        self._discord_redirect_uri: Optional[str] = "https://landclaim.oninetwork.com/auth/callback"
        self._allowed_discord_ids: List[str] = []
        self._master_password: str = ""  # Master password for dashboard login (empty = disabled)
        
        # Initialize SQLite database and migrate JSON if needed
        self._init_database()
        self._migrate_json_to_sqlite()
        
        self._load_claims()
        self._load_claim_requests()
        self._load_config()
        self._load_player_mapping()
        self.logger.info(f"[{self.name}] Loaded {len(self._claims)} claims from database.")
        self.logger.info(f"[{self.name}] Loaded {len(self._claim_requests)} pending claim requests.")
        self.logger.info(f"[{self.name}] Loaded {len(self._player_name_mapping)} player name mappings.")

        # Register the listener instance (typed handlers are required)
        self._listener = LandClaimListener(self)
        self.register_events(self._listener)
        self.logger.info(f"[{self.name}] Event listeners have been registered.")

        # Start particle display task
        self._start_particle_task()

        # Start mob killer task
        self._start_mob_killer_task()
        
        # Start web dashboard server
        self._start_web_dashboard()

    def on_disable(self):
        self._save_claims()
        self._save_claim_requests()
        # self._save_config()  # Do not save config on disable to prevent overwriting manual edits
        self._save_player_mapping()
        # Stop particle task
        if self._particle_task_id is not None:
            self.server.scheduler.cancel_task(self._particle_task_id)
        # Stop mob killer task
        if self._mob_killer_task_id is not None:
            self.server.scheduler.cancel_task(self._mob_killer_task_id)
            self._mob_killer_task_id = None
        self._reset_mob_sweep()
        # Stop the Flask web dashboard server cleanly
        try:
            from endstone_landclaim.web_dashboard.app import stop_server
            stop_server()
            # Wait for the server thread to finish
            if self._web_server_thread is not None:
                self._web_server_thread.join(timeout=5)
                self._web_server_thread = None
        except Exception as e:
            self.logger.warning(f"[{self.name}] Error stopping web dashboard: {e}")
        self.logger.info(f"[{self.name}] Claims saved and web dashboard stopped.")

    # ------------------ Persistence ------------------
    @property
    def _sqlite_path(self) -> str:
        os.makedirs(self.data_folder, exist_ok=True)
        return os.path.join(self.data_folder, "landclaims.db")

    @property
    def _json_claims_path(self) -> str:
        return os.path.join(self.data_folder, "claims.json")

    @property
    def _config_path(self) -> str:
        os.makedirs(self.data_folder, exist_ok=True)
        return os.path.join(self.data_folder, "config.json")

    @property
    def _requests_db_path(self) -> str:
        os.makedirs(self.data_folder, exist_ok=True)
        return os.path.join(self.data_folder, "claim_requests.json")

    def _get_db_connection(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode enabled."""
        conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self):
        """Initialize SQLite database schema."""
        conn = self._get_db_connection()
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS claims (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    x1 INTEGER, y1 INTEGER, z1 INTEGER,
                    x2 INTEGER, y2 INTEGER, z2 INTEGER,
                    parent_claim TEXT,
                    is_child INTEGER DEFAULT 0,
                    flags_block_break INTEGER DEFAULT 1,
                    flags_block_place INTEGER DEFAULT 1,
                    flags_interact INTEGER DEFAULT 1,
                    flags_perimeter_particles INTEGER DEFAULT 1,
                    flags_kill_passive_mobs INTEGER DEFAULT 0,
                    flags_kill_hostile_mobs INTEGER DEFAULT 0,
                    flags_ignore_named_mobs INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS claim_owners (
                    claim_key TEXT,
                    uuid TEXT,
                    PRIMARY KEY (claim_key, uuid)
                );

                CREATE TABLE IF NOT EXISTS pending_names (
                    claim_key TEXT,
                    player_name TEXT,
                    PRIMARY KEY (claim_key, player_name)
                );

                CREATE TABLE IF NOT EXISTS player_mapping (
                    name_lower TEXT PRIMARY KEY,
                    uuid TEXT NOT NULL,
                    display_name TEXT,
                    last_seen REAL
                );

                CREATE TABLE IF NOT EXISTS claim_requests (
                    request_id TEXT PRIMARY KEY,
                    player_uuid TEXT,
                    player_name TEXT,
                    claim_name TEXT,
                    dimension TEXT,
                    x1 INTEGER, y1 INTEGER, z1 INTEGER,
                    x2 INTEGER, y2 INTEGER, z2 INTEGER,
                    timestamp REAL,
                    status TEXT DEFAULT 'pending'
                );
            ''')
            conn.commit()
            self.logger.info(f"[{self.name}] SQLite database initialized.")
        finally:
            conn.close()

    def _migrate_json_to_sqlite(self):
        """Migrate existing JSON data to SQLite on first run."""
        json_path = self._json_claims_path
        if not os.path.exists(json_path):
            return

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        if not raw:
            return

        conn = self._get_db_connection()
        try:
            # Check if claims already exist in DB
            cursor = conn.execute("SELECT COUNT(*) FROM claims")
            if cursor.fetchone()[0] > 0:
                self.logger.info(f"[{self.name}] Database already has claims, skipping JSON migration.")
                return

            migrated = 0
            for key, entry in raw.items():
                display_name = entry.get("name", key)
                flags = entry.get("flags", {})
                
                conn.execute('''
                    INSERT OR REPLACE INTO claims 
                    (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
                     flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
                     flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    self._normalize_claim_key(display_name),
                    display_name,
                    entry.get("dimension", "overworld"),
                    entry.get("x1", 0), entry.get("y1", -64), entry.get("z1", 0),
                    entry.get("x2", 0), entry.get("y2", 320), entry.get("z2", 0),
                    entry.get("parent_claim"),
                    1 if entry.get("is_child", False) else 0,
                    1 if flags.get("block_break", True) else 0,
                    1 if flags.get("block_place", True) else 0,
                    1 if flags.get("interact", True) else 0,
                    1 if flags.get("perimeter_particles", True) else 0,
                    1 if flags.get("kill_passive_mobs", False) else 0,
                    1 if flags.get("kill_hostile_mobs", False) else 0,
                    1 if flags.get("ignore_named_mobs", True) else 0
                ))

                # Migrate allowed UUIDs
                for uuid in entry.get("allowed_uuids", []):
                    conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)",
                                (self._normalize_claim_key(display_name), uuid))

                # Migrate pending names
                for pn in entry.get("pending_names", []):
                    conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)",
                                (self._normalize_claim_key(display_name), pn))

                migrated += 1

            conn.commit()
            self.logger.info(f"[{self.name}] Migrated {migrated} claims from JSON to SQLite.")

            # Rename JSON file as backup
            backup_path = json_path + ".migrated"
            os.rename(json_path, backup_path)
            self.logger.info(f"[{self.name}] JSON file backed up to {backup_path}")

        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to migrate JSON to SQLite: {e}")
        finally:
            conn.close()

    def _normalize_claim_key(self, name: str) -> str:
        return " ".join(name.split()).lower()

    def _reload_claims_from_disk(self):
        """Queue a dashboard-requested claim reload on the server thread."""
        try:
            self.server.scheduler.run_task(self, self._reload_claims_on_main_thread)
        except Exception as exc:
            self.logger.error(f"[{self.name}] Failed to schedule claim reload: {exc}")

    def _reload_claims_on_main_thread(self):
        """Reload the in-memory claim map without racing server event handlers."""
        try:
            old_count = len(self._claims)
            self._load_claims()
            new_count = len(self._claims)
            self.logger.info(f"[{self.name}] Hot-reloaded claims from database: {old_count} -> {new_count} claims")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to reload claims: {e}")

    def _reload_config_from_disk(self):
        """Queue a dashboard-requested configuration reload on the server thread."""
        try:
            self.server.scheduler.run_task(self, self._reload_config_on_main_thread)
        except Exception as exc:
            self.logger.error(f"[{self.name}] Failed to schedule config reload: {exc}")

    def _reload_config_on_main_thread(self):
        """Reload configuration without racing the periodic server tasks."""
        try:
            self._load_config()
            self.logger.info(f"[{self.name}] Hot-reloaded config from disk")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to reload config: {e}")

    def _load_claims(self):
        """Load claims from SQLite database into memory."""
        loaded_claims: Dict[str, Claim] = {}
        conn = self._get_db_connection()
        try:
            owners_by_claim: Dict[str, List[str]] = {}
            for owner_row in conn.execute("SELECT claim_key, uuid FROM claim_owners"):
                owners_by_claim.setdefault(owner_row["claim_key"], []).append(owner_row["uuid"])

            pending_by_claim: Dict[str, List[str]] = {}
            for pending_row in conn.execute("SELECT claim_key, player_name FROM pending_names"):
                pending_by_claim.setdefault(pending_row["claim_key"], []).append(pending_row["player_name"])

            for row in conn.execute("SELECT * FROM claims"):
                key = row["key"]
                flags = ClaimFlags(
                    block_break=bool(row["flags_block_break"]),
                    block_place=bool(row["flags_block_place"]),
                    interact=bool(row["flags_interact"]),
                    perimeter_particles=bool(row["flags_perimeter_particles"]),
                    kill_passive_mobs=bool(row["flags_kill_passive_mobs"]),
                    kill_hostile_mobs=bool(row["flags_kill_hostile_mobs"]),
                    ignore_named_mobs=bool(row["flags_ignore_named_mobs"])
                )
                
                claim = Claim(
                    name=row["name"],
                    dimension=row["dimension"],
                    x1=row["x1"], y1=row["y1"], z1=row["z1"],
                    x2=row["x2"], y2=row["y2"], z2=row["z2"],
                    allowed_uuids=owners_by_claim.get(key, []),
                    flags=flags,
                    parent_claim=row["parent_claim"],
                    is_child=bool(row["is_child"]),
                    pending_names=pending_by_claim.get(key, [])
                )
                loaded_claims[key] = claim
            self._claims = loaded_claims
        finally:
            conn.close()

    def _save_claims(self):
        """Save in-memory claims to SQLite database.
        
        Uses INSERT OR REPLACE to update only claims known to the plugin.
        Claims added via web dashboard are preserved since we don't delete them.
        """
        conn = self._get_db_connection()
        try:
            for c in self._claims.values():
                key = self._normalize_claim_key(c.name)
                
                # Use INSERT OR REPLACE to upsert claims
                conn.execute('''
                    INSERT OR REPLACE INTO claims 
                    (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
                     flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
                     flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    key, c.name, c.dimension,
                    c.x1, c.y1, c.z1, c.x2, c.y2, c.z2,
                    c.parent_claim, 1 if c.is_child else 0,
                    1 if c.flags.block_break else 0,
                    1 if c.flags.block_place else 0,
                    1 if c.flags.interact else 0,
                    1 if c.flags.perimeter_particles else 0,
                    1 if c.flags.kill_passive_mobs else 0,
                    1 if c.flags.kill_hostile_mobs else 0,
                    1 if c.flags.ignore_named_mobs else 0
                ))
                
                # Update owners for this claim only (delete + reinsert for this key)
                conn.execute("DELETE FROM claim_owners WHERE claim_key = ?", (key,))
                for uuid in set(c.allowed_uuids):
                    conn.execute("INSERT INTO claim_owners (claim_key, uuid) VALUES (?, ?)", (key, uuid))
                
                # Update pending names for this claim only
                conn.execute("DELETE FROM pending_names WHERE claim_key = ?", (key,))
                for pn in (c.pending_names or []):
                    conn.execute("INSERT INTO pending_names (claim_key, player_name) VALUES (?, ?)", (key, pn))
            
            conn.commit()
        except Exception as e:
            self.logger.error(f"Failed to save claims to SQLite: {e}")
        finally:
            conn.close()

    def _delete_claim_records(self, claim_key: str):
        """Delete one claim and its related rows in a single transaction."""
        conn = self._get_db_connection()
        try:
            conn.execute("DELETE FROM claim_owners WHERE claim_key = ?", (claim_key,))
            conn.execute("DELETE FROM pending_names WHERE claim_key = ?", (claim_key,))
            conn.execute("DELETE FROM claims WHERE key = ?", (claim_key,))
            conn.commit()
        finally:
            conn.close()

    def _load_config(self):
        """Loads plugin configuration from disk. Only overwrites defaults if file has actual values."""
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._notification_type = config.get("notification_type", self._notification_type)
                    if "dashboard_enabled" in config:
                        self._dashboard_enabled = self._coerce_bool(config["dashboard_enabled"], self._dashboard_enabled)
                    if "dashboard_port" in config:
                        self._dashboard_port = self._clamp_int(config["dashboard_port"], self._dashboard_port, 1, 65535)
                    for key, attribute in (
                        ("dashboard_host", "_dashboard_host"),
                        ("dashboard_public_url", "_dashboard_public_url"),
                        ("discord_client_id", "_discord_client_id"),
                        ("discord_client_secret", "_discord_client_secret"),
                        ("discord_redirect_uri", "_discord_redirect_uri"),
                    ):
                        if key in config:
                            setattr(self, attribute, config[key])
                    if "allowed_discord_ids" in config:
                        value = config["allowed_discord_ids"]
                        self._allowed_discord_ids = [str(item) for item in value] if isinstance(value, list) else []
                    if "master_password" in config:
                        self._master_password = config["master_password"]
                    if "mob_cleanup_enabled" in config:
                        self._mob_cleanup_enabled = self._coerce_bool(config["mob_cleanup_enabled"], True)
                    if "mob_cleanup_cancel_spawns" in config:
                        self._mob_cleanup_cancel_spawns = self._coerce_bool(config["mob_cleanup_cancel_spawns"], True)
                    self._mob_cleanup_interval_ticks = self._clamp_int(
                        config.get("mob_cleanup_interval_ticks", self._mob_cleanup_interval_ticks),
                        self._mob_cleanup_interval_ticks, 20, 1200
                    )
                    self._mob_cleanup_actors_per_tick = self._clamp_int(
                        config.get("mob_cleanup_actors_per_tick", self._mob_cleanup_actors_per_tick),
                        self._mob_cleanup_actors_per_tick, 10, 2000
                    )
                    self._mob_cleanup_max_removals_per_tick = self._clamp_int(
                        config.get("mob_cleanup_max_removals_per_tick", self._mob_cleanup_max_removals_per_tick),
                        self._mob_cleanup_max_removals_per_tick, 1, 200
                    )
                    self._mob_cleanup_max_actor_snapshot = self._clamp_int(
                        config.get("mob_cleanup_max_actor_snapshot", self._mob_cleanup_max_actor_snapshot),
                        self._mob_cleanup_max_actor_snapshot, 100, 100000
                    )
            else:
                # Create default config if it doesn't exist
                self._save_config()
        except Exception as e:
            self.logger.error(f"Failed to load config: {e}")

    def _save_config(self):
        """Save known settings while preserving future or dashboard-only keys."""
        try:
            config = {}
            if os.path.exists(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as config_file:
                    existing = json.load(config_file)
                    if isinstance(existing, dict):
                        config.update(existing)
            config.update({
                "notification_type": self._notification_type,
                "dashboard_enabled": self._dashboard_enabled,
                "dashboard_port": self._dashboard_port,
                "dashboard_host": self._dashboard_host,
                "dashboard_public_url": self._dashboard_public_url,
                "discord_client_id": self._discord_client_id,
                "discord_client_secret": self._discord_client_secret,
                "discord_redirect_uri": self._discord_redirect_uri,
                "allowed_discord_ids": self._allowed_discord_ids,
                "master_password": self._master_password,
                "mob_cleanup_enabled": self._mob_cleanup_enabled,
                "mob_cleanup_cancel_spawns": self._mob_cleanup_cancel_spawns,
                "mob_cleanup_interval_ticks": self._mob_cleanup_interval_ticks,
                "mob_cleanup_actors_per_tick": self._mob_cleanup_actors_per_tick,
                "mob_cleanup_max_removals_per_tick": self._mob_cleanup_max_removals_per_tick,
                "mob_cleanup_max_actor_snapshot": self._mob_cleanup_max_actor_snapshot,
            })
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save config: {e}")

    @staticmethod
    def _coerce_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "on", "1"}:
                return True
            if normalized in {"false", "no", "off", "0"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    @staticmethod
    def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @property
    def _player_mapping_path(self) -> str:
        os.makedirs(self.data_folder, exist_ok=True)
        return os.path.join(self.data_folder, "player_name_mapping.json")

    def _load_player_mapping(self):
        """Loads player name to UUID mapping from disk."""
        try:
            if os.path.exists(self._player_mapping_path):
                with open(self._player_mapping_path, "r", encoding="utf-8") as f:
                    self._player_name_mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._player_name_mapping = {}
        except Exception as e:
            self.logger.error(f"Failed to load player mapping: {e}")
            self._player_name_mapping = {}

    def _save_player_mapping(self):
        """Saves player name to UUID mapping to disk."""
        try:
            with open(self._player_mapping_path, "w", encoding="utf-8") as f:
                json.dump(self._player_name_mapping, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save player mapping: {e}")

    def _update_player_mapping(self, name_lower: str, player_uuid: str, display_name: str):
        """Update or add a player name mapping."""
        self._player_name_mapping[name_lower] = {
            "uuid": player_uuid,
            "display_name": display_name,
            "last_seen": time.time()
        }
        self._save_player_mapping()

    def _resolve_pending_name(self, name_lower: str, player_uuid: str):
        """Resolve pending player name to UUID in all claims."""
        modified = False
        for claim in self._claims.values():
            pending = claim.__dict__.get("pending_names", [])
            if not pending:
                continue
            
            for pn in pending:
                if pn.lower() == name_lower:
                    if player_uuid not in claim.allowed_uuids:
                        claim.allowed_uuids.append(player_uuid)
                        modified = True
                        self.logger.info(f"Resolved {pn} to {player_uuid} for claim {claim.name}")
        
        if modified:
            self._save_claims()

    # NOTE: _reload_claims_from_disk is defined earlier and uses the scheduler
    # to ensure the reload happens on the main server thread.

    def _start_web_dashboard(self):
        """Start the web dashboard server in a background thread."""
        if not self._dashboard_enabled:
            self.logger.info(f"[{self.name}] Web dashboard is disabled.")
            return
        
        try:
            # Import from the bundled web_dashboard package
            from endstone_landclaim.web_dashboard.app import configure, run_server
            
            # Debug log the config values
            self.logger.info(f"[{self.name}] OAuth Config - Client ID: {self._discord_client_id[:10] if self._discord_client_id else 'None'}...")
            self.logger.info(f"[{self.name}] Dashboard public URL: {self._dashboard_public_url}")
            
            # Configure the web dashboard with reload callback
            configure(
                data_folder=self.data_folder,
                public_url=self._dashboard_public_url,
                discord_client_id=self._discord_client_id,
                discord_client_secret=self._discord_client_secret,
                discord_redirect_uri=self._discord_redirect_uri,
                allowed_discord_ids=self._allowed_discord_ids,
                reload_callback=self._reload_claims_from_disk,
                config_reload_callback=self._reload_config_from_disk,
                master_password=self._master_password
            )
            
            # Start server in daemon thread
            def start_server():
                try:
                    run_server(host=self._dashboard_host, port=self._dashboard_port, debug=False)
                except Exception as e:
                    self.logger.error(f"Web dashboard server error: {e}")
            
            self._web_server_thread = threading.Thread(target=start_server, daemon=True)
            self._web_server_thread.start()
            
            self.logger.info(f"[{self.name}] Web dashboard started: {self._dashboard_public_url}")
            self.logger.info(f"[{self.name}] Dashboard backend listening on http://{self._dashboard_host}:{self._dashboard_port}")
            
        except ImportError as e:
            self.logger.warning(f"[{self.name}] Web dashboard not available: {e}")
            self.logger.warning(f"[{self.name}] Install Flask with: pip install flask flask-cors requests")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to start web dashboard: {e}")

    def _load_claim_requests(self):
        """Loads pending claim requests from disk."""
        try:
            with open(self._requests_db_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}

        self._claim_requests.clear()
        for request_id, entry in raw.items():
            request = ClaimRequest(
                request_id=request_id,
                player_uuid=entry["player_uuid"],
                player_name=entry["player_name"],
                claim_name=entry["claim_name"],
                dimension=entry["dimension"],
                x1=entry["x1"], y1=entry["y1"], z1=entry["z1"],
                x2=entry["x2"], y2=entry["y2"], z2=entry["z2"],
                timestamp=entry["timestamp"],
                status=entry.get("status", "pending")
            )
            self._claim_requests[request_id] = request

    def _save_claim_requests(self):
        """Saves pending claim requests to disk."""
        data = {
            req_id: {
                "request_id": req.request_id,
                "player_uuid": req.player_uuid,
                "player_name": req.player_name,
                "claim_name": req.claim_name,
                "dimension": req.dimension,
                "x1": req.x1, "y1": req.y1, "z1": req.z1,
                "x2": req.x2, "y2": req.y2, "z2": req.z2,
                "timestamp": req.timestamp,
                "status": req.status
            }
            for req_id, req in self._claim_requests.items()
        }
        try:
            with open(self._requests_db_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save claim requests to {self._requests_db_path}: {e}")

    # ------------------ Utilities ------------------
    def _player_uuid(self, player: Player) -> str:
        return str(player.unique_id)

    def _player_dimension_name(self, player: Player) -> str:
        # Player inherits from Mob (which inherits from Actor); Actor.dimension is a Dimension object
        # We persist the dimension's name as a lowercase string.
        try:
            return player.dimension.name.lower()
        except Exception:
            # Very defensive fallback
            return "overworld"

    def _send_claim_notification(self, player: Player, title: str, content: str):
        """Sends a notification to the player based on the configured notification type."""
        if self._notification_type == "toast":
            player.send_toast(title=f"§l{title}", content=f"§7{content}")
        elif self._notification_type == "tip":
            player.send_tip(f"§e{title} §7- {content}")
        elif self._notification_type == "chat":
            player.send_message(f"§e{title} §7- {content}")

    def _get_smallest_containing_claim(self, x: float, z: float, dim: str) -> Optional[Claim]:
        smallest = None
        smallest_area = None
        for claim in self._claims.values():
            if claim.contains(x, z, dim):
                area = claim.area()
                if smallest_area is None or area < smallest_area:
                    smallest, smallest_area = claim, area
        return smallest

    def _get_smallest_containing_claim_3d(self, x: float, y: float, z: float, dim: str) -> Optional[Claim]:
        return self._get_smallest_containing_claim_3d_from(
            self._claims.values(), x, y, z, dim
        )

    @staticmethod
    def _get_smallest_containing_claim_3d_from(claims, x: float, y: float, z: float, dim: str) -> Optional[Claim]:
        smallest = None
        smallest_volume = None
        for claim in claims:
            if claim.contains_3d(x, y, z, dim):
                volume = claim.volume()
                if smallest_volume is None or volume < smallest_volume:
                    smallest, smallest_volume = claim, volume
        return smallest

    def _is_protected_action(self, player: Player, claim: Claim) -> bool:
        return (not player.has_permission("adminlandclaims.bypass")) and (self._player_uuid(player) not in claim.allowed_uuids)

    def _get_parent_claims(self) -> List[Claim]:
        """Get all parent claims (non-child claims)."""
        return [c for c in self._claims.values() if not c.is_child]

    def _get_child_claims(self, parent_name: str) -> List[Claim]:
        """Get all child claims of a specific parent."""
        parent_key = self._normalize_claim_key(parent_name)
        return [c for c in self._claims.values() if c.parent_claim == parent_key]

    @staticmethod
    def _claim_is_inside_parent(child: Claim, parent: Claim) -> bool:
        """Return whether every child boundary is inside its selected parent."""
        if child.dimension != parent.dimension:
            return False
        child_bounds = child.bounds_xyz()
        parent_bounds = parent.bounds_xyz()
        return (
            parent_bounds[0] <= child_bounds[0] <= child_bounds[1] <= parent_bounds[1]
            and parent_bounds[2] <= child_bounds[2] <= child_bounds[3] <= parent_bounds[3]
            and parent_bounds[4] <= child_bounds[4] <= child_bounds[5] <= parent_bounds[5]
        )

    def _check_child_claim_proximity(self, new_claim: Claim) -> Tuple[bool, List[str], Optional[Tuple[int, int, int, int]]]:
        """
        Check if a child claim is too close to other child claims.
        Returns (is_too_close, conflicting_claims, suggested_coords)
        """
        if not new_claim.is_child:
            return False, [], None

        conflicting_claims = []
        for claim in self._claims.values():
            if (
                claim.is_child
                and self._normalize_claim_key(claim.name) != self._normalize_claim_key(new_claim.name)
                and claim.dimension == new_claim.dimension
            ):
                distance = new_claim.distance_to_claim(claim)
                if distance < 150:
                    conflicting_claims.append(claim.name)

        suggested_coords = None
        if conflicting_claims:
            # Calculate suggested coordinates 150 blocks away
            center_x, center_z = new_claim.center_xz()
            # Simple suggestion: move 150 blocks in a direction
            suggested_coords = (int(center_x + 150), int(center_z), int(center_x + 170), int(center_z + 20))

        return len(conflicting_claims) > 0, conflicting_claims, suggested_coords

    def _should_kill_mob(self, actor, claim: Claim) -> bool:
        """Return whether a non-player mob is covered by this claim's flags."""
        if not isinstance(actor, Mob) or isinstance(actor, Player):
            return False
        flags = claim.flags
        if not (flags.kill_passive_mobs or flags.kill_hostile_mobs):
            return False
        if flags.ignore_named_mobs and self._actor_has_custom_name(actor):
            return False
        if flags.kill_passive_mobs and flags.kill_hostile_mobs:
            return True
        mob_type = self._get_mob_type(actor)
        return (
            (flags.kill_hostile_mobs and self._is_hostile_mob(mob_type))
            or (flags.kill_passive_mobs and self._is_passive_mob(mob_type))
        )

    @staticmethod
    def _actor_has_custom_name(actor) -> bool:
        try:
            return bool(actor.name_tag and actor.name_tag.strip())
        except (AttributeError, TypeError):
            return False

    @staticmethod
    def _get_mob_type(actor) -> str:
        """Return Endstone's canonical actor identifier without its namespace."""
        try:
            return str(actor.type).strip().lower().split(":")[-1]
        except (AttributeError, TypeError, ValueError):
            return ""

    def _is_hostile_mob(self, mob_type: str) -> bool:
        """Checks if a mob type is hostile."""
        if not mob_type:
            return False

        hostile_mobs = {
            "blaze", "bogged", "breeze", "cave_spider", "creaking", "creeper",
            "drowned", "elder_guardian", "ender_dragon", "enderman", "endermite",
            "evocation_illager", "evoker", "ghast", "guardian", "hoglin", "husk",
            "magma_cube", "phantom", "piglin_brute", "pillager", "ravager", "shulker",
            "silverfish", "skeleton", "slime", "spider", "stray", "vex", "vindicator",
            "warden", "witch", "wither", "wither_skeleton", "zoglin", "zombie",
            "zombie_villager", "zombie_villager_v2",
        }
        return mob_type.strip().lower().split(":")[-1] in hostile_mobs

    def _is_passive_mob(self, mob_type: str) -> bool:
        """Checks if a mob type is passive."""
        if not mob_type:
            return False

        passive_mobs = {
            "allay", "armadillo", "axolotl", "bat", "bee", "camel", "cat", "chicken",
            "cow", "dolphin", "donkey", "fox", "frog", "glow_squid", "goat",
            "happy_ghast", "horse", "iron_golem", "llama", "mooshroom", "mule",
            "ocelot", "panda", "parrot", "pig", "piglin", "polar_bear", "pufferfish",
            "rabbit", "salmon", "sheep", "skeleton_horse", "sniffer", "snow_golem",
            "squid", "strider", "tadpole", "tropical_fish", "tropicalfish", "turtle",
            "villager", "villager_v2", "wandering_trader", "wolf", "zombie_horse",
            "zombie_pigman",
        }
        return mob_type.strip().lower().split(":")[-1] in passive_mobs

    def _claim_containing_actor(
        self, actor, claims_by_dimension: Optional[Dict[str, List[Claim]]] = None
    ) -> Optional[Claim]:
        if not isinstance(actor, Mob) or isinstance(actor, Player):
            return None
        try:
            location = actor.location
            dimension = actor.dimension.name.lower()
        except (AttributeError, RuntimeError):
            return None
        if claims_by_dimension is None:
            return self._get_smallest_containing_claim_3d(
                location.x, location.y, location.z, dimension
            )
        return self._get_smallest_containing_claim_3d_from(
            claims_by_dimension.get(dimension, ()),
            location.x,
            location.y,
            location.z,
            dimension,
        )

    # ------------------ Command Handler ------------------
    def on_command(self, sender: CommandSender, command: Command, args: List[str]) -> bool:
        if not isinstance(sender, Player):
            sender.send_message(f"{ColorFormat.RED}This command can only be run by a player.")
            return True

        cmd_name = command.name.lower()
        if cmd_name == "landclaim":
            self._open_main_menu(sender)
        elif cmd_name == "requestclaim":
            self._open_request_claim_form(sender)
        elif cmd_name == "lcadmin":
            self._handle_admin_command(sender, args)
        elif cmd_name == "lcdebug":
            self._handle_debug_command(sender, args)
        else:
            return False
        return True

    def _handle_admin_command(self, player: Player, args: List[str]):
        """Handles admin commands for plugin configuration."""
        if not player.has_permission("adminlandclaims.admin"):
            player.send_message(f"{ColorFormat.RED}You don't have permission to use admin commands.")
            return

        if not args:
            self._open_admin_menu(player)
            return

        if args[0].lower() == "notification":
            if len(args) < 2:
                player.send_message(f"{ColorFormat.YELLOW}Usage: /lcadmin notification <toast|tip|chat>")
                player.send_message(f"{ColorFormat.GRAY}Current: {self._notification_type}")
                return

            new_type = args[1].lower()
            if new_type in ["toast", "tip", "chat"]:
                self._notification_type = new_type
                self._save_config()
                player.send_message(f"{ColorFormat.GREEN}Notification type set to: {new_type}")
            else:
                player.send_message(f"{ColorFormat.RED}Invalid notification type. Use: toast, tip, or chat")
        else:
            player.send_message(f"{ColorFormat.RED}Unknown admin command. Use /lcadmin for help.")

    def _handle_debug_command(self, player: Player, args: List[str]):
        """Handles debug commands for testing particles and mob killing."""
        if not player.has_permission("adminlandclaims.admin"):
            player.send_message(f"{ColorFormat.RED}You don't have permission to use debug commands.")
            return

        if not args:
            player.send_message(f"{ColorFormat.YELLOW}Debug commands:")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug particles - Test particle spawning")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug claims - List claims with particle settings")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug mobs - Show mob killing settings for current location")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug killmobs - Manually trigger mob killing for current claim")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug forceparticles - Force show particles for current claim")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug testmob <mobtype> - Test mob classification")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug scanmobs - Scan and kill mobs in all claims")
            player.send_message(f"{ColorFormat.GRAY}/lcdebug countmobs - Count entities in current claim")
            return

        if args[0].lower() == "particles":
            # Test particle spawning at player location
            loc = player.location
            player.send_message(f"{ColorFormat.GREEN}Testing particles at your location...")

            # Test different particle types
            particles_to_test = [
                "minecraft:villager_happy",
                "minecraft:happy_villager",
                "minecraft:heart",
                "minecraft:redstone_dust",
                "minecraft:dust",
                "minecraft:barrier",
                "minecraft:angry_villager",
                "minecraft:shriek_particle"
            ]

            for i, particle in enumerate(particles_to_test):
                try:
                    player.spawn_particle(particle, loc.x + i, loc.y + 2, loc.z)
                    player.send_message(f"{ColorFormat.GREEN}✓ {particle}")
                except Exception as e:
                    player.send_message(f"{ColorFormat.RED}✗ {particle}: {e}")

            # Test proximity particles if in a claim
            dim = self._player_dimension_name(player)
            claim = self._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)
            if claim and claim.flags.perimeter_particles:
                player.send_message(f"{ColorFormat.YELLOW}Testing proximity particles for claim {claim.name}...")
                has_protections = (not claim.flags.block_break or not claim.flags.block_place or not claim.flags.interact)
                self._spawn_proximity_particles(player, claim, has_protections)

        elif args[0].lower() == "claims":
            # List claims with particle settings
            particle_claims = [c for c in self._claims.values() if c.flags.perimeter_particles]
            player.send_message(f"{ColorFormat.YELLOW}Claims with particles enabled: {len(particle_claims)}")
            for claim in particle_claims:
                player.send_message(f"{ColorFormat.GREEN}- {claim.name} in {claim.dimension}")

        elif args[0].lower() == "mobs":
            # Show mob killing settings for current location
            loc = player.location
            dim = self._player_dimension_name(player)
            claim = self._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)

            if claim:
                player.send_message(f"{ColorFormat.YELLOW}Current claim: {claim.name}")
                player.send_message(f"{ColorFormat.GRAY}Kill passive mobs: {claim.flags.kill_passive_mobs}")
                player.send_message(f"{ColorFormat.GRAY}Kill hostile mobs: {claim.flags.kill_hostile_mobs}")
                player.send_message(f"{ColorFormat.GRAY}Ignore named mobs: {claim.flags.ignore_named_mobs}")
            else:
                player.send_message(f"{ColorFormat.RED}You are not in a claim.")

        elif args[0].lower() == "killmobs":
            # Manually trigger mob killing for current claim
            loc = player.location
            dim = self._player_dimension_name(player)
            claim = self._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)

            if claim:
                player.send_message(f"{ColorFormat.YELLOW}Manually triggering mob killing for claim {claim.name}...")
                self._backup_mob_killing_single_claim(claim)
                player.send_message(f"{ColorFormat.GREEN}Mob killing triggered. Check server logs for details.")
            else:
                player.send_message(f"{ColorFormat.RED}You are not in a claim.")

        elif args[0].lower() == "forceparticles":
            # Force show particles for current claim
            loc = player.location
            dim = self._player_dimension_name(player)
            claim = self._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)

            if claim:
                player.send_message(f"{ColorFormat.YELLOW}Force showing particles for claim {claim.name}...")
                has_protections = (not claim.flags.block_break or not claim.flags.block_place or not claim.flags.interact)
                self._spawn_proximity_particles(player, claim, has_protections)
                player.send_message(f"{ColorFormat.GREEN}Particles triggered. Check if you can see them.")
            else:
                player.send_message(f"{ColorFormat.RED}You are not in a claim.")

        elif args[0].lower() == "testmob":
            # Test mob classification
            if len(args) < 2:
                player.send_message(f"{ColorFormat.RED}Usage: /lcdebug testmob <mobtype>")
                return

            mob_type = args[1].lower()
            is_hostile = self._is_hostile_mob(mob_type)
            is_passive = self._is_passive_mob(mob_type)

            player.send_message(f"{ColorFormat.YELLOW}Testing mob type: {mob_type}")
            player.send_message(f"{ColorFormat.GRAY}Is hostile: {is_hostile}")
            player.send_message(f"{ColorFormat.GRAY}Is passive: {is_passive}")

            # Test some variations
            variations = [mob_type, mob_type.replace('_', ''), mob_type.replace(' ', '')]
            for var in variations:
                if var != mob_type:
                    player.send_message(f"{ColorFormat.GRAY}Variation '{var}': hostile={self._is_hostile_mob(var)}, passive={self._is_passive_mob(var)}")

        elif args[0].lower() == "scanmobs":
            # Manually trigger mob scanning for all claims
            claims_with_mob_killing = [
                claim for claim in self._claims.values()
                if claim.flags.kill_passive_mobs or claim.flags.kill_hostile_mobs
            ]

            player.send_message(f"{ColorFormat.YELLOW}Manually triggering mob scanning...")
            player.send_message(f"{ColorFormat.GRAY}Claims with mob killing: {len(claims_with_mob_killing)}")

            for claim in claims_with_mob_killing:
                player.send_message(f"{ColorFormat.GRAY}- {claim.name}: hostile={claim.flags.kill_hostile_mobs}, passive={claim.flags.kill_passive_mobs}")

            self._scan_and_kill_mobs_in_claims()
            player.send_message(f"{ColorFormat.GREEN}Mob scanning triggered. Check server logs for details.")

        elif args[0].lower() == "countmobs":
            # Count entities in current claim
            loc = player.location
            dim = self._player_dimension_name(player)
            claim = self._get_smallest_containing_claim_3d(loc.x, loc.y, loc.z, dim)

            if claim:
                min_x, max_x, min_z, max_z = claim.bounds_xz()
                min_y, max_y = min(claim.y1, claim.y2), max(claim.y1, claim.y2)

                player.send_message(f"{ColorFormat.YELLOW}Counting entities in claim {claim.name}...")
                player.send_message(f"{ColorFormat.GRAY}Bounds: x={min_x}-{max_x}, y={min_y}-{max_y}, z={min_z}-{max_z}")

                try:
                    counts = self._count_loaded_mobs_in_claim(claim)
                    player.send_message(f"{ColorFormat.GREEN}Loaded mobs: {counts['all']}")
                    player.send_message(
                        f"{ColorFormat.GRAY}Hostile: {counts['hostile']}, "
                        f"passive/neutral: {counts['passive']}, named: {counts['named']}"
                    )
                    if counts["truncated"]:
                        player.send_message(
                            f"{ColorFormat.YELLOW}Count capped at {counts['inspected']} loaded actors for safety."
                        )
                except Exception as e:
                    player.send_message(f"{ColorFormat.RED}Error counting entities: {e}")
            else:
                player.send_message(f"{ColorFormat.RED}You are not in a claim.")
        else:
            player.send_message(f"{ColorFormat.RED}Unknown debug command. Use /lcdebug for help.")

    # ------------------ Forms UI ------------------
    def _open_main_menu(self, player: Player):
        form = ActionForm("Land Claim Manager")
        form.add_button("§aCreate New Claim", on_click=self._open_create_claim_form)
        form.add_button("Manage Existing Claims", on_click=self._open_claim_list_menu)
        form.add_button("Help: How to Create a Claim", on_click=self._open_help_menu)
        if player.has_permission("adminlandclaims.admin"):
            form.add_button("§6Admin Settings", on_click=self._open_admin_menu)
        player.send_form(form)

    def _open_request_claim_form(self, player: Player):
        """Opens form for players to request a new claim."""
        player_uuid = self._player_uuid(player)
        player_claims = [c for c in self._claims.values() if player_uuid in c.allowed_uuids]
        pending_requests = [
            request for request in self._claim_requests.values()
            if request.player_uuid == player_uuid and request.status == "pending"
        ]
        if len(player_claims) + len(pending_requests) >= 2:
            player.send_message(f"{ColorFormat.RED}You already have two active claims or pending requests.")
            return

        form = ModalForm("Request New Claim", submit_button="Submit Request")
        form.add_control(TextInput(
            label="Claim Name",
            placeholder="Enter a unique name for your claim",
            default_value=""
        ))
        form.add_control(TextInput(
            label="X1 Coordinate",
            placeholder="First corner X coordinate",
            default_value=str(int(player.location.x))
        ))
        form.add_control(TextInput(
            label="Y1 Coordinate",
            placeholder="First corner Y coordinate (bedrock to sky)",
            default_value="-64"
        ))
        form.add_control(TextInput(
            label="Z1 Coordinate",
            placeholder="First corner Z coordinate",
            default_value=str(int(player.location.z))
        ))
        form.add_control(TextInput(
            label="X2 Coordinate",
            placeholder="Second corner X coordinate",
            default_value=str(int(player.location.x) + 50)
        ))
        form.add_control(TextInput(
            label="Y2 Coordinate",
            placeholder="Second corner Y coordinate (bedrock to sky)",
            default_value="320"
        ))
        form.add_control(TextInput(
            label="Z2 Coordinate",
            placeholder="Second corner Z coordinate",
            default_value=str(int(player.location.z) + 50)
        ))

        def on_submit(p: Player, result: str):
            self._handle_request_claim_submission(p, result, player_uuid)

        form.on_submit = on_submit
        player.send_form(form)

    def _handle_request_claim_submission(self, player: Player, result, player_uuid: str):
        """Handles the submission of the request claim form."""
        import json as json_module

        try:
            # Handle both string (JSON) and list formats
            if isinstance(result, str):
                data = json_module.loads(result)
            else:
                data = result

            self.logger.debug(f"Request claim form data: {data}, type: {type(data)}")

            # Extract form values with proper stripping
            claim_name = str(data[0]).strip()  # TextInput for claim name
            x1 = int(str(data[1]).strip())  # TextInput for X1
            y1 = int(str(data[2]).strip())  # TextInput for Y1
            z1 = int(str(data[3]).strip())  # TextInput for Z1
            x2 = int(str(data[4]).strip())  # TextInput for X2
            y2 = int(str(data[5]).strip())  # TextInput for Y2
            z2 = int(str(data[6]).strip())  # TextInput for Z2

            if not claim_name:
                player.send_message(f"{ColorFormat.RED}Claim name cannot be empty.")
                return

            if x1 == x2 or y1 == y2 or z1 == z2:
                player.send_message(f"{ColorFormat.RED}Invalid coordinates. Claims must have volume.")
                return

            active_claims = sum(
                player_uuid in claim.allowed_uuids for claim in self._claims.values()
            )
            active_requests = sum(
                request.player_uuid == player_uuid and request.status == "pending"
                for request in self._claim_requests.values()
            )
            if active_claims + active_requests >= 2:
                player.send_message(f"{ColorFormat.RED}You already have two active claims or pending requests.")
                return

            # Check if claim name already exists
            normalized_name = self._normalize_claim_key(claim_name)
            if normalized_name in self._claims or any(
                request.status == "pending"
                and self._normalize_claim_key(request.claim_name) == normalized_name
                for request in self._claim_requests.values()
            ):
                player.send_message(f"{ColorFormat.RED}A claim with that name already exists.")
                return

            # Check proximity to other claims (150 blocks)
            temp_claim = Claim(
                name=claim_name,
                dimension=self._player_dimension_name(player),
                x1=x1, y1=y1, z1=z1,
                x2=x2, y2=y2, z2=z2,
                allowed_uuids=[],
                flags=ClaimFlags()
            )

            nearby_claims = []
            for other_claim in self._claims.values():
                distance = temp_claim.distance_to_claim(other_claim)
                if distance <= 150:
                    nearby_claims.append((other_claim.name, distance))

            if nearby_claims:
                msg = f"{ColorFormat.YELLOW}Warning: Your claim is within 150 blocks of:\n"
                for claim_name_nearby, dist in nearby_claims:
                    msg += f"  - {claim_name_nearby} ({int(dist)} blocks away)\n"
                player.send_message(msg)

            # Create request
            request_id = str(uuid.uuid4())
            request = ClaimRequest(
                request_id=request_id,
                player_uuid=player_uuid,
                player_name=player.name,
                claim_name=claim_name,
                dimension=self._player_dimension_name(player),
                x1=x1, y1=y1, z1=z1,
                x2=x2, y2=y2, z2=z2,
                timestamp=time.time(),
                status="pending"
            )
            self._claim_requests[request_id] = request
            self._save_claim_requests()
            player.send_toast(title="§lRequest Submitted", content="§7Your claim request has been submitted for review.")

        except ValueError as e:
            player.send_message(f"{ColorFormat.RED}Invalid coordinates. Make sure all coordinates are whole numbers (including negative numbers like -64).")
            self.logger.error(f"Coordinate parsing error: {e}, result: {result}")
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Error processing request: {str(e)}")
            self.logger.error(f"Error in claim request form: {e}")

    def _open_admin_menu(self, player: Player):
        pending_count = len([r for r in self._claim_requests.values() if r.status == "pending"])
        form = ActionForm("Admin Settings", f"Current notification type: {self._notification_type}\nPending requests: {pending_count}")
        form.add_button("§eChange Notification Type", on_click=self._open_notification_settings)
        form.add_button(f"§bReview Claim Requests ({pending_count})", on_click=self._open_claim_requests_list)
        form.add_button("§7Back to Main Menu", on_click=self._open_main_menu)
        player.send_form(form)

    def _open_notification_settings(self, player: Player):
        form = ActionForm("Notification Settings", "Choose how players are notified when entering/leaving claims:")
        form.add_button("§aToast Messages", on_click=lambda p: self._set_notification_type(p, "toast"))
        form.add_button("§bTip Messages", on_click=lambda p: self._set_notification_type(p, "tip"))
        form.add_button("§eChat Messages", on_click=lambda p: self._set_notification_type(p, "chat"))
        form.add_button("§7Back to Admin Menu", on_click=self._open_admin_menu)
        player.send_form(form)

    def _set_notification_type(self, player: Player, notification_type: str):
        self._notification_type = notification_type
        self._save_config()
        player.send_toast(
            title="§lSettings Updated",
            content=f"§7Notification type set to: §e{notification_type}"
        )
        self._open_admin_menu(player)

    def _open_claim_requests_list(self, player: Player):
        """Opens a list of pending claim requests for admin review."""
        pending_requests = [r for r in self._claim_requests.values() if r.status == "pending"]

        if not pending_requests:
            player.send_message(f"{ColorFormat.GREEN}No pending claim requests.")
            self._open_admin_menu(player)
            return

        form = ActionForm("Pending Claim Requests", f"Total: {len(pending_requests)}")
        for request in pending_requests:
            area = (abs(request.x2 - request.x1) + 1) * (abs(request.z2 - request.z1) + 1)
            button_text = f"§e{request.claim_name}\n§7By: {request.player_name}\n§7Area: {area} blocks"
            form.add_button(button_text, on_click=lambda p, r=request: self._open_request_review(p, r))

        form.add_button("§7Back to Admin Menu", on_click=self._open_admin_menu)
        player.send_form(form)

    def _open_request_review(self, player: Player, request: ClaimRequest):
        """Opens detailed review form for a specific claim request."""
        area = (abs(request.x2 - request.x1) + 1) * (abs(request.z2 - request.z1) + 1)
        volume = area * (abs(request.y2 - request.y1) + 1)

        content = (
            f"§lClaim Request Review\n\n"
            f"§ePlayer:§r {request.player_name}\n"
            f"§eClaim Name:§r {request.claim_name}\n"
            f"§eDimension:§r {request.dimension}\n"
            f"§eCoordinates:§r X({request.x1}, {request.x2}) Z({request.z1}, {request.z2})\n"
            f"§eArea:§r {area} blocks\n"
            f"§eVolume:§r {volume} blocks"
        )

        form = ActionForm("Review Request", content)
        form.add_button("§aApprove", on_click=lambda p: self._open_approval_settings(p, request))
        form.add_button("§cDeny", on_click=lambda p: self._deny_claim_request(p, request))
        form.add_button("§7Back to List", on_click=self._open_claim_requests_list)
        player.send_form(form)

    def _open_approval_settings(self, player: Player, request: ClaimRequest):
        """Opens form to configure protection settings for approved claim."""
        form = ModalForm(f"Approve: {request.claim_name}", submit_button="Approve Claim")
        form.add_control(Toggle(label="Block Breaking Protection", default_value=True))
        form.add_control(Toggle(label="Block Placement Protection", default_value=True))
        form.add_control(Toggle(label="Interaction Protection", default_value=True))
        form.add_control(Toggle(label="Perimeter Particles", default_value=True))
        form.add_control(Toggle(label="Kill Passive Mobs", default_value=False))
        form.add_control(Toggle(label="Kill Hostile Mobs", default_value=False))
        form.add_control(Toggle(label="Ignore Named Mobs", default_value=True))

        def on_submit(p: Player, result: str):
            self._handle_approval_settings_submission(p, request, result)

        form.on_submit = on_submit
        player.send_form(form)

    def _handle_approval_settings_submission(self, player: Player, request: ClaimRequest, result: str):
        """Handles the submission of the approval settings form."""
        import json as json_module

        try:
            # Handle both string (JSON) and list formats
            if isinstance(result, str):
                data = json_module.loads(result)
            else:
                data = result

            self.logger.debug(f"Approval settings form data: {data}, type: {type(data)}")

            # Extract toggle values (they should be booleans)
            protection_values = [bool(data[i]) for i in range(7)]
            self._approve_claim_request(player, request, protection_values)

        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Error processing approval: {str(e)}")
            self.logger.error(f"Error in approval settings form: {e}")

    def _approve_claim_request(self, player: Player, request: ClaimRequest, protection_values: List[bool]):
        """Approves a claim request and creates the claim."""
        try:
            claim_key = self._normalize_claim_key(request.claim_name)
            if claim_key in self._claims:
                player.send_message(
                    f"{ColorFormat.RED}A claim named '{request.claim_name}' now exists; rename or deny this request."
                )
                return
            # Create the claim from the request
            new_claim = Claim(
                name=request.claim_name,
                dimension=request.dimension,
                x1=request.x1, y1=request.y1, z1=request.z1,
                x2=request.x2, y2=request.y2, z2=request.z2,
                allowed_uuids=[request.player_uuid],
                flags=ClaimFlags(
                    block_break=not protection_values[0],
                    block_place=not protection_values[1],
                    interact=not protection_values[2],
                    perimeter_particles=protection_values[3],
                    kill_passive_mobs=protection_values[4],
                    kill_hostile_mobs=protection_values[5],
                    ignore_named_mobs=protection_values[6]
                )
            )

            # Add the claim
            self._claims[claim_key] = new_claim
            self._save_claims()

            # Update request status
            request.status = "approved"
            self._save_claim_requests()

            player.send_toast(title="§lClaim Approved", content=f"§7{request.claim_name} has been approved.")

            # Try to notify the player if they're online
            target_player = self.server.get_player(request.player_name)
            if target_player:
                target_player.send_toast(
                    title="§lClaim Approved",
                    content=f"§7Your claim '{request.claim_name}' has been approved!"
                )

            self._open_claim_requests_list(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Error approving claim: {e}")
            self.logger.error(f"Error approving claim request: {e}")

    def _deny_claim_request(self, player: Player, request: ClaimRequest):
        """Denies a claim request."""
        try:
            request.status = "denied"
            self._save_claim_requests()

            player.send_toast(title="§lRequest Denied", content=f"§7{request.claim_name} has been denied.")

            # Try to notify the player if they're online
            target_player = self.server.get_player(request.player_name)
            if target_player:
                target_player.send_message(
                    f"{ColorFormat.RED}Your claim request '{request.claim_name}' has been denied."
                )

            self._open_claim_requests_list(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Error denying claim: {e}")
            self.logger.error(f"Error denying claim request: {e}")

    def _open_help_menu(self, player: Player):
        content = (
            "§lHow to Use Land Claims§r\n\n"
            "§e• Creating Claims:§r\n"
            "Use the '§aCreate New Claim§r' button to open a form where you can:\n"
            "- Set claim name and type (parent/child)\n"
            "- Define X, Y, Z coordinates for 3D boundaries\n"
            "- Configure protection settings\n"
            "- Enable perimeter particles\n\n"
            "§e• Managing Claims:§r\n"
            "Use '§bManage Existing Claims§r' to:\n"
            "- Edit claim settings and coordinates\n"
            "- Add/remove player access\n"
            "- Toggle protection features\n"
            "- Delete claims\n\n"
            "§e• 3D Claims:§r\n"
            "Claims now support Y-coordinates for multi-level protection!\n\n"
            "§e• Child Claims:§r\n"
            "Create child claims within parent claims for specific areas.\n"
            "Child claims must be 150+ blocks apart from each other."
        )
        form = MessageForm(
            title="Land Claims Help",
            content=content,
            button1="Back",
            button2="Close",
            on_submit=lambda p, index: self._open_main_menu(p) if index == 0 else None,
        )
        player.send_form(form)

    def _open_claim_list_menu(self, player: Player):
        form = ActionForm("Manage Claims", "Select a claim to manage:")
        if not self._claims:
            form.content = "There are no claims to manage."
        form.add_button("§7Back to Main Menu", on_click=self._open_main_menu)

        for claim in sorted(self._claims.values(), key=lambda c: c.name.lower()):
            form.add_button(claim.name, on_click=lambda p, c=claim: self._open_manage_claim_menu(p, c))
        player.send_form(form)

    def _open_manage_claim_menu(self, player: Player, claim: Claim):
        claim_type = "Child Claim" if claim.is_child else "Parent Claim"
        volume_info = f"Volume: {claim.volume()} blocks"
        if claim.is_child and claim.parent_claim:
            parent_name = next((c.name for c in self._claims.values() if self._normalize_claim_key(c.name) == claim.parent_claim), "Unknown")
            volume_info += f" | Parent: {parent_name}"

        form = ActionForm(f"Managing: {claim.name}", f"{claim_type} | {volume_info}")

        form.add_button("§eEdit Claim Settings", on_click=lambda p: self._open_edit_claim_form(p, claim))
        form.add_button("§bManage Player Access", on_click=lambda p: self._open_player_access_form(p, claim))

        flags = claim.flags
        form.add_button(f"Particles: {'§aEnabled' if flags.perimeter_particles else '§7Disabled'}",
                        on_click=lambda p: self._toggle_flag(p, claim, "perimeter_particles"))

        # Add mob killing toggles
        form.add_button(f"Kill Passive Mobs: {'§aEnabled' if flags.kill_passive_mobs else '§7Disabled'}",
                        on_click=lambda p: self._toggle_flag(p, claim, "kill_passive_mobs"))
        form.add_button(f"Kill Hostile Mobs: {'§aEnabled' if flags.kill_hostile_mobs else '§7Disabled'}",
                        on_click=lambda p: self._toggle_flag(p, claim, "kill_hostile_mobs"))
        form.add_button(f"Ignore Named Mobs: {'§aEnabled' if flags.ignore_named_mobs else '§7Disabled'}",
                        on_click=lambda p: self._toggle_flag(p, claim, "ignore_named_mobs"))

        form.add_button("§cDelete Claim", on_click=lambda p: self._open_delete_confirm_form(p, claim))
        form.add_button("§7Back to Claim List", on_click=self._open_claim_list_menu)
        player.send_form(form)

    def _toggle_flag(self, player: Player, claim: Claim, flag_name: str):
        current_value = getattr(claim.flags, flag_name)
        setattr(claim.flags, flag_name, not current_value)
        self._save_claims()
        if flag_name in {"kill_passive_mobs", "kill_hostile_mobs", "ignore_named_mobs"}:
            self._request_mob_sweep()
        player.send_tip(f"{ColorFormat.GREEN}Updated {flag_name} for '{claim.name}'.")
        self._open_manage_claim_menu(player, claim)

    def _open_delete_confirm_form(self, player: Player, claim: Claim):
        form = ActionForm(f"§cConfirm Deletion: {claim.name}", "This action cannot be undone.")
        form.add_button("§cDelete Permanently", on_click=lambda p: self._confirm_delete_claim(p, claim))
        form.add_button("§7Cancel", on_click=lambda p: self._open_manage_claim_menu(p, claim))
        player.send_form(form)

    def _confirm_delete_claim(self, player: Player, claim: Claim):
        """Confirms and deletes a claim."""
        key = self._normalize_claim_key(claim.name)
        child_claims = self._get_child_claims(claim.name)
        if child_claims:
            player.send_message(
                f"{ColorFormat.RED}Delete or reassign the {len(child_claims)} child claim(s) first."
            )
            self._open_manage_claim_menu(player, claim)
            return
        self._delete_claim_records(key)
        self._claims.pop(key, None)
        player.send_toast(title="§lClaim Deleted", content=f"§7Removed §e{claim.name}§7.")
        self._open_claim_list_menu(player)

    def _open_create_claim_form(self, player: Player):
        """Opens the comprehensive claim creation form."""
        form = ModalForm("Create New Claim", submit_button="Create Claim")

        # Claim name input
        form.add_control(TextInput(
            label="Claim Name",
            placeholder="Enter a unique name for your claim",
            default_value=""
        ))

        # Claim type selection
        claim_type_dropdown = Dropdown(
            label="Claim Type",
            options=["Parent Claim", "Child Claim"],
            default_index=0
        )
        form.add_control(claim_type_dropdown)

        # Parent claim selection (for child claims)
        parent_claims = self._get_parent_claims()
        parent_options = ["None"] + [claim.name for claim in parent_claims]
        parent_dropdown = Dropdown(
            label="Parent Claim (for child claims only)",
            options=parent_options,
            default_index=0
        )
        form.add_control(parent_dropdown)

        # Get player's current location for defaults
        try:
            loc = player.location
            current_x, current_y, current_z = int(loc.x), int(loc.y), int(loc.z)
        except (AttributeError, TypeError):
            current_x, current_y, current_z = 0, 64, 0

        # Coordinate inputs - First Corner (x1, y1, z1)
        form.add_control(TextInput(
            label="X1 Coordinate (First Corner)",
            placeholder="X coordinate",
            default_value=str(current_x - 10)
        ))

        form.add_control(TextInput(
            label="Y1 Coordinate (First Corner)",
            placeholder="Y coordinate",
            default_value=str(current_y - 10)
        ))

        form.add_control(TextInput(
            label="Z1 Coordinate (First Corner)",
            placeholder="Z coordinate",
            default_value=str(current_z - 10)
        ))

        # Second Corner (x2, y2, z2)
        form.add_control(TextInput(
            label="X2 Coordinate (Second Corner)",
            placeholder="X coordinate",
            default_value=str(current_x + 10)
        ))

        form.add_control(TextInput(
            label="Y2 Coordinate (Second Corner)",
            placeholder="Y coordinate",
            default_value=str(current_y + 10)
        ))

        form.add_control(TextInput(
            label="Z2 Coordinate (Second Corner)",
            placeholder="Z coordinate",
            default_value=str(current_z + 10)
        ))

        # Protection toggles
        form.add_control(Toggle(
            label="Block Break Protection",
            default_value=True
        ))

        form.add_control(Toggle(
            label="Block Place Protection",
            default_value=True
        ))

        form.add_control(Toggle(
            label="Item Interaction Protection",
            default_value=True
        ))

        form.add_control(Toggle(
            label="Perimeter Particles",
            default_value=False
        ))

        # Mob killing toggles
        form.add_control(Toggle(
            label="Kill Passive Mobs",
            default_value=False
        ))

        form.add_control(Toggle(
            label="Kill Hostile Mobs",
            default_value=False
        ))

        form.add_control(Toggle(
            label="Ignore Named Mobs",
            default_value=True
        ))

        def on_submit(p: Player, result: str):
            self._handle_create_claim_submission(p, result)

        form.on_submit = on_submit
        player.send_form(form)

    def _handle_create_claim_submission(self, player: Player, result: str):
        """Handles the submission of the create claim form."""
        import json as json_module

        try:
            # Parse the form result
            data = json_module.loads(result)

            # Extract form values
            claim_name = data[0].strip()  # TextInput for claim name
            is_child = data[1] == 1  # Dropdown: 0=Parent, 1=Child
            parent_claim_index = data[2]  # Dropdown for parent claim
            x1 = int(data[3])  # TextInput for X1
            y1 = int(data[4])  # TextInput for Y1
            z1 = int(data[5])  # TextInput for Z1
            x2 = int(data[6])  # TextInput for X2
            y2 = int(data[7])  # TextInput for Y2
            z2 = int(data[8])  # TextInput for Z2
            block_break_protection = data[9]  # Toggle
            block_place_protection = data[10]  # Toggle
            interact_protection = data[11]  # Toggle
            perimeter_particles = data[12]  # Toggle
            kill_passive_mobs = data[13]  # Toggle
            kill_hostile_mobs = data[14]  # Toggle
            ignore_named_mobs = data[15]  # Toggle

            # Validate claim name
            if not claim_name:
                player.send_message(f"{ColorFormat.RED}Claim name cannot be empty.")
                return

            key = self._normalize_claim_key(claim_name)
            if key in self._claims:
                player.send_message(f"{ColorFormat.RED}A claim with that name already exists.")
                return

            # Validate coordinates
            if x1 == x2 or y1 == y2 or z1 == z2:
                player.send_message(f"{ColorFormat.RED}Invalid coordinates. Claims must have volume.")
                return

            # Get parent claim if this is a child
            parent_claim = None
            selected_parent = None
            if is_child:
                parent_claims = self._get_parent_claims()
                if parent_claim_index > 0 and parent_claim_index <= len(parent_claims):
                    selected_parent = parent_claims[parent_claim_index - 1]
                    parent_claim = self._normalize_claim_key(selected_parent.name)
                else:
                    player.send_message(f"{ColorFormat.RED}Invalid parent claim selection.")
                    return

            # Create the new claim
            new_claim = Claim(
                name=claim_name,
                dimension=self._player_dimension_name(player),
                x1=x1, y1=y1, z1=z1,
                x2=x2, y2=y2, z2=z2,
                allowed_uuids=[],
                flags=ClaimFlags(
                    block_break=not block_break_protection,  # Inverted: True means allowed
                    block_place=not block_place_protection,
                    interact=not interact_protection,
                    perimeter_particles=perimeter_particles,
                    kill_passive_mobs=kill_passive_mobs,
                    kill_hostile_mobs=kill_hostile_mobs,
                    ignore_named_mobs=ignore_named_mobs
                ),
                parent_claim=parent_claim,
                is_child=is_child
            )

            if is_child and not self._claim_is_inside_parent(new_claim, selected_parent):
                player.send_message(f"{ColorFormat.RED}A child claim must be fully inside its selected parent.")
                return

            # Check proximity for child claims
            if is_child:
                is_too_close, conflicting_claims, suggested_coords = self._check_child_claim_proximity(new_claim)
                if is_too_close:
                    self._show_proximity_warning(player, new_claim, conflicting_claims, suggested_coords)
                    return

            # Save the claim
            self._claims[key] = new_claim
            self._save_claims()

            player.send_toast(
                title="§lClaim Created",
                content=f"§7Created §e{claim_name}§7 successfully!"
            )

            # Return to main menu
            self._open_main_menu(player)

        except (ValueError, IndexError, json_module.JSONDecodeError) as e:
            player.send_message(f"{ColorFormat.RED}Invalid form data. Please try again.")
            self.logger.error(f"Error processing claim creation form: {e}")

    def _show_proximity_warning(self, player: Player, new_claim: Claim, conflicting_claims: List[str], suggested_coords: Optional[Tuple[int, int, int, int]]):
        """Shows a warning about child claim proximity with options to override or use suggested coordinates."""
        conflicting_names = ", ".join(conflicting_claims)
        content = (
            f"§cWarning: This child claim is within 150 blocks of existing child claims:\n"
            f"§e{conflicting_names}\n\n"
            f"§7You can either:\n"
            f"1. Override and create anyway\n"
            f"2. Use suggested coordinates (150+ blocks away)\n"
            f"3. Cancel and modify manually"
        )

        if suggested_coords:
            content += f"\n\n§bSuggested coordinates:\n§7X1: {suggested_coords[0]}, Z1: {suggested_coords[1]}\n§7X2: {suggested_coords[2]}, Z2: {suggested_coords[3]}"

        form = ActionForm("Proximity Warning", content)

        form.add_button("§cOverride and Create", on_click=lambda p: self._create_claim_override(p, new_claim))

        if suggested_coords:
            form.add_button("§aUse Suggested Coordinates", on_click=lambda p: self._create_claim_with_suggested_coords(p, new_claim, suggested_coords))

        form.add_button("§7Cancel", on_click=self._open_main_menu)

        player.send_form(form)

    def _create_claim_override(self, player: Player, claim: Claim):
        """Creates the claim despite proximity warnings."""
        key = self._normalize_claim_key(claim.name)
        self._claims[key] = claim
        self._save_claims()

        player.send_toast(
            title="§lClaim Created",
            content=f"§7Created §e{claim.name}§7 with override!"
        )
        self._open_main_menu(player)

    def _create_claim_with_suggested_coords(self, player: Player, claim: Claim, suggested_coords: Tuple[int, int, int, int]):
        """Creates the claim using suggested coordinates."""
        claim.x1, claim.z1, claim.x2, claim.z2 = suggested_coords

        key = self._normalize_claim_key(claim.name)
        self._claims[key] = claim
        self._save_claims()

        player.send_toast(
            title="§lClaim Created",
            content=f"§7Created §e{claim.name}§7 with suggested coordinates!"
        )
        self._open_main_menu(player)

    def _open_edit_claim_form(self, player: Player, claim: Claim):
        """Opens the comprehensive claim editing form."""
        form = ModalForm(f"Edit Claim: {claim.name}", submit_button="Save Changes")

        # Claim name input
        form.add_control(TextInput(
            label="Claim Name",
            placeholder="Enter claim name",
            default_value=claim.name
        ))

        # Claim type selection
        claim_type_dropdown = Dropdown(
            label="Claim Type",
            options=["Parent Claim", "Child Claim"],
            default_index=1 if claim.is_child else 0
        )
        form.add_control(claim_type_dropdown)

        # Parent claim selection (for child claims)
        parent_claims = self._get_parent_claims()
        parent_options = ["None"] + [c.name for c in parent_claims if c.name != claim.name]  # Exclude self

        # Find current parent index
        current_parent_index = 0
        if claim.is_child and claim.parent_claim:
            for i, parent in enumerate(parent_claims):
                if self._normalize_claim_key(parent.name) == claim.parent_claim:
                    current_parent_index = i + 1
                    break

        parent_dropdown = Dropdown(
            label="Parent Claim (for child claims only)",
            options=parent_options,
            default_index=current_parent_index
        )
        form.add_control(parent_dropdown)

        # Coordinate inputs - First Corner (x1, y1, z1)
        form.add_control(TextInput(
            label="X1 Coordinate (First Corner)",
            placeholder="X coordinate",
            default_value=str(claim.x1)
        ))

        form.add_control(TextInput(
            label="Y1 Coordinate (First Corner)",
            placeholder="Y coordinate",
            default_value=str(claim.y1)
        ))

        form.add_control(TextInput(
            label="Z1 Coordinate (First Corner)",
            placeholder="Z coordinate",
            default_value=str(claim.z1)
        ))

        # Second Corner (x2, y2, z2)
        form.add_control(TextInput(
            label="X2 Coordinate (Second Corner)",
            placeholder="X coordinate",
            default_value=str(claim.x2)
        ))

        form.add_control(TextInput(
            label="Y2 Coordinate (Second Corner)",
            placeholder="Y coordinate",
            default_value=str(claim.y2)
        ))

        form.add_control(TextInput(
            label="Z2 Coordinate (Second Corner)",
            placeholder="Z coordinate",
            default_value=str(claim.z2)
        ))

        # Protection toggles
        form.add_control(Toggle(
            label="Block Break Protection",
            default_value=not claim.flags.block_break  # Inverted for UI
        ))

        form.add_control(Toggle(
            label="Block Place Protection",
            default_value=not claim.flags.block_place
        ))

        form.add_control(Toggle(
            label="Item Interaction Protection",
            default_value=not claim.flags.interact
        ))

        form.add_control(Toggle(
            label="Perimeter Particles",
            default_value=claim.flags.perimeter_particles
        ))

        # Mob killing toggles
        form.add_control(Toggle(
            label="Kill Passive Mobs",
            default_value=claim.flags.kill_passive_mobs
        ))

        form.add_control(Toggle(
            label="Kill Hostile Mobs",
            default_value=claim.flags.kill_hostile_mobs
        ))

        form.add_control(Toggle(
            label="Ignore Named Mobs",
            default_value=claim.flags.ignore_named_mobs
        ))

        def on_submit(p: Player, result: str):
            self._handle_edit_claim_submission(p, claim, result)

        form.on_submit = on_submit
        player.send_form(form)

    def _handle_edit_claim_submission(self, player: Player, original_claim: Claim, result: str):
        """Handles the submission of the edit claim form."""
        import json as json_module

        try:
            # Parse the form result
            data = json_module.loads(result)

            # Extract form values
            new_name = data[0].strip()  # TextInput for claim name
            is_child = data[1] == 1  # Dropdown: 0=Parent, 1=Child
            parent_claim_index = data[2]  # Dropdown for parent claim
            x1 = int(data[3])  # TextInput for X1
            y1 = int(data[4])  # TextInput for Y1
            z1 = int(data[5])  # TextInput for Z1
            x2 = int(data[6])  # TextInput for X2
            y2 = int(data[7])  # TextInput for Y2
            z2 = int(data[8])  # TextInput for Z2
            block_break_protection = data[9]  # Toggle
            block_place_protection = data[10]  # Toggle
            interact_protection = data[11]  # Toggle
            perimeter_particles = data[12]  # Toggle
            kill_passive_mobs = data[13]  # Toggle
            kill_hostile_mobs = data[14]  # Toggle
            ignore_named_mobs = data[15]  # Toggle

            # Validate claim name
            if not new_name:
                player.send_message(f"{ColorFormat.RED}Claim name cannot be empty.")
                return

            # Check if name changed and if new name conflicts
            old_key = self._normalize_claim_key(original_claim.name)
            new_key = self._normalize_claim_key(new_name)

            if new_key != old_key and new_key in self._claims:
                player.send_message(f"{ColorFormat.RED}A claim with that name already exists.")
                return

            # Validate coordinates
            if x1 == x2 or y1 == y2 or z1 == z2:
                player.send_message(f"{ColorFormat.RED}Invalid coordinates. Claims must have volume.")
                return

            # Get parent claim if this is a child
            parent_claim = None
            selected_parent = None
            if is_child:
                parent_claims = self._get_parent_claims()
                # Filter out self from parent options
                available_parents = [c for c in parent_claims if c.name != original_claim.name]
                if parent_claim_index > 0 and parent_claim_index <= len(available_parents):
                    selected_parent = available_parents[parent_claim_index - 1]
                    parent_claim = self._normalize_claim_key(selected_parent.name)
                elif parent_claim_index > 0:
                    player.send_message(f"{ColorFormat.RED}Invalid parent claim selection.")
                    return

            # Create updated claim
            updated_claim = Claim(
                name=new_name,
                dimension=original_claim.dimension,  # Keep original dimension
                x1=x1, y1=y1, z1=z1,
                x2=x2, y2=y2, z2=z2,
                allowed_uuids=original_claim.allowed_uuids,  # Keep existing access list
                flags=ClaimFlags(
                    block_break=not block_break_protection,  # Inverted: True means allowed
                    block_place=not block_place_protection,
                    interact=not interact_protection,
                    perimeter_particles=perimeter_particles,
                    kill_passive_mobs=kill_passive_mobs,
                    kill_hostile_mobs=kill_hostile_mobs,
                    ignore_named_mobs=ignore_named_mobs
                ),
                parent_claim=parent_claim,
                is_child=is_child
            )

            if is_child and not self._claim_is_inside_parent(updated_claim, selected_parent):
                player.send_message(f"{ColorFormat.RED}A child claim must be fully inside its selected parent.")
                return

            # Check proximity for child claims (only if coordinates changed or became child)
            coords_changed = (x1 != original_claim.x1 or y1 != original_claim.y1 or z1 != original_claim.z1 or
                            x2 != original_claim.x2 or y2 != original_claim.y2 or z2 != original_claim.z2)
            became_child = is_child and not original_claim.is_child

            if is_child and (coords_changed or became_child):
                is_too_close, conflicting_claims, suggested_coords = self._check_child_claim_proximity(updated_claim)
                if is_too_close:
                    self._show_edit_proximity_warning(player, original_claim, updated_claim, conflicting_claims, suggested_coords)
                    return

            self._persist_edited_claim(original_claim, updated_claim)

            player.send_toast(
                title="§lClaim Updated",
                content=f"§7Updated §e{new_name}§7 successfully!"
            )

            # Return to manage menu for the updated claim
            self._open_manage_claim_menu(player, updated_claim)

        except (ValueError, IndexError, json_module.JSONDecodeError) as e:
            player.send_message(f"{ColorFormat.RED}Invalid form data. Please try again.")
            self.logger.error(f"Error processing claim edit form: {e}")

    def _show_edit_proximity_warning(self, player: Player, original_claim: Claim, updated_claim: Claim, conflicting_claims: List[str], suggested_coords: Optional[Tuple[int, int, int, int]]):
        """Shows a warning about child claim proximity during editing."""
        conflicting_names = ", ".join(conflicting_claims)
        content = (
            f"§cWarning: The updated claim would be within 150 blocks of existing child claims:\n"
            f"§e{conflicting_names}\n\n"
            f"§7You can either:\n"
            f"1. Override and save anyway\n"
            f"2. Use suggested coordinates (150+ blocks away)\n"
            f"3. Cancel and modify manually"
        )

        if suggested_coords:
            content += f"\n\n§bSuggested coordinates:\n§7X1: {suggested_coords[0]}, Z1: {suggested_coords[1]}\n§7X2: {suggested_coords[2]}, Z2: {suggested_coords[3]}"

        form = ActionForm("Edit Proximity Warning", content)

        form.add_button("§cOverride and Save", on_click=lambda p: self._save_edited_claim_override(p, original_claim, updated_claim))

        if suggested_coords:
            form.add_button("§aUse Suggested Coordinates", on_click=lambda p: self._save_edited_claim_with_suggested_coords(p, original_claim, updated_claim, suggested_coords))

        form.add_button("§7Cancel", on_click=lambda p: self._restore_and_return_to_manage(p, original_claim))

        player.send_form(form)

    def _save_edited_claim_override(self, player: Player, original_claim: Claim, updated_claim: Claim):
        """Saves the edited claim despite proximity warnings."""
        self._persist_edited_claim(original_claim, updated_claim)

        player.send_toast(
            title="§lClaim Updated",
            content=f"§7Updated §e{updated_claim.name}§7 with override!"
        )
        self._open_manage_claim_menu(player, updated_claim)

    def _save_edited_claim_with_suggested_coords(self, player: Player, original_claim: Claim, updated_claim: Claim, suggested_coords: Tuple[int, int, int, int]):
        """Saves the edited claim using suggested coordinates."""
        updated_claim.x1, updated_claim.z1, updated_claim.x2, updated_claim.z2 = suggested_coords

        self._persist_edited_claim(original_claim, updated_claim)

        player.send_toast(
            title="§lClaim Updated",
            content=f"§7Updated §e{updated_claim.name}§7 with suggested coordinates!"
        )
        self._open_manage_claim_menu(player, updated_claim)

    def _restore_and_return_to_manage(self, player: Player, original_claim: Claim):
        """Return to the unchanged original claim after cancelling an edit."""
        self._open_manage_claim_menu(player, original_claim)

    def _persist_edited_claim(self, original_claim: Claim, updated_claim: Claim):
        """Persist an edit and clean up renamed keys and child references."""
        old_key = self._normalize_claim_key(original_claim.name)
        new_key = self._normalize_claim_key(updated_claim.name)
        if new_key != old_key:
            self._delete_claim_records(old_key)
            self._claims.pop(old_key, None)
            for claim in self._claims.values():
                if claim.parent_claim == old_key:
                    claim.parent_claim = new_key
        self._claims[new_key] = updated_claim
        self._save_claims()

    def _open_player_access_form(self, player: Player, claim: Claim):
        """Opens the player access management form."""
        form = ActionForm(f"Player Access: {claim.name}", "Manage which players can access this claim")

        # Show current allowed players
        if claim.allowed_uuids:
            form.add_header("Current Access List:")
            for uuid in claim.allowed_uuids:
                # Try to get player name from server
                player_name = self._get_player_name_by_uuid(uuid)
                form.add_label(f"§a{player_name}")
        else:
            form.add_label("§7No players currently have access")

        form.add_button("§aAdd Player", on_click=lambda p: self._open_add_player_form(p, claim))

        if claim.allowed_uuids:
            form.add_button("§cRemove Player", on_click=lambda p: self._open_remove_player_form(p, claim))

        form.add_button("§7Back to Manage Claim", on_click=lambda p: self._open_manage_claim_menu(p, claim))
        player.send_form(form)

    def _get_player_name_by_uuid(self, uuid: str) -> str:
        """Gets a player name by UUID, with fallback to UUID if not found."""
        try:
            # Try to find online player first
            for online_player in self.server.online_players:
                if str(online_player.unique_id) == uuid:
                    return online_player.name

            # Check the player name mapping for offline players
            for name_lower, info in self._player_name_mapping.items():
                if info.get("uuid") == uuid:
                    # Return the display name if available, otherwise the lowercase name
                    return info.get("display_name", name_lower)

            # If not found anywhere, return truncated UUID
            return f"Unknown ({uuid[:8]}...)"
        except Exception:
            return f"Unknown ({uuid[:8]}...)"

    def _open_add_player_form(self, player: Player, claim: Claim):
        """Opens form to add a player to the claim access list."""
        form = ModalForm(f"Add Player to {claim.name}", submit_button="Add Player")

        form.add_control(TextInput(
            label="Player Name",
            placeholder="Enter the exact player name",
            default_value=""
        ))

        def on_submit(p: Player, result: str):
            self._handle_add_player_submission(p, claim, result)

        form.on_submit = on_submit
        player.send_form(form)

    def _handle_add_player_submission(self, player: Player, claim: Claim, result: str):
        """Handles adding a player to the claim access list."""
        import json as json_module

        try:
            data = json_module.loads(result)
            player_name = data[0].strip()

            if not player_name:
                player.send_message(f"{ColorFormat.RED}Player name cannot be empty.")
                return

            # Try to find the player by name
            target_uuid = None

            # Check online players first
            for online_player in self.server.online_players:
                if online_player.name.lower() == player_name.lower():
                    target_uuid = str(online_player.unique_id)
                    break

            if not target_uuid:
                player.send_message(f"{ColorFormat.RED}Player '{player_name}' not found. They must be online to be added.")
                return

            if target_uuid in claim.allowed_uuids:
                player.send_message(f"{ColorFormat.YELLOW}Player '{player_name}' already has access to this claim.")
                return

            # Add player to access list
            claim.allowed_uuids.append(target_uuid)
            self._save_claims()

            player.send_toast(
                title="§lPlayer Added",
                content=f"§7Added §e{player_name}§7 to claim access!"
            )

            self._open_player_access_form(player, claim)

        except (ValueError, IndexError, json_module.JSONDecodeError) as e:
            player.send_message(f"{ColorFormat.RED}Invalid form data. Please try again.")
            self.logger.error(f"Error processing add player form: {e}")

    def _open_remove_player_form(self, player: Player, claim: Claim):
        """Opens form to remove a player from the claim access list."""
        if not claim.allowed_uuids:
            player.send_message(f"{ColorFormat.RED}No players to remove from this claim.")
            return

        form = ActionForm(f"Remove Player from {claim.name}", "Select a player to remove:")

        for player_id in claim.allowed_uuids:
            player_name = self._get_player_name_by_uuid(player_id)
            form.add_button(f"§c{player_name}", on_click=lambda p, u=player_id: self._remove_player_from_claim(p, claim, u))

        form.add_button("§7Cancel", on_click=lambda p: self._open_player_access_form(p, claim))
        player.send_form(form)

    def _remove_player_from_claim(self, player: Player, claim: Claim, uuid_to_remove: str):
        """Removes a player from the claim access list."""
        if uuid_to_remove in claim.allowed_uuids:
            claim.allowed_uuids.remove(uuid_to_remove)
            self._save_claims()

            player_name = self._get_player_name_by_uuid(uuid_to_remove)
            player.send_toast(
                title="§lPlayer Removed",
                content=f"§7Removed §e{player_name}§7 from claim access!"
            )

        self._open_player_access_form(player, claim)

    # ------------------ Particle System ------------------
    def _start_particle_task(self):
        """Starts the periodic task to display claim boundary particles."""
        # Run every 1 second to show particles for claims with perimeter_particles enabled
        task = self.server.scheduler.run_task(
            self, self._display_claim_particles, delay=20, period=20  # 20 ticks = 1 second
        )
        self._particle_task_id = task.task_id

    def _display_claim_particles(self):
        """Displays particles around claim boundaries for claims with particles enabled."""
        try:
            particle_claims = [claim for claim in self._claims.values() if claim.flags.perimeter_particles]
            if particle_claims:
                self.logger.debug(f"Displaying particles for {len(particle_claims)} claims")

            for claim in particle_claims:
                # Show particles to nearby players
                self._show_claim_boundary_particles(claim)

        except Exception as e:
            self.logger.error(f"Error in particle display task: {e}")

    def _show_claim_boundary_particles(self, claim: Claim):
        """Shows actual particle effects around claim boundaries using Endstone's particle system."""
        # Check if any protections are active
        has_protections = (not claim.flags.block_break or
                          not claim.flags.block_place or
                          not claim.flags.interact)

        # Show particles to nearby players
        for player in self.server.online_players:
            if self._player_dimension_name(player) != claim.dimension:
                continue

            try:
                player_x, player_z = player.location.x, player.location.z

                # Check if player is within 5 blocks of any part of the claim boundary
                is_near = self._is_player_near_claim_boundary(player_x, player_z, claim, 5)

                if is_near:
                    # Spawn particles only in the area near the player
                    self._spawn_proximity_particles(player, claim, has_protections)

            except Exception as e:
                self.logger.error(f"Error showing particles to player {player.name}: {e}")

    def _is_player_near_claim_boundary(self, player_x: float, player_z: float, claim: Claim, distance: float) -> bool:
        """Check if player is within specified distance of claim boundary."""
        min_x, max_x, min_z, max_z = claim.bounds_xz()

        # Check if player is within the expanded boundary (claim + distance)
        expanded_min_x = min_x - distance
        expanded_max_x = max_x + distance
        expanded_min_z = min_z - distance
        expanded_max_z = max_z + distance

        # Player must be within the expanded area
        if not (expanded_min_x <= player_x <= expanded_max_x and expanded_min_z <= player_z <= expanded_max_z):
            return False

        # Inside a large claim, only render when near one of its four edges.
        if min_x <= player_x <= max_x and min_z <= player_z <= max_z:
            return min(
                player_x - min_x,
                max_x - player_x,
                player_z - min_z,
                max_z - player_z,
            ) <= distance

        # If player is outside claim but within distance, check if they're actually near boundary
        # Calculate distance to nearest boundary edge
        dx = max(0, max(min_x - player_x, player_x - max_x))
        dz = max(0, max(min_z - player_z, player_z - max_z))
        actual_distance = (dx * dx + dz * dz) ** 0.5

        return actual_distance <= distance

    def _spawn_proximity_particles(self, player: Player, claim: Claim, has_protections: bool):
        """Spawns particle effects around claim boundaries near the player's location."""
        min_x, max_x, min_z, max_z = claim.bounds_xz()
        min_y, max_y = min(claim.y1, claim.y2), max(claim.y1, claim.y2)

        player_x, player_z = player.location.x, player.location.z
        proximity_radius = 5  # Show particles within 5 blocks of player

        try:
            # Use player's Y level for visibility, clamped to claim bounds
            particle_y = max(min_y, min(max_y, int(player.location.y)))

            # Create particle positions only near the player
            positions = []

            # Calculate the area around the player to show particles
            search_min_x = max(min_x, int(player_x - proximity_radius))
            search_max_x = min(max_x, int(player_x + proximity_radius))
            search_min_z = max(min_z, int(player_z - proximity_radius))
            search_max_z = min(max_z, int(player_z + proximity_radius))

            # Add boundary particles only in the area near the player
            step = 2  # More frequent particles for better visibility

            # North and South boundaries (Z edges)
            for x in range(search_min_x, search_max_x + 1, step):
                # North boundary (min_z)
                if abs(x - player_x) <= proximity_radius and abs(min_z - player_z) <= proximity_radius:
                    positions.append((x, particle_y, min_z))
                # South boundary (max_z)
                if abs(x - player_x) <= proximity_radius and abs(max_z - player_z) <= proximity_radius:
                    positions.append((x, particle_y, max_z))

            # East and West boundaries (X edges)
            for z in range(search_min_z, search_max_z + 1, step):
                # West boundary (min_x)
                if abs(min_x - player_x) <= proximity_radius and abs(z - player_z) <= proximity_radius:
                    positions.append((min_x, particle_y, z))
                # East boundary (max_x)
                if abs(max_x - player_x) <= proximity_radius and abs(z - player_z) <= proximity_radius:
                    positions.append((max_x, particle_y, z))

            # Spawn particles at each position
            particles_spawned = 0
            for x, y, z in positions:
                try:
                    if has_protections:
                        # Enchantment particles for protected areas
                        success = False
                        # Try enchantment particles first, then fallbacks
                        for particle_name in ["minecraft:shriek_particle"]:
                            try:
                                player.spawn_particle(particle_name, x + 0.5, y + 1, z + 0.5)
                                success = True
                                break
                            except Exception:
                                continue

                        if not success:
                            # Final fallback for protected areas
                            try:
                                player.spawn_particle("minecraft:shriek_particle", x + 0.5, y + 1, z + 0.5)
                            except Exception:
                                pass  # Silent fail
                    else:
                        # Green/happy particles for unprotected areas
                        success = False
                        for particle_name in ["minecraft:villager_happy", "minecraft:heart", "minecraft:totem"]:
                            try:
                                player.spawn_particle(particle_name, x + 0.5, y + 1, z + 0.5)
                                success = True
                                break
                            except Exception:
                                continue

                        if not success:
                            # Final fallback for unprotected areas
                            try:
                                player.spawn_particle("minecraft:happy_villager", x + 0.5, y + 1, z + 0.5)
                            except Exception:
                                pass  # Silent fail

                    particles_spawned += 1

                except Exception as e:
                    self.logger.debug(f"Failed to spawn any particles at {x}, {y}, {z}: {e}")
                    continue

            # Removed particle count logging to reduce console spam

        except Exception as e:
            self.logger.debug(f"Error spawning particles: {e}")

    # ------------------ Mob Killing System ------------------
    def _start_mob_killer_task(self):
        """Start the one-tick budgeted loaded-actor cleanup worker."""
        task = self.server.scheduler.run_task(
            self, self._periodic_mob_management, delay=20, period=1
        )
        self._mob_killer_task_id = task.task_id

    def _periodic_mob_management(self):
        """Process a bounded number of loaded actors and removals each tick."""
        try:
            if not self._mob_cleanup_enabled:
                self._reset_mob_sweep()
                self._mob_sweep_wait_ticks = self._mob_cleanup_interval_ticks
                return

            if self._mob_sweep_actors is None:
                if self._mob_sweep_wait_ticks > 0:
                    self._mob_sweep_wait_ticks -= 1
                    return
                if not self._has_mob_control_claims():
                    self._mob_sweep_wait_ticks = self._mob_cleanup_interval_ticks
                    return
                self._begin_mob_sweep()
                if not self._mob_sweep_actors:
                    self._finish_mob_sweep()
                    return

            inspected = 0
            removed_this_tick = 0
            while (
                self._mob_sweep_actors is not None
                and self._mob_sweep_index < len(self._mob_sweep_actors)
                and inspected < self._mob_cleanup_actors_per_tick
                and removed_this_tick < self._mob_cleanup_max_removals_per_tick
            ):
                actor = self._mob_sweep_actors[self._mob_sweep_index]
                self._mob_sweep_index += 1
                inspected += 1
                if self._remove_actor_if_managed(actor):
                    removed_this_tick += 1
                    self._mob_sweep_removed += 1

            if self._mob_sweep_actors is not None and self._mob_sweep_index >= len(self._mob_sweep_actors):
                self._finish_mob_sweep()
        except Exception as exc:
            self.logger.error(f"Error in periodic mob management: {exc}")
            self._reset_mob_sweep()
            self._mob_sweep_wait_ticks = self._mob_cleanup_interval_ticks

    def _has_mob_control_claims(self) -> bool:
        return any(
            claim.flags.kill_passive_mobs or claim.flags.kill_hostile_mobs
            for claim in self._claims.values()
        )

    def _begin_mob_sweep(self):
        """Snapshot a fair, capped window of actors that are already loaded."""
        actors = self.server.level.actors
        total = len(actors)
        limit = self._mob_cleanup_max_actor_snapshot
        if total > limit:
            start = self._mob_snapshot_offset % total
            end = start + limit
            if end <= total:
                actors = actors[start:end]
            else:
                actors = actors[start:] + actors[:end - total]
            self._mob_snapshot_offset = end % total
            if not self._mob_snapshot_limit_warned:
                self.logger.warning(
                    f"Mob cleanup limited each sweep to {limit} of {total} loaded actors"
                )
                self._mob_snapshot_limit_warned = True
        else:
            self._mob_snapshot_offset = 0
            self._mob_snapshot_limit_warned = False
        claims_by_dimension: Dict[str, List[Claim]] = {}
        for claim in self._claims.values():
            claims_by_dimension.setdefault(claim.dimension, []).append(claim)
        self._mob_sweep_claims_by_dimension = claims_by_dimension
        self._mob_sweep_actors = actors
        self._mob_sweep_index = 0
        self._mob_sweep_removed = 0

    def _finish_mob_sweep(self):
        inspected = self._mob_sweep_index
        removed = self._mob_sweep_removed
        self._reset_mob_sweep()
        self._mob_sweep_wait_ticks = self._mob_cleanup_interval_ticks
        if removed:
            self.logger.debug(
                f"Mob cleanup inspected {inspected} loaded actors and removed {removed}"
            )

    def _reset_mob_sweep(self):
        self._mob_sweep_actors = None
        self._mob_sweep_index = 0
        self._mob_sweep_removed = 0
        self._mob_sweep_claims_by_dimension = {}

    def _remove_actor_if_managed(self, actor) -> bool:
        """Revalidate a loaded actor immediately before direct removal."""
        if not isinstance(actor, Mob) or isinstance(actor, Player):
            return False
        try:
            if not actor.is_valid or actor.is_dead:
                return False
            claim = self._claim_containing_actor(actor, self._mob_sweep_claims_by_dimension)
            if not claim or not self._should_kill_mob(actor, claim):
                return False
            actor.remove()
            return True
        except Exception as exc:
            self.logger.debug(f"Skipped stale mob during cleanup: {exc}")
            return False

    def _count_loaded_mobs_in_claim(self, claim: Claim) -> Dict[str, int]:
        """Count a capped loaded-actor snapshot without running volume selectors."""
        actors = self.server.level.actors
        total = len(actors)
        actors = actors[:self._mob_cleanup_max_actor_snapshot]
        counts = {
            "all": 0,
            "hostile": 0,
            "passive": 0,
            "named": 0,
            "inspected": len(actors),
            "truncated": int(total > len(actors)),
        }
        for actor in actors:
            if not isinstance(actor, Mob) or isinstance(actor, Player):
                continue
            try:
                location = actor.location
                dimension = actor.dimension.name.lower()
            except (AttributeError, RuntimeError):
                continue
            if not claim.contains_3d(location.x, location.y, location.z, dimension):
                continue
            counts["all"] += 1
            mob_type = self._get_mob_type(actor)
            counts["hostile"] += int(self._is_hostile_mob(mob_type))
            counts["passive"] += int(self._is_passive_mob(mob_type))
            counts["named"] += int(self._actor_has_custom_name(actor))
        return counts

    def _request_mob_sweep(self):
        """Ask the periodic worker to begin as soon as its current batch permits."""
        self._mob_sweep_wait_ticks = 0

    def _scan_and_kill_mobs_in_claims(self):
        """Compatibility entry point for the debug command's safe sweep request."""
        self._request_mob_sweep()

    def _kill_mobs_in_specific_claim(self, claim: Claim):
        """Compatibility wrapper; all removals use the bounded loaded-actor worker."""
        if claim.flags.kill_passive_mobs or claim.flags.kill_hostile_mobs:
            self._request_mob_sweep()

    def _backup_mob_killing(self):
        """Compatibility wrapper retained for existing integrations."""
        self._request_mob_sweep()

    def _backup_mob_killing_single_claim(self, claim: Claim):
        """Compatibility wrapper retained for the manual debug action."""
        self._kill_mobs_in_specific_claim(claim)





