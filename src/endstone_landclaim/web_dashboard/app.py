# Land Claim Web Dashboard - Flask Backend
# This server is launched by the Endstone plugin on enable

import json
import os
import shutil
import threading
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import secrets

from flask import Flask, jsonify, request, redirect, session, send_from_directory, url_for
from flask_cors import CORS
import requests

# Get the absolute path to the static folder (bundled with the package)
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_FOLDER = os.path.join(_PACKAGE_DIR, 'static')

app = Flask(__name__, static_folder=_STATIC_FOLDER, static_url_path='')
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True)

# ---------------------------------------------------------
# Configuration (set by plugin on startup)
# ---------------------------------------------------------
DATA_FOLDER = None  # Set by plugin
DISCORD_CLIENT_ID = None
DISCORD_CLIENT_SECRET = None
DISCORD_REDIRECT_URI = None
ALLOWED_DISCORD_IDS = set()  # Discord user IDs allowed to access
MASTER_PASSWORD = ""  # Master password for direct login (empty = disabled)
PLUGIN_RELOAD_CALLBACK = None  # Callback to reload claims in the plugin
PLUGIN_CONFIG_RELOAD_CALLBACK = None  # Callback to reload config in the plugin

# ---------------------------------------------------------
# Data Models (matching plugin)
# ---------------------------------------------------------
@dataclass
class ClaimFlags:
    block_break: bool = True
    block_place: bool = True
    interact: bool = True
    perimeter_particles: bool = True
    kill_passive_mobs: bool = False
    kill_hostile_mobs: bool = False
    ignore_named_mobs: bool = True

# ---------------------------------------------------------
# Database Paths and Connection
# ---------------------------------------------------------
_db_lock = threading.Lock()

def get_db_path():
    return os.path.join(DATA_FOLDER, "landclaims.db")

def get_requests_path():
    return os.path.join(DATA_FOLDER, "claim_requests.json")

def get_config_path():
    return os.path.join(DATA_FOLDER, "config.json")

def get_db_connection():
    """Get a database connection with WAL mode enabled."""
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def notify_plugin_reload():
    """Notify the plugin to reload claims from database."""
    if PLUGIN_RELOAD_CALLBACK:
        try:
            PLUGIN_RELOAD_CALLBACK()
        except Exception as e:
            print(f"[WebDashboard] Failed to notify plugin: {e}")

def notify_config_reload():
    """Notify the plugin to reload config from disk."""
    if PLUGIN_CONFIG_RELOAD_CALLBACK:
        try:
            PLUGIN_CONFIG_RELOAD_CALLBACK()
        except Exception as e:
            print(f"[WebDashboard] Failed to notify plugin config reload: {e}")

def normalize_claim_key(name: str) -> str:
    """Match plugin's key normalization"""
    return " ".join(name.split()).lower()

# JSON helpers for config file (still uses JSON)
def read_json_file(path):
    """Read JSON file safely"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def write_json_file(path, data):
    """Write JSON file"""
    with _db_lock:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

# ---------------------------------------------------------
# Player Name -> UUID Mapping (from SQLite)
# ---------------------------------------------------------
def get_name_mapping() -> Dict[str, Dict]:
    """Get player name to UUID mapping from SQLite"""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT name_lower, uuid, display_name, last_seen FROM player_mapping")
        result = {}
        for row in cursor.fetchall():
            result[row["name_lower"]] = {
                "uuid": row["uuid"],
                "display_name": row["display_name"],
                "last_seen": row["last_seen"]
            }
        return result
    finally:
        conn.close()

def resolve_name_to_uuid(name: str) -> Optional[str]:
    """Resolve player name to UUID if known"""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT uuid FROM player_mapping WHERE name_lower = ?", (name.lower(),))
        row = cursor.fetchone()
        return row["uuid"] if row else None
    finally:
        conn.close()

def get_allowed_names_for_claim(claim_key: str) -> List[str]:
    """Get list of player names for a claim's allowed_uuids"""
    conn = get_db_connection()
    try:
        # Get all owners for this claim
        cursor = conn.execute("SELECT uuid FROM claim_owners WHERE claim_key = ?", (claim_key,))
        uuids = [row["uuid"] for row in cursor.fetchall()]
        
        # Build uuid -> name mapping
        mapping = get_name_mapping()
        uuid_to_name = {v.get("uuid"): v.get("display_name", k) for k, v in mapping.items()}
        
        allowed_names = []
        for uuid in uuids:
            if uuid in uuid_to_name:
                allowed_names.append(uuid_to_name[uuid])
            else:
                allowed_names.append(f"unknown:{uuid[:8]}")
        
        return allowed_names
    finally:
        conn.close()

