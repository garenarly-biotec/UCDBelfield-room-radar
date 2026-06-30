#!/usr/bin/env python3
"""
gtfs_build_db.py — one-time / monthly GTFS preprocessing for Dublin Room Radar.

Downloads the TFI national static GTFS feed, finds every stop near UCD, and
for every other stop in the feed computes which bus routes reach UCD from
there and how long the trip takes. Writes a compact SQLite lookup table
(~/daft/gtfs_transit.db) that daft_monitor.py reads on every run.

Usage:
    python3 gtfs_build_db.py      # macOS / Linux
    python  gtfs_build_db.py      # Windows

If the automatic download fails with a 403 (common from outside Ireland),
manually download this URL in a browser:
    https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip
and move it to ~/daft/gtfs.zip, then re-run this script — it detects the
file and skips straight to processing.
"""

import os
import sys
import csv
import sqlite3
import zipfile
import shutil
import urllib.request
import urllib.error

# ── Paths ────────────────────────────────────────────────────────────────
DAFT_DIR    = os.path.expanduser("~/daft")
ZIP_PATH    = os.path.join(DAFT_DIR, "gtfs.zip")
EXTRACT_DIR = os.path.join(DAFT_DIR, "gtfs_extracted")
DB_PATH     = os.path.join(DAFT_DIR, "gtfs_transit.db")

GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"

# A stop counts as "at UCD" if its name contains any of these (case-insensitive).
# Extend this list if TFI renames or adds a campus stop.
UCD_NAME_MARKERS = ["ucd", "university college dublin"]

# Ignore implausible travel times (e.g. overnight wrap-around trips, or a
# stop that's technically on the same trip but on a totally separate leg).
MIN_PLAUSIBLE_MIN = 0
MAX_PLAUSIBLE_MIN = 90

PROGRESS_EVERY = 1_000_000


# ── Download / extract ──────────────────────────────────────────────────

