from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
import sqlite3
import time
import math
import os

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── CONFIG ────────────────────────────────────────────────────────────────────
BBOX = {"lamin": 48.5, "lamax": 55.5, "lomin": 13.5, "lomax": 24.5}
OPEN_SKY_STATES  = "https://opensky-network.org/api/states/all"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

CLIENT_ID     = os.getenv("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

FETCH_INTERVAL = 45      # seconds between API calls (~1920 req/day = 3840 credits, fits 4000 limit)
GRID_SIZE_KM   = 10      # cell side in km
DB_PATH        = "/app/data/stats.db"

# ── TOKEN MANAGER ─────────────────────────────────────────────────────────────
class TokenManager:
    def __init__(self):
        self.token = None
        self.expires_at = None

    def get_token(self):
        if self.token and self.expires_at and datetime.now() < self.expires_at:
            return self.token
        return self._refresh()

    def _refresh(self):
#        print("AUTH ATTEMPT:", CLIENT_ID, CLIENT_SECRET[:6], "...")
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=10
        )
        if r.status_code != 200:
            print("TOKEN ERROR:", r.status_code, r.text[:200])
            raise Exception("Token fetch failed")
        data = r.json()
        self.token = data["access_token"]
        expires_in = data.get("expires_in", 1800)
        self.expires_at = datetime.now() + timedelta(seconds=expires_in - 30)
        print("TOKEN REFRESHED")
        return self.token

    def headers(self):
        return {"Authorization": f"Bearer {self.get_token()}"}

tokens = TokenManager()