def get_pending_names_for_claim(claim_key: str) -> List[str]:
    """Get pending names for a claim"""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT player_name FROM pending_names WHERE claim_key = ?", (claim_key,))
        return [row["player_name"] for row in cursor.fetchall()]
    finally:
        conn.close()

# ---------------------------------------------------------
# Authentication (Discord OAuth2 + Master Password)
# ---------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        user = session['user']
        
        # Password-authenticated users are always authorized
        if user.get('auth_method') == 'password':
            return f(*args, **kwargs)
        
        # Discord OAuth users must be in the allowed IDs list
        if ALLOWED_DISCORD_IDS and user['id'] not in ALLOWED_DISCORD_IDS:
            return jsonify({"error": "Not authorized"}), 403
            
        return f(*args, **kwargs)
    return decorated

@app.route('/auth/login')
def login():
    """Redirect to Discord OAuth"""
    if not DISCORD_CLIENT_ID:
        return jsonify({"error": "OAuth not configured"}), 500
    
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI or url_for('callback', _external=True),
        'response_type': 'code',
        'scope': 'identify',
        'state': state
    }
    
    url = "https://discord.com/api/oauth2/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(url)

@app.route('/auth/password', methods=['POST'])
def password_login():
    """Authenticate via master password"""
    if not MASTER_PASSWORD:
        return jsonify({"error": "Password login is not configured"}), 400
    
    data = request.json or {}
    provided = data.get('password', '')
    
    if not provided:
        return jsonify({"error": "Password is required"}), 400
    
    if provided != MASTER_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    
    # Create a session for password-authenticated users
    session['user'] = {
        'id': 'master',
        'username': 'Administrator',
        'avatar': None,
        'auth_method': 'password'
    }
    
    return jsonify({"success": True, "user": session['user']})

@app.route('/auth/info')
def auth_info():
    """Return available authentication methods"""
    return jsonify({
        'discord': bool(DISCORD_CLIENT_ID),
        'password': bool(MASTER_PASSWORD)
    })

@app.route('/auth/callback')
def callback():
    """Handle Discord OAuth callback"""
    if request.args.get('state') != session.get('oauth_state'):
        return jsonify({"error": "Invalid state"}), 400
    
    code = request.args.get('code')
    if not code:
        return jsonify({"error": "No code provided"}), 400
    
    # Exchange code for token
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI or url_for('callback', _external=True)
    }
    
    resp = requests.post('https://discord.com/api/oauth2/token', data=data)
    if resp.status_code != 200:
        return jsonify({"error": "Failed to get token"}), 400
    
    token = resp.json()['access_token']
    
    # Get user info
    headers = {'Authorization': f'Bearer {token}'}
    user_resp = requests.get('https://discord.com/api/users/@me', headers=headers)
    if user_resp.status_code != 200:
        return jsonify({"error": "Failed to get user"}), 400
    
    user = user_resp.json()
    session['user'] = {
        'id': user['id'],
        'username': user['username'],
        'avatar': user.get('avatar')
    }
    
    return redirect('/')

@app.route('/auth/logout')
def logout():
    """Clear session"""
    session.clear()
    return redirect('/')

@app.route('/auth/me')
def me():
    """Get current user"""
    if 'user' in session:
        user = dict(session['user'])
        user['auth_method'] = user.get('auth_method', 'discord')
        return jsonify(user)
    return jsonify(None)

# ---------------------------------------------------------
# Static Files
# ---------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

