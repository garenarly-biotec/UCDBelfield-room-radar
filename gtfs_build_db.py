#!/usr/bin/env python3
"""
gtfs_build_db.py — one-time / monthly GTFS preprocessing for Dublin Room Radar.

Downloads the TFI national static GTFS feed, finds every stop near UCD, and
for every other stop in the feed computes which bus routes reach UCD from
there and how long the trip takes, ON AVERAGE, across a normal weekday's
daytime service. Also records every (route, stop) pair for Dublin-area
stops, which daft_monitor.py needs to find one-transfer journeys when no
direct route exists. Writes a compact SQLite lookup table
(~/daft/gtfs_transit.db) that daft_monitor.py reads on every run.

Why "average across weekday daytime service" specifically:
  A naive approach (storing every individual trip and later taking the
  fastest one observed) systematically understates real travel time — it
  cherry-picks whichever single departure happened to be quickest all day,
  which is often a near-empty very-early-morning run or a rare express
  service, not what a student will typically experience commuting to class.
  This script instead averages across all trips that run on a normal
  Monday-Friday service pattern, within a representative daytime window,
  so the number shown is something you'd actually expect on a typical day.

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
from collections import defaultdict

# ── Paths ────────────────────────────────────────────────────────────────
DAFT_DIR    = os.path.expanduser("~/daft")
ZIP_PATH    = os.path.join(DAFT_DIR, "gtfs.zip")
EXTRACT_DIR = os.path.join(DAFT_DIR, "gtfs_extracted")
DB_PATH     = os.path.join(DAFT_DIR, "gtfs_transit.db")

GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"

# A stop counts as "at UCD" if its name contains any of these (case-insensitive).
# Includes "belfield" because two of the busiest stops right at the UCD main
# entrance are named "Belfield Court" (inbound/outbound) rather than "UCD" —
# without this marker those stops are silently missed.
# Extend this list if TFI renames or adds a campus stop.
UCD_NAME_MARKERS = ["ucd", "university college dublin", "belfield"]

# Bounding box used to decide which stops are "Dublin area" for the purposes
# of stop_routes (the transfer-route lookup table). Keeps that table from
# growing to cover the whole country.
DUBLIN_LAT = (53.20, 53.55)
DUBLIN_LON = (-6.60, -6.00)

# Ignore implausible travel times (e.g. overnight wrap-around trips, or a
# stop that's technically on the same trip but on a totally separate leg).
MIN_PLAUSIBLE_MIN = 0
MAX_PLAUSIBLE_MIN = 90

# Only average trips that arrive at UCD within this window. Excludes rare
# very-early depot/positioning runs and late-night Nitelink-style express
# services, both of which run far faster than normal daytime traffic and
# would otherwise drag the average down to something unrealistic.
DAYTIME_START = "06:00:00"
DAYTIME_END   = "23:00:00"

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


def file_exists_in_feed(name):
    direct = os.path.join(EXTRACT_DIR, name)
    if os.path.exists(direct):
        return True
    for root, _, files in os.walk(EXTRACT_DIR):
        if name in files:
            return True
    return False


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


def load_trips():
    """Returns (trip_route: trip_id -> route_id, trip_service: trip_id -> service_id)."""
    print("Loading trips.txt...")
    trip_route, trip_service = {}, {}
    f, reader, idx = open_csv_reader(find_file("trips.txt"))
    try:
        i_trip, i_route = idx["trip_id"], idx["route_id"]
        i_service = idx.get("service_id")
        for row in reader:
            trip_route[row[i_trip]] = row[i_route]
            if i_service is not None:
                trip_service[row[i_trip]] = row[i_service]
    finally:
        f.close()
    print(f"  {len(trip_route):,} trips loaded.")
    return trip_route, trip_service


def load_weekday_services():
    """
    Returns the set of service_ids that operate on at least one weekday
    (Monday-Friday), per calendar.txt. Used to exclude weekend-only
    timetables from the averaged travel times, since a normal week of
    classes is what matters for a commute estimate.

    If calendar.txt is missing from the feed (some agencies use only
    calendar_dates.txt for exceptions), falls back to treating every
    service as valid — better to include slightly more data than to
    silently produce an empty transit_times table.
    """
    if not file_exists_in_feed("calendar.txt"):
        print("calendar.txt not found in feed — skipping weekday filtering "
              "(all services will be treated as valid).")
        return None
    print("Loading calendar.txt (weekday service filter)...")
    weekday_services = set()
    f, reader, idx = open_csv_reader(find_file("calendar.txt"))
    try:
        i_sid = idx["service_id"]
        day_cols = [idx[d] for d in ("monday", "tuesday", "wednesday", "thursday", "friday")
                    if d in idx]
        for row in reader:
            if any(row[c].strip() == "1" for c in day_cols):
                weekday_services.add(row[i_sid])
    finally:
        f.close()
    print(f"  {len(weekday_services):,} weekday service patterns found.")
    return weekday_services


# ── Two-pass stop_times.txt processing ──────────────────────────────────
#
# stop_times.txt is the huge file (tens of millions of rows nationwide), so
# we never load it fully into memory. Pass 1 finds which trips touch a UCD
# stop, restricted to weekday daytime service, and at what time. Pass 2
# re-streams the file once more and does TWO things at the same time:
#
#   1. For trips that DO reach UCD (within the weekday/daytime filter):
#      accumulate minutes-to-UCD from every upstream stop on that trip,
#      then average all observations per (stop, route, UCD stop) at the
#      end — rather than storing every individual trip and later taking
#      the single fastest one, which understates real travel time.
#
#   2. For EVERY row whose stop falls inside the Dublin bounding box,
#      regardless of whether that trip ever reaches UCD: record the
#      (route_name, stop_id) pair → stop_routes table. This is what lets
#      daft_monitor.py find a one-transfer journey (e.g. "take the 16 to
#      Wellington Lane, then transfer to the 77X to UCD") for the very
#      common case where a listing's nearest stop has no direct route.
#
# Doing both in a single pass avoids a third full scan of a multi-GB file.

def pass_one_find_relevant_trips(ucd_stop_ids, trip_service, weekday_services):
    print("Pass 1/2: scanning stop_times.txt for weekday-daytime trips that reach UCD...")
    trip_ucd_arrival = {}  # trip_id -> {ucd_stop_id: seconds}
    day_start = time_to_seconds(DAYTIME_START)
    day_end   = time_to_seconds(DAYTIME_END)
    path = find_file("stop_times.txt")
    f, reader, idx = open_csv_reader(path)
    skipped_non_weekday = 0
    skipped_off_hours = 0
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
            tid = row[i_trip]
            if weekday_services is not None:
                if trip_service.get(tid) not in weekday_services:
                    skipped_non_weekday += 1
                    continue
            t = (row[i_arr] if i_arr is not None else "") or \
                (row[i_dep] if i_dep is not None else "")
            if not t:
                continue
            try:
                secs = time_to_seconds(t)
            except ValueError:
                continue
            if not (day_start <= secs <= day_end):
                skipped_off_hours += 1
                continue
            trip_ucd_arrival.setdefault(tid, {})[sid] = secs
    finally:
        f.close()
    print(f"  {len(trip_ucd_arrival):,} weekday-daytime trips pass through a UCD stop "
          f"(skipped {skipped_non_weekday:,} non-weekday, {skipped_off_hours:,} outside "
          f"{DAYTIME_START}–{DAYTIME_END}).")
    return trip_ucd_arrival


def pass_two_collect_travel_times(trip_ucd_arrival, trip_route, routes, ucd_stops, stops):
    print("Pass 2/2: averaging travel times to UCD + indexing all Dublin routes...")
    # (stop_id, route_name, ucd_stop_name) -> list of per-trip minute deltas,
    # accumulated across the day and averaged once at the end.
    samples = defaultdict(list)
    stop_routes_set = set()  # (route_name, stop_id) for every Dublin-area stop

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
            sid = row[i_stop]
            route_id = trip_route.get(tid)
            route_name = routes.get(route_id, route_id or "?")

            # ── (2) stop_routes: record this (route, stop) pair if the stop
            # is in the Dublin area — independent of whether this trip ever
            # reaches UCD, and independent of the weekday/daytime filter
            # (this table is just "what routes serve this stop at all", used
            # for transfer detection, not for timing).
            stop_info = stops.get(sid)
            if stop_info:
                _, slat, slon = stop_info
                if DUBLIN_LAT[0] <= slat <= DUBLIN_LAT[1] and DUBLIN_LON[0] <= slon <= DUBLIN_LON[1]:
                    stop_routes_set.add((route_name, sid))

            # ── (1) transit_times: only for trips that reach UCD within the
            # weekday/daytime filter applied in pass 1.
            ucd_arrivals = trip_ucd_arrival.get(tid)
            if not ucd_arrivals:
                continue
            t = (row[i_dep] if i_dep is not None else "") or \
                (row[i_arr] if i_arr is not None else "")
            if not t:
                continue
            try:
                secs = time_to_seconds(t)
            except ValueError:
                continue
            for ucd_sid, ucd_secs in ucd_arrivals.items():
                delta_min = (ucd_secs - secs) / 60.0
                if MIN_PLAUSIBLE_MIN < delta_min <= MAX_PLAUSIBLE_MIN:
                    samples[(sid, route_name, ucd_stops[ucd_sid])].append(delta_min)
    finally:
        f.close()

    total_samples = sum(len(v) for v in samples.values())
    rows = [
        (sid, route_name, round(sum(vals) / len(vals), 1), ucd_name, len(vals))
        for (sid, route_name, ucd_name), vals in samples.items()
    ]
    print(f"  {len(rows):,} unique (stop, route, UCD-stop) combinations, "
          f"averaged across {total_samples:,} individual weekday-daytime trips.")
    print(f"  {len(stop_routes_set):,} (route, stop) pairs indexed for transfer lookups.")
    return rows, stop_routes_set


# ── Write SQLite db ──────────────────────────────────────────────────────

def write_db(stops, rows, stop_routes_set):
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
    # avg_min is now a genuine arithmetic mean across all qualifying weekday
    # daytime trips (see pass_two_collect_travel_times) — n_trips records
    # how many samples went into that average, for transparency/debugging.
    c.execute("""CREATE TABLE transit_times (
        stop_id       TEXT,
        route_name    TEXT,
        avg_min       REAL,
        ucd_stop_name TEXT,
        n_trips       INTEGER
    )""")
    c.execute("""CREATE TABLE stop_routes (
        route_name TEXT,
        stop_id    TEXT,
        PRIMARY KEY (route_name, stop_id)
    )""")
    c.executemany(
        "INSERT INTO stops VALUES (?,?,?,?)",
        [(sid, name, lat, lon) for sid, (name, lat, lon) in stops.items()],
    )
    c.executemany(
        "INSERT INTO transit_times VALUES (?,?,?,?,?)", rows
    )
    c.executemany(
        "INSERT OR IGNORE INTO stop_routes VALUES (?,?)", list(stop_routes_set)
    )
    c.execute("CREATE INDEX idx_stops_latlon ON stops(lat, lon)")
    c.execute("CREATE INDEX idx_transit_stop ON transit_times(stop_id)")
    c.execute("CREATE INDEX idx_sr_stop ON stop_routes(stop_id)")
    c.execute("CREATE INDEX idx_sr_route ON stop_routes(route_name)")
    conn.commit()
    conn.close()
    size_mb = os.path.getsize(DB_PATH) / 1_048_576
    print(f"Done — {DB_PATH} ({size_mb:.1f} MB)")


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
    trip_route, trip_service = load_trips()
    weekday_services = load_weekday_services()

    trip_ucd_arrival = pass_one_find_relevant_trips(
        set(ucd_stops), trip_service, weekday_services
    )
    rows, stop_routes_set = pass_two_collect_travel_times(
        trip_ucd_arrival, trip_route, routes, ucd_stops, stops
    )

    if not rows:
        print("\nWARNING: no transit_times rows were produced. This usually means the "
              f"weekday/daytime filter ({DAYTIME_START}–{DAYTIME_END}) excluded everything — "
              "check that DAYTIME_START/DAYTIME_END still make sense for this feed.")
    if not stop_routes_set:
        print("\nWARNING: stop_routes ended up empty — check the DUBLIN_LAT/DUBLIN_LON "
              "bounding box still covers your stops.txt coordinates.")

    write_db(stops, rows, stop_routes_set)
    cleanup()

    print(f"\n✅ {DB_PATH} is ready. daft_monitor.py will use it automatically on its next run.")
    print("Re-run this script monthly to stay current with TFI route changes.")


if __name__ == "__main__":
    main()