# ── GRID ──────────────────────────────────────────────────────────────────────
def latlon_to_cell(lat, lon):
    """Convert lat/lon to a 20x20 km grid cell id."""
    lat_km = lat * 111
    lon_km = lon * 111 * math.cos(math.radians(lat))
    x = int(lon_km // GRID_SIZE_KM)
    y = int(lat_km // GRID_SIZE_KM)
    return f"{x}_{y}"

def cell_to_bounds(cell_id):
    """Return (lat_min, lat_max, lon_min, lon_max) for a cell id."""
    x, y = map(int, cell_id.split("_"))
    ref_lat = 52.0  # central Poland for lon correction
    cos_lat = math.cos(math.radians(ref_lat))

    lat_min = (y * GRID_SIZE_KM) / 111
    lat_max = ((y + 1) * GRID_SIZE_KM) / 111
    lon_min = (x * GRID_SIZE_KM) / (111 * cos_lat)
    lon_max = ((x + 1) * GRID_SIZE_KM) / (111 * cos_lat)
    return lat_min, lat_max, lon_min, lon_max

# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Raw sightings — one row per plane per fetch
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sightings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        INTEGER NOT NULL,
            icao24    TEXT NOT NULL,
            callsign  TEXT,
            cell_id   TEXT NOT NULL,
            lat       REAL,
            lon       REAL,
            altitude  REAL,
            velocity  REAL
        )
    """)

    # Aggregated stats per cell (updated in memory, flushed periodically)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cell_stats (
            cell_id       TEXT PRIMARY KEY,
            sighting_count INTEGER DEFAULT 0,
            time_seconds   INTEGER DEFAULT 0,
            last_updated   INTEGER
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_cell ON sightings(cell_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_icao ON sightings(icao24)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_ts   ON sightings(ts)")

    con.commit()
    con.close()
    print("DB READY:", DB_PATH)

def flush_stats_to_db(cell_counts, cell_time):
    """Write in-memory stats to DB."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = int(time.time())
    for cell_id, count in cell_counts.items():
        t = cell_time.get(cell_id, 0)
        cur.execute("""
            INSERT INTO cell_stats (cell_id, sighting_count, time_seconds, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cell_id) DO UPDATE SET
                sighting_count = sighting_count + excluded.sighting_count,
                time_seconds   = time_seconds   + excluded.time_seconds,
                last_updated   = excluded.last_updated
        """, (cell_id, count, t, now))
    con.commit()
    con.close()

def log_sightings(planes, ts):
    """Insert raw sightings for this fetch."""
    if not planes:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executemany(
        "INSERT INTO sightings (ts, icao24, callsign, cell_id, lat, lon, altitude, velocity) VALUES (?,?,?,?,?,?,?,?)",
        [(ts, p["id"], p["callsign"], p["cell"], p["lat"], p["lon"], p["altitude"], p["velocity"]) for p in planes]
    )
    con.commit()
    con.close()

# ── IN-MEMORY ACCUMULATORS ────────────────────────────────────────────────────
cell_counts   = {}   # cell_id → sighting count (since last flush)
cell_time     = {}   # cell_id → estimated seconds (since last flush)
prev_seen     = {}   # icao24  → (cell_id, timestamp) for time estimation
last_flush    = time.time()
FLUSH_INTERVAL = 300  # flush to DB every 5 minutes

# ── CACHE ─────────────────────────────────────────────────────────────────────
cache_planes = []
last_fetch   = 0

# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_planes():
    global cache_planes, last_fetch, last_flush

    now = time.time()
    if now - last_fetch < FETCH_INTERVAL:
        return cache_planes

    last_fetch = now  # lock immediately to prevent double-fetch

    try:
        r = requests.get(
            OPEN_SKY_STATES,
            params=BBOX,
            headers=tokens.headers(),
            timeout=10
        )

        remaining = r.headers.get("X-Rate-Limit-Remaining", "?")
        print(f"API STATUS: {r.status_code} | Credits remaining: {remaining}")

        if r.status_code == 401:
            print("TOKEN EXPIRED — will refresh next cycle")
            tokens.token = None
            return cache_planes

        if r.status_code == 429:
            retry_after = r.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            print(f"RATE LIMITED — retry after {retry_after}s")
            return cache_planes

        if r.status_code != 200:
            return cache_planes

        if not r.text.strip():
            print("API EMPTY")
            return cache_planes

        data = r.json()

    except Exception as e:
        print("API error:", e)
        return cache_planes

    states = data.get("states", [])
    ts = int(now)
    result = []

    for s in states:
        if not s:
            continue

        lat       = s[6]
        lon       = s[5]
        velocity  = s[9]
        on_ground = s[8]

        if lat is None or lon is None:
            continue
        if on_ground:
            continue
        if velocity is not None and velocity < 30:
            continue

        icao24  = s[0]
        cell_id = latlon_to_cell(lat, lon)

        # ── COUNT ──
        cell_counts[cell_id] = cell_counts.get(cell_id, 0) + 1

        # ── TIME ESTIMATION ──
        # If same plane was in same cell last fetch, credit FETCH_INTERVAL seconds
        prev = prev_seen.get(icao24)
        if prev and prev[0] == cell_id:
            elapsed = min(ts - prev[1], FETCH_INTERVAL * 2)  # cap to avoid gaps inflating time
            cell_time[cell_id] = cell_time.get(cell_id, 0) + elapsed

        prev_seen[icao24] = (cell_id, ts)

        result.append({
            "id":       icao24,
            "callsign": (s[1] or "").strip(),
            "lat":      lat,
            "lon":      lon,
            "track":    s[10] or 0,
            "velocity": velocity,
            "altitude": s[13],
            "country":  s[2],
            "cell":     cell_id
        })

    # ── LOG + FLUSH ──
    log_sightings(result, ts)

    if now - last_flush >= FLUSH_INTERVAL:
        flush_stats_to_db(cell_counts, cell_time)
        cell_counts.clear()
        cell_time.clear()
        last_flush = now
        print("STATS FLUSHED TO DB")

    cache_planes = result
    print(f"FETCHED: {len(result)} planes")
    return result

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stats")
def get_stats():
    lat = float(request.args.get("lat"))
    lon = float(request.args.get("lon"))
    cell_id = latlon_to_cell(lat, lon)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT sighting_count, time_seconds FROM cell_stats WHERE cell_id = ?", (cell_id,))
    row = cur.fetchone()
    con.close()

    # Add unflushed in-memory counts
    mem_count = cell_counts.get(cell_id, 0)
    mem_time  = cell_time.get(cell_id, 0)

    db_count = row[0] if row else 0
    db_time  = row[1] if row else 0

    return jsonify({
        "cell":     cell_id,
        "count":    db_count + mem_count,
        "time_sec": db_time  + mem_time,
        "time_min": round((db_time + mem_time) / 60, 1)
    })

@app.route("/heatmap")
def get_heatmap():
    """Return all cell stats for heatmap rendering."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT cell_id, sighting_count, time_seconds FROM cell_stats")
    rows = cur.fetchall()
    con.close()

    # Start with DB data
    merged = {}
    for cell_id, count, time_sec in rows:
        merged[cell_id] = [count, time_sec]

    # Merge in-memory counts (includes cells not yet flushed)
    for cell_id, count in cell_counts.items():
        if cell_id in merged:
            merged[cell_id][0] += count
            merged[cell_id][1] += cell_time.get(cell_id, 0)
        else:
            merged[cell_id] = [count, cell_time.get(cell_id, 0)]

    cells = []
    for cell_id, (count, time_sec) in merged.items():
        lat_min, lat_max, lon_min, lon_max = cell_to_bounds(cell_id)
        cells.append({
            "cell_id":  cell_id,
            "count":    count,
            "time_sec": time_sec,
            "time_min": round(time_sec / 60, 1),
            "lat_min":  lat_min,
            "lat_max":  lat_max,
            "lon_min":  lon_min,
            "lon_max":  lon_max,
        })

    return jsonify(cells)

# ── BACKGROUND LOOP ───────────────────────────────────────────────────────────
def background_loop():
    while True:
        planes = fetch_planes()
        # Strip internal 'cell' field before emitting to frontend
        socketio.emit("planes", [{k: v for k, v in p.items() if k != "cell"} for p in planes])
        socketio.sleep(1)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    socketio.start_background_task(background_loop)
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True, use_reloader=False)