# ---------------------------------------------------------
# Claims API (SQLite)
# ---------------------------------------------------------
@app.route('/api/claims', methods=['GET'])
@login_required
def list_claims():
    """List all claims from SQLite database"""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT * FROM claims")
        result = []
        for row in cursor.fetchall():
            key = row["key"]
            claim = {
                "name": row["name"],
                "dimension": row["dimension"],
                "x1": row["x1"], "y1": row["y1"], "z1": row["z1"],
                "x2": row["x2"], "y2": row["y2"], "z2": row["z2"],
                "parent_claim": row["parent_claim"],
                "is_child": bool(row["is_child"]),
                "flags": {
                    "block_break": bool(row["flags_block_break"]),
                    "block_place": bool(row["flags_block_place"]),
                    "interact": bool(row["flags_interact"]),
                    "perimeter_particles": bool(row["flags_perimeter_particles"]),
                    "kill_passive_mobs": bool(row["flags_kill_passive_mobs"]),
                    "kill_hostile_mobs": bool(row["flags_kill_hostile_mobs"]),
                    "ignore_named_mobs": bool(row["flags_ignore_named_mobs"])
                },
                "allowed_names": get_allowed_names_for_claim(key),
                "pending_names": get_pending_names_for_claim(key),
                "_key": key
            }
            result.append(claim)
    finally:
        conn.close()
    
    # Apply filters
    dimension = request.args.get('dimension')
    if dimension:
        result = [c for c in result if c.get('dimension') == dimension]
    
    search = request.args.get('search', '').lower()
    if search:
        result = [c for c in result if search in c.get('name', '').lower()]
    
    return jsonify(result)