def download_gtfs():
    if os.path.exists(ZIP_PATH):
        print(f"Found existing {ZIP_PATH} — skipping download.")
        return
    os.makedirs(DAFT_DIR, exist_ok=True)
    print("Downloading TFI GTFS static feed (~144 MB)...")
    req = urllib.request.Request(GTFS_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(ZIP_PATH, "wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as e:
        print(f"\nDownload failed: HTTP {e.code}")
        if e.code == 403:
            print("The TFI server is blocking this automated request (common from outside Ireland).")
            print("Manual fix:")
            print(f"  1. Open this URL in a browser — it will auto-download:")
            print(f"     {GTFS_URL}")
            print(f"  2. Move the downloaded file to: {ZIP_PATH}")
            print("  3. Re-run this script — it will detect the file and skip the download.")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\nDownload failed: {e}")
        print("Check your internet connection, or use the manual-download steps in the README.")
        sys.exit(1)
    print("Download complete.")


def extract_gtfs():
    print("Extracting GTFS feed...")
    if os.path.exists(EXTRACT_DIR):
        shutil.rmtree(EXTRACT_DIR)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(EXTRACT_DIR)
    print("Extraction complete.")


def find_file(name):
    """GTFS feeds occasionally nest the .txt files one directory deep."""
    direct = os.path.join(EXTRACT_DIR, name)
    if os.path.exists(direct):
        return direct
    for root, _, files in os.walk(EXTRACT_DIR):
        if name in files:
            return os.path.join(root, name)
    raise FileNotFoundError(
        f"{name} not found anywhere under {EXTRACT_DIR}. "
        f"The GTFS feed format may have changed — check the extracted folder by hand."
    )


# ── Helpers ──────────────────────────────────────────────────────────────

def time_to_seconds(t):
    """GTFS times can exceed 24:00:00 for trips that run past midnight."""
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def open_csv_reader(path):
    f = open(path, encoding="utf-8-sig", newline="")
    reader = csv.reader(f)
    header = next(reader)
    idx = {col.strip(): i for i, col in enumerate(header)}
    return f, reader, idx


# ── Load reference tables ───────────────────────────────────────────────

def load_stops():
    print("Loading stops.txt...")
    stops = {}  # stop_id -> (name, lat, lon)
    f, reader, idx = open_csv_reader(find_file("stops.txt"))
    try:
        i_id, i_name = idx["stop_id"], idx.get("stop_name")
        i_lat, i_lon = idx["stop_lat"], idx["stop_lon"]
        for row in reader:
            try:
                lat = float(row[i_lat])
                lon = float(row[i_lon])
            except (ValueError, IndexError):
                continue
            name = row[i_name].strip() if i_name is not None else ""
            stops[row[i_id]] = (name, lat, lon)
    finally:
        f.close()
    print(f"  {len(stops):,} stops loaded.")
    return stops


def find_ucd_stops(stops):
    ucd = {}  # stop_id -> stop_name
    for sid, (name, _, _) in stops.items():
        lname = name.lower()
        if any(marker in lname for marker in UCD_NAME_MARKERS):
            ucd[sid] = name
    names = sorted(set(ucd.values()))
    print(f"  {len(ucd)} UCD-area stops identified: {names}")
    if not ucd:
        print("WARNING: no stops matched UCD_NAME_MARKERS — transit_times will be empty.")
        print("Open stops.txt and check what TFI currently calls the on-campus stops,")
        print("then add the right keyword to UCD_NAME_MARKERS near the top of this file.")
    return ucd


def load_routes():
    print("Loading routes.txt...")
    routes = {}  # route_id -> display name
    f, reader, idx = open_csv_reader(find_file("routes.txt"))
    try:
        i_id = idx["route_id"]
        i_short = idx.get("route_short_name")
        i_long = idx.get("route_long_name")
        for row in reader:
            short = row[i_short].strip() if i_short is not None and i_short < len(row) else ""
            long_ = row[i_long].strip() if i_long is not None and i_long < len(row) else ""
            routes[row[i_id]] = short or long_ or row[i_id]
    finally:
        f.close()
    print(f"  {len(routes):,} routes loaded.")
    return routes


def load_trip_routes():
    print("Loading trips.txt...")
    trip_route = {}  # trip_id -> route_id
    f, reader, idx = open_csv_reader(find_file("trips.txt"))
    try:
        i_trip, i_route = idx["trip_id"], idx["route_id"]
        for row in reader:
            trip_route[row[i_trip]] = row[i_route]
    finally:
        f.close()
    print(f"  {len(trip_route):,} trips loaded.")
    return trip_route


# ── Two-pass stop_times.txt processing ──────────────────────────────────
#
# stop_times.txt is the huge file (tens of millions of rows nationwide), so
# we never load it fully into memory. Pass 1 finds which trips touch a UCD
# stop and at what time. Pass 2 re-streams the file, but now only keeps rows
# belonging to those (relatively few) trips, and for each upstream stop on
# the trip computes minutes-to-UCD.

def pass_one_find_relevant_trips(ucd_stop_ids):
    print("Pass 1/2: scanning stop_times.txt for trips that reach UCD...")
    trip_ucd_arrival = {}  # trip_id -> {ucd_stop_id: seconds}
    path = find_file("stop_times.txt")
    f, reader, idx = open_csv_reader(path)
    try:
        i_trip = idx["trip_id"]
        i_stop = idx["stop_id"]
        i_arr = idx.get("arrival_time")
        i_dep = idx.get("departure_time")
        for n, row in enumerate(reader, 1):
            if n % PROGRESS_EVERY == 0:
                print(f"  ...{n:,} rows scanned")
            sid = row[i_stop]
            if sid not in ucd_stop_ids:
                continue
            t = (row[i_arr] if i_arr is not None else "") or \
                (row[i_dep] if i_dep is not None else "")
            if not t:
                continue
            try:
                secs = time_to_seconds(t)
            except ValueError:
                continue
            trip_ucd_arrival.setdefault(row[i_trip], {})[sid] = secs
    finally:
        f.close()
    print(f"  {len(trip_ucd_arrival):,} trips pass through a UCD stop.")
    return trip_ucd_arrival


def pass_two_collect_travel_times(trip_ucd_arrival, trip_route, routes, ucd_stops):
    print("Pass 2/2: computing travel times to UCD for every upstream stop...")
    relevant_trips = set(trip_ucd_arrival)
    results = []  # (origin_stop_id, route_name, minutes, ucd_stop_name)
    path = find_file("stop_times.txt")
    f, reader, idx = open_csv_reader(path)
    try:
        i_trip = idx["trip_id"]
        i_stop = idx["stop_id"]
        i_arr = idx.get("arrival_time")
        i_dep = idx.get("departure_time")
        for n, row in enumerate(reader, 1):
            if n % PROGRESS_EVERY == 0:
                print(f"  ...{n:,} rows scanned")
            tid = row[i_trip]
            ucd_arrivals = trip_ucd_arrival.get(tid)
            if not ucd_arrivals:
                continue
            sid = row[i_stop]
            t = (row[i_dep] if i_dep is not None else "") or \
                (row[i_arr] if i_arr is not None else "")
            if not t:
                continue
            try:
                secs = time_to_seconds(t)
            except ValueError:
                continue
            route_id = trip_route.get(tid)
            route_name = routes.get(route_id, route_id or "?")
            for ucd_sid, ucd_secs in ucd_arrivals.items():
                delta_min = (ucd_secs - secs) / 60.0
                if MIN_PLAUSIBLE_MIN < delta_min <= MAX_PLAUSIBLE_MIN:
                    results.append((sid, route_name, round(delta_min, 1), ucd_stops[ucd_sid]))
    finally:
        f.close()
    print(f"  {len(results):,} (stop, route, UCD-stop) travel-time rows collected.")
    return results


# ── Write SQLite db ──────────────────────────────────────────────────────

def write_db(stops, rows):
    print(f"Writing {DB_PATH} ...")
    os.makedirs(DAFT_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE stops (
        stop_id   TEXT PRIMARY KEY,
        stop_name TEXT,
        lat       REAL,
        lon       REAL
    )""")
    c.execute("""CREATE TABLE transit_times (
        stop_id       TEXT,
        route_name    TEXT,
        avg_min       REAL,
        ucd_stop_name TEXT
    )""")
    c.executemany(
        "INSERT INTO stops VALUES (?,?,?,?)",
        [(sid, name, lat, lon) for sid, (name, lat, lon) in stops.items()],
    )
    c.executemany(
        "INSERT INTO transit_times VALUES (?,?,?,?)", rows
    )
    c.execute("CREATE INDEX idx_stops_latlon ON stops(lat, lon)")
    c.execute("CREATE INDEX idx_transit_stop ON transit_times(stop_id)")
    conn.commit()
    conn.close()
    print("Done.")


def cleanup():
    if os.path.exists(EXTRACT_DIR):
        shutil.rmtree(EXTRACT_DIR, ignore_errors=True)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    download_gtfs()
    extract_gtfs()

    stops = load_stops()
    ucd_stops = find_ucd_stops(stops)
    if not ucd_stops:
        print("\nAborting: cannot build transit_times without at least one matching UCD stop.")
        sys.exit(1)

    routes = load_routes()
    trip_route = load_trip_routes()

    trip_ucd_arrival = pass_one_find_relevant_trips(set(ucd_stops))
    rows = pass_two_collect_travel_times(trip_ucd_arrival, trip_route, routes, ucd_stops)

    write_db(stops, rows)
    cleanup()

    print(f"\n✅ {DB_PATH} is ready. daft_monitor.py will use it automatically on its next run.")
    print("Re-run this script monthly to stay current with TFI route changes.")


if __name__ == "__main__":
    main()