@app.route('/api/claims', methods=['POST'])
@login_required
def create_claim():
    """Create a new claim in SQLite database"""
    data = request.json
    
    if not data.get('name'):
        return jsonify({"error": "Name is required"}), 400
    
    key = normalize_claim_key(data['name'])
    conn = get_db_connection()
    
    try:
        # Check if claim exists
        cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (key,))
        if cursor.fetchone():
            return jsonify({"error": "Claim already exists"}), 409
        
        flags = data.get('flags', {})
        
        # Insert claim
        conn.execute('''
            INSERT INTO claims 
            (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
             flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
             flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            key,
            data['name'],
            data.get('dimension', 'overworld'),
            int(data.get('x1', 0)), int(data.get('y1', -64)), int(data.get('z1', 0)),
            int(data.get('x2', 0)), int(data.get('y2', 320)), int(data.get('z2', 0)),
            data.get('parent_claim'),
            1 if data.get('is_child', False) else 0,
            1 if flags.get('block_break', True) else 0,
            1 if flags.get('block_place', True) else 0,
            1 if flags.get('interact', True) else 0,
            1 if flags.get('perimeter_particles', True) else 0,
            1 if flags.get('kill_passive_mobs', False) else 0,
            1 if flags.get('kill_hostile_mobs', False) else 0,
            1 if flags.get('ignore_named_mobs', True) else 0
        ))
        
        # Add owners (resolve names to UUIDs where possible)
        mapping = get_name_mapping()
        for name in data.get('allowed_names', []):
            name_lower = name.lower()
            if name_lower in mapping:
                uuid = mapping[name_lower].get('uuid')
                if uuid:
                    conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)", (key, uuid))
            else:
                # Add as pending name
                conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)", (key, name))
        
        conn.commit()
        notify_plugin_reload()
        return jsonify({"success": True, "key": key}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/claims/<name>', methods=['GET'])
@login_required
def get_claim(name):
    """Get a single claim from SQLite"""
    key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT * FROM claims WHERE key = ?", (key,))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({"error": "Claim not found"}), 404
        
        claim = {
            "name": row["name"],
            "dimension": row["dimension"],
            "x1": row["x1"], "y1": row["y1"], "z1": row["z1"],
            "x2": row["x2"], "y2": row["y2"], "z2": row["z2"],
            "parent_claim": row["parent_claim"],
            "is_child": bool(row["is_child"]),
            "flags": {
                "block_break": bool(row["flags_block_break"]),
                "block_place": bool(row["flags_block_place"]),
                "interact": bool(row["flags_interact"]),
                "perimeter_particles": bool(row["flags_perimeter_particles"]),
                "kill_passive_mobs": bool(row["flags_kill_passive_mobs"]),
                "kill_hostile_mobs": bool(row["flags_kill_hostile_mobs"]),
                "ignore_named_mobs": bool(row["flags_ignore_named_mobs"])
            },
            "allowed_names": get_allowed_names_for_claim(key),
            "pending_names": get_pending_names_for_claim(key),
            "_key": key
        }
        return jsonify(claim)
    finally:
        conn.close()

@app.route('/api/claims/<name>', methods=['PUT'])
@login_required
def update_claim(name):
    """Update an existing claim in SQLite"""
    old_key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT * FROM claims WHERE key = ?", (old_key,))
        old_row = cursor.fetchone()
        if not old_row:
            return jsonify({"error": "Claim not found"}), 404
        
        data = request.json
        flags = data.get('flags', {})
        
        # Check if name is being changed
        new_name = data.get('name', old_row['name'])
        new_key = normalize_claim_key(new_name)
        
        if new_key != old_key:
            # Name changed - need to delete old and insert new
            # First check if new key already exists
            cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (new_key,))
            if cursor.fetchone():
                return jsonify({"error": f"A claim with name '{new_name}' already exists"}), 409
            
            # Delete old claim and related data
            conn.execute("DELETE FROM claims WHERE key = ?", (old_key,))
            
            # Insert with new key
            conn.execute('''
                INSERT INTO claims 
                (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
                 flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
                 flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                new_key, new_name,
                data.get('dimension', old_row['dimension']),
                data.get('x1', old_row['x1']), data.get('y1', old_row['y1']), data.get('z1', old_row['z1']),
                data.get('x2', old_row['x2']), data.get('y2', old_row['y2']), data.get('z2', old_row['z2']),
                data.get('parent_claim', old_row['parent_claim']),
                1 if data.get('is_child', old_row['is_child']) else 0,
                1 if flags.get('block_break', old_row['flags_block_break']) else 0,
                1 if flags.get('block_place', old_row['flags_block_place']) else 0,
                1 if flags.get('interact', old_row['flags_interact']) else 0,
                1 if flags.get('perimeter_particles', old_row['flags_perimeter_particles']) else 0,
                1 if flags.get('kill_passive_mobs', old_row['flags_kill_passive_mobs']) else 0,
                1 if flags.get('kill_hostile_mobs', old_row['flags_kill_hostile_mobs']) else 0,
                1 if flags.get('ignore_named_mobs', old_row['flags_ignore_named_mobs']) else 0
            ))
            
            # Migrate owners to new key
            conn.execute("UPDATE claim_owners SET claim_key = ? WHERE claim_key = ?", (new_key, old_key))
            conn.execute("UPDATE pending_names SET claim_key = ? WHERE claim_key = ?", (new_key, old_key))
        else:
            # No name change - just update fields
            conn.execute('''
                UPDATE claims SET
                    name = ?,
                    dimension = COALESCE(?, dimension),
                    x1 = COALESCE(?, x1), y1 = COALESCE(?, y1), z1 = COALESCE(?, z1),
                    x2 = COALESCE(?, x2), y2 = COALESCE(?, y2), z2 = COALESCE(?, z2),
                    parent_claim = ?,
                    is_child = ?,
                    flags_block_break = ?, flags_block_place = ?, flags_interact = ?,
                    flags_perimeter_particles = ?, flags_kill_passive_mobs = ?,
                    flags_kill_hostile_mobs = ?, flags_ignore_named_mobs = ?
                WHERE key = ?
            ''', (
                new_name,
                data.get('dimension'),
                data.get('x1'), data.get('y1'), data.get('z1'),
                data.get('x2'), data.get('y2'), data.get('z2'),
                data.get('parent_claim'),
                1 if data.get('is_child', False) else 0,
                1 if flags.get('block_break', True) else 0,
                1 if flags.get('block_place', True) else 0,
                1 if flags.get('interact', True) else 0,
                1 if flags.get('perimeter_particles', True) else 0,
                1 if flags.get('kill_passive_mobs', False) else 0,
                1 if flags.get('kill_hostile_mobs', False) else 0,
                1 if flags.get('ignore_named_mobs', True) else 0,
                old_key
            ))
        
        # Update owners if provided
        if 'allowed_names' in data:
            target_key = new_key if new_key != old_key else old_key
            conn.execute("DELETE FROM claim_owners WHERE claim_key = ?", (target_key,))
            conn.execute("DELETE FROM pending_names WHERE claim_key = ?", (target_key,))
            
            mapping = get_name_mapping()
            for pname in data['allowed_names']:
                pname_lower = pname.lower()
                if pname_lower in mapping:
                    uuid = mapping[pname_lower].get('uuid')
                    if uuid:
                        conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)", (target_key, uuid))
                else:
                    conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)", (target_key, pname))
        
        conn.commit()
        notify_plugin_reload()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/claims/<name>', methods=['DELETE'])
@login_required
def delete_claim(name):
    """Delete a claim from SQLite"""
    key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (key,))
        if not cursor.fetchone():
            return jsonify({"error": "Claim not found"}), 404
        
        conn.execute("DELETE FROM claims WHERE key = ?", (key,))
        conn.execute("DELETE FROM claim_owners WHERE claim_key = ?", (key,))
        conn.execute("DELETE FROM pending_names WHERE claim_key = ?", (key,))
        conn.commit()
        
        notify_plugin_reload()
        return jsonify({"success": True})
    finally:
        conn.close()

# ---------------------------------------------------------
# Claim Owners API (SQLite)
# ---------------------------------------------------------
@app.route('/api/claims/<name>/owners', methods=['GET'])
@login_required
def list_owners(name):
    """List owners for a claim from SQLite"""
    key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (key,))
        if not cursor.fetchone():
            return jsonify({"error": "Claim not found"}), 404
        
        resolved = get_allowed_names_for_claim(key)
        pending = get_pending_names_for_claim(key)
        
        return jsonify({
            "resolved": resolved,
            "pending": [n for n in pending if n.lower() not in [x.lower() for x in resolved]]
        })
    finally:
        conn.close()

@app.route('/api/claims/<name>/owners', methods=['POST'])
@login_required
def add_owner(name):
    """Add owner(s) by player name to SQLite"""
    key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (key,))
        if not cursor.fetchone():
            return jsonify({"error": "Claim not found"}), 404
        
        data = request.json
        player_names = data.get('names', [])
        if isinstance(player_names, str):
            player_names = [player_names]
        
        mapping = get_name_mapping()
        
        for pn in player_names:
            pn_lower = pn.lower()
            
            if pn_lower in mapping:
                # If we know the UUID, add it as owner
                uuid = mapping[pn_lower].get('uuid')
                if uuid:
                    conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)", (key, uuid))
            else:
                # Add as pending name
                conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)", (key, pn))
        
        conn.commit()
        notify_plugin_reload()
        return jsonify({"success": True})
    finally:
        conn.close()

@app.route('/api/claims/<name>/owners/<player_name>', methods=['DELETE'])
@login_required
def remove_owner(name, player_name):
    """Remove owner by player name from SQLite"""
    key = normalize_claim_key(name)
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT key FROM claims WHERE key = ?", (key,))
        if not cursor.fetchone():
            return jsonify({"error": "Claim not found"}), 404
        
        pn_lower = player_name.lower()
        
        # Remove from pending names
        conn.execute("DELETE FROM pending_names WHERE claim_key = ? AND LOWER(player_name) = ?", (key, pn_lower))
        
        # Remove UUID if we have the mapping
        mapping = get_name_mapping()
        if pn_lower in mapping:
            uuid = mapping[pn_lower].get('uuid')
            if uuid:
                conn.execute("DELETE FROM claim_owners WHERE claim_key = ? AND uuid = ?", (key, uuid))
        
        conn.commit()
        notify_plugin_reload()
        return jsonify({"success": True})
    finally:
        conn.close()

# ---------------------------------------------------------
# Claim Requests API
# ---------------------------------------------------------
@app.route('/api/requests', methods=['GET'])
@login_required
def list_requests():
    """List pending claim requests"""
    reqs = read_json_file(get_requests_path())
    
    # Filter by status
    status = request.args.get('status', 'pending')
    if status != 'all':
        reqs = {k: v for k, v in reqs.items() if v.get('status') == status}
    
    return jsonify(list(reqs.values()))

@app.route('/api/requests/<request_id>', methods=['PUT'])
@login_required
def update_request(request_id):
    """Approve or deny a claim request"""
    reqs = read_json_file(get_requests_path())
    
    if request_id not in reqs:
        return jsonify({"error": "Request not found"}), 404
    
    data = request.json
    action = data.get('action')  # 'approve' or 'deny'
    
    if action == 'approve':
        req = reqs[request_id]
        req['status'] = 'approved'
        
        # Create the claim in SQLite
        key = normalize_claim_key(req['claim_name'])
        flags = data.get('flags', {})
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO claims 
                (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
                 flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
                 flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                key, req['claim_name'], req['dimension'],
                req['x1'], req['y1'], req['z1'],
                req['x2'], req['y2'], req['z2'],
                None, 0,  # parent_claim, is_child
                1 if flags.get('block_break', False) else 0,
                1 if flags.get('block_place', False) else 0,
                1 if flags.get('interact', False) else 0,
                1 if flags.get('perimeter_particles', True) else 0,
                1 if flags.get('kill_passive_mobs', False) else 0,
                1 if flags.get('kill_hostile_mobs', False) else 0,
                1 if flags.get('ignore_named_mobs', True) else 0
            ))
            
            # Add the requester as owner
            conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)", 
                        (key, req['player_uuid']))
            conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)",
                        (key, req['player_name']))
            
            conn.commit()
        finally:
            conn.close()
        
        notify_plugin_reload()
        
    elif action == 'deny':
        reqs[request_id]['status'] = 'denied'
    else:
        return jsonify({"error": "Invalid action"}), 400
    
    write_json_file(get_requests_path(), reqs)
    return jsonify(reqs[request_id])

# ---------------------------------------------------------
# Bulk Import/Export API (SQLite)
# ---------------------------------------------------------
@app.route('/api/bulk/export', methods=['GET'])
@login_required
def export_claims():
    """Export all claims from SQLite as JSON"""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT * FROM claims")
        claims = []
        for row in cursor.fetchall():
            key = row["key"]
            claim = {
                "name": row["name"],
                "dimension": row["dimension"],
                "x1": row["x1"], "y1": row["y1"], "z1": row["z1"],
                "x2": row["x2"], "y2": row["y2"], "z2": row["z2"],
                "parent_claim": row["parent_claim"],
                "is_child": bool(row["is_child"]),
                "flags": {
                    "block_break": bool(row["flags_block_break"]),
                    "block_place": bool(row["flags_block_place"]),
                    "interact": bool(row["flags_interact"]),
                    "perimeter_particles": bool(row["flags_perimeter_particles"]),
                    "kill_passive_mobs": bool(row["flags_kill_passive_mobs"]),
                    "kill_hostile_mobs": bool(row["flags_kill_hostile_mobs"]),
                    "ignore_named_mobs": bool(row["flags_ignore_named_mobs"])
                },
                "allowed_names": get_allowed_names_for_claim(key),
                "_key": key
            }
            claims.append(claim)
        
        return jsonify({
            "exported_at": datetime.utcnow().isoformat(),
            "claims": claims
        })
    finally:
        conn.close()

@app.route('/api/bulk/import', methods=['POST'])
@login_required
def import_claims():
    """Import claims from JSON or CSV into SQLite"""
    mode = request.args.get('mode', 'merge')  # 'merge' or 'replace'
    
    if request.content_type == 'application/json':
        data = request.json
        import_claims_list = data.get('claims', [])
    else:
        # Handle CSV upload
        file = request.files.get('file')
        if not file:
            return jsonify({"error": "No file provided"}), 400
        
        import_claims_list = parse_csv_claims(file)
    
    conn = get_db_connection()
    try:
        # Get existing claim keys for merge mode
        existing_keys = set()
        if mode == 'merge':
            cursor = conn.execute("SELECT key FROM claims")
            existing_keys = {row["key"] for row in cursor.fetchall()}
        elif mode == 'replace':
            conn.execute("DELETE FROM claims")
            conn.execute("DELETE FROM claim_owners")
            conn.execute("DELETE FROM pending_names")
        
        imported = 0
        skipped = 0
        errors = []
        
        mapping = get_name_mapping()
        
        for claim_data in import_claims_list:
            try:
                name = claim_data.get('name')
                if not name:
                    errors.append("Claim missing name")
                    continue
                
                key = normalize_claim_key(name)
                
                if mode == 'merge' and key in existing_keys:
                    skipped += 1
                    continue
                
                # Build claim
                flags = claim_data.get('flags', {})
                if isinstance(flags, bool):  # Simple "protected" flag
                    protected = flags
                    flags = {
                        "block_break": not protected,
                        "block_place": not protected,
                        "interact": not protected,
                    }
                
                # Insert claim
                conn.execute('''
                    INSERT OR REPLACE INTO claims 
                    (key, name, dimension, x1, y1, z1, x2, y2, z2, parent_claim, is_child,
                     flags_block_break, flags_block_place, flags_interact, flags_perimeter_particles,
                     flags_kill_passive_mobs, flags_kill_hostile_mobs, flags_ignore_named_mobs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    key, name,
                    claim_data.get('dimension', 'overworld'),
                    int(claim_data.get('x1', 0)), int(claim_data.get('y1', -64)), int(claim_data.get('z1', 0)),
                    int(claim_data.get('x2', 0)), int(claim_data.get('y2', 320)), int(claim_data.get('z2', 0)),
                    claim_data.get('parent_claim'),
                    1 if claim_data.get('is_child', False) else 0,
                    1 if flags.get('block_break', True) else 0,
                    1 if flags.get('block_place', True) else 0,
                    1 if flags.get('interact', True) else 0,
                    1 if flags.get('perimeter_particles', True) else 0,
                    1 if flags.get('kill_passive_mobs', False) else 0,
                    1 if flags.get('kill_hostile_mobs', False) else 0,
                    1 if flags.get('ignore_named_mobs', True) else 0
                ))
                
                # Add owners
                pending_names = claim_data.get('owners', claim_data.get('allowed_names', []))
                for pn in pending_names:
                    pn_lower = pn.lower()
                    if pn_lower in mapping:
                        uuid = mapping[pn_lower].get('uuid')
                        if uuid:
                            conn.execute("INSERT OR IGNORE INTO claim_owners (claim_key, uuid) VALUES (?, ?)", (key, uuid))
                    else:
                        conn.execute("INSERT OR IGNORE INTO pending_names (claim_key, player_name) VALUES (?, ?)", (key, pn))
                
                imported += 1
                
            except Exception as e:
                errors.append(f"Error importing {claim_data.get('name', 'unknown')}: {str(e)}")
        
        conn.commit()
        notify_plugin_reload()
        
        return jsonify({
            "imported": imported,
            "skipped": skipped,
            "errors": errors
        })
    finally:
        conn.close()

def parse_csv_claims(file):
    """Parse CSV file into claims list"""
    import csv
    import io
    
    content = file.read().decode('utf-8')
    reader = csv.DictReader(io.StringIO(content))
    
    claims = []
    for row in reader:
        # Parse owners from comma-separated string
        owners = []
        if row.get('owners'):
            owners = [o.strip() for o in row['owners'].split(',')]
        
        # Parse protected flag
        protected = row.get('protected', '').lower() in ('true', '1', 'yes')
        
        claims.append({
            "name": row.get('name'),
            "dimension": row.get('dimension', 'overworld'),
            "x1": row.get('x1', 0),
            "y1": row.get('y1', -64),
            "z1": row.get('z1', 0),
            "x2": row.get('x2', 0),
            "y2": row.get('y2', 320),
            "z2": row.get('z2', 0),
            "owners": owners,
            "flags": not protected  # Inverted: protected=True means flags=False
        })
    
    return claims

# ---------------------------------------------------------
# Config API
# ---------------------------------------------------------
@app.route('/api/config', methods=['GET'])
@login_required
def get_config():
    """Get plugin configuration"""
    return jsonify(read_json_file(get_config_path()))

@app.route('/api/config', methods=['PUT'])
@login_required
def update_config():
    """Update plugin configuration"""
    data = request.json
    config = read_json_file(get_config_path())
    config.update(data)
    write_json_file(get_config_path(), config)
    notify_config_reload()
    return jsonify(config)

# ---------------------------------------------------------
# Name Mapping API (for debugging)
# ---------------------------------------------------------
@app.route('/api/player-mapping', methods=['GET'])
@login_required
def get_player_mapping():
    """Get all player name -> UUID mappings"""
    return jsonify(get_name_mapping())

# ---------------------------------------------------------
# Server Startup / Shutdown
# ---------------------------------------------------------
_server = None  # Reference to the Werkzeug server for clean shutdown

def configure(data_folder, discord_client_id=None, discord_client_secret=None, 
              discord_redirect_uri=None, allowed_discord_ids=None, reload_callback=None,
              config_reload_callback=None, master_password=""):
    """Configure the web dashboard (called by plugin)"""
    global DATA_FOLDER, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET
    global DISCORD_REDIRECT_URI, ALLOWED_DISCORD_IDS, PLUGIN_RELOAD_CALLBACK, PLUGIN_CONFIG_RELOAD_CALLBACK
    global MASTER_PASSWORD
    
    DATA_FOLDER = data_folder
    DISCORD_CLIENT_ID = discord_client_id
    DISCORD_CLIENT_SECRET = discord_client_secret
    DISCORD_REDIRECT_URI = discord_redirect_uri
    PLUGIN_RELOAD_CALLBACK = reload_callback
    PLUGIN_CONFIG_RELOAD_CALLBACK = config_reload_callback
    MASTER_PASSWORD = master_password or ""
    
    if allowed_discord_ids:
        ALLOWED_DISCORD_IDS = set(str(i) for i in allowed_discord_ids)
    
    print(f"[WebDashboard] Configured with data_folder: {DATA_FOLDER}")
    print(f"[WebDashboard] Database path: {get_db_path()}")
    print(f"[WebDashboard] Auth methods: Discord={'YES' if discord_client_id else 'NO'}, Password={'YES' if MASTER_PASSWORD else 'NO'}")
    print(f"[WebDashboard] Reload callback: {'SET' if reload_callback else 'NOT SET'}")
    print(f"[WebDashboard] Config reload callback: {'SET' if config_reload_callback else 'NOT SET'}")

def run_server(host='0.0.0.0', port=5000, debug=False):
    """Start the Flask server using a stoppable Werkzeug server."""
    global _server
    from werkzeug.serving import make_server
    _server = make_server(host, port, app, threaded=True)
    print(f"[WebDashboard] Server listening on {host}:{port}")
    _server.serve_forever()

def stop_server():
    """Cleanly shut down the Flask server so the port is released."""
    global _server, PLUGIN_RELOAD_CALLBACK, PLUGIN_CONFIG_RELOAD_CALLBACK
    if _server is not None:
        print("[WebDashboard] Shutting down server...")
        _server.shutdown()
        _server = None
    # Clear callbacks to avoid stale references to old plugin instance
    PLUGIN_RELOAD_CALLBACK = None
    PLUGIN_CONFIG_RELOAD_CALLBACK = None

if __name__ == '__main__':
    # For testing outside of plugin
    DATA_FOLDER = os.path.dirname(os.path.abspath(__file__))
    run_server(debug=True)
