# 🏠 Dublin Room Radar

A Python-based automated room rental monitor for Dublin, Ireland. Searches multiple listing platforms on a schedule, emails you a zone-grouped interactive digest when new listings appear, and serves a local dashboard where you can rate and track every property.

Built for anyone relocating to Dublin — particularly students commuting to UCD Belfield — but the zone system and search areas are easy to adapt.

---

## Features

- **Multi-source search** — polls Daft.ie (primary), MyHome.ie, SpareRoom.ie, and Rent.ie
- **Zone-grouped results** — listings are automatically grouped into five distance zones from UCD Belfield and sorted nearest-first within each zone
- **Interactive email digest** — tabbed layout, no endless scrolling; click a zone tab to jump straight to listings in that area
- **NEW vs seen-before badges** — every email shows which listings are fresh and which are recurring from earlier runs
- **Local dashboard** — open `http://localhost:8765` to rate every listing 👍 / 😐 / 👎, mark properties as checked, and filter your shortlist
- **Smart email cadence** — only sends an email when genuinely new listings appear; won't spam you with an identical digest every 20 minutes
- **Persistent state** — ratings, checked/unchecked status, and seen history are all saved to a local SQLite database and survive restarts
- **GTFS-powered transit info** — per-listing bus information computed from TFI's official static feed, accurate to the nearest bus stop rather than just the neighbourhood

---

## Zones

Listings are automatically classified into five zones by straight-line distance from UCD Belfield (lat `53.3079`, lon `-6.2236`). The zone boundaries and bus estimates below are approximate.

| Zone | Distance | Est. commute | Example areas |
|------|----------|--------------|---------------|
| **A — Walk / Cycle** | < 2.5 km | 5–20 min | Clonskeagh, Stillorgan, Donnybrook, Booterstown, Milltown |
| **B — South Dublin** | 2.5–5 km | 15–30 min | Ranelagh, Rathgar, Rathmines, Harold's Cross, Dundrum, Blackrock |
| **C — South-West** | 4–8 km | 25–45 min | Terenure, Kimmage, Crumlin, Rathfarnham, Firhouse |
| **D — Coastal / South County** | 5–12 km | 20–45 min | Foxrock, Dun Laoghaire, Dalkey, Leopardstown, Killiney |
| **E — City / North-of-Canal** | 3–7 km | 20–40 min | Portobello, Glasnevin, Drumcondra, Phibsborough |

> North Dublin (Swords, Malahide, Artane, Clare Hall) is excluded by default because journey times to UCD are too long for daily commuting.

---

## Requirements

- macOS or Linux (Windows untested)
- Python 3.9 or later (Anaconda environment works fine)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) set up

---

## Installation

```bash
# Clone the repository
git clone https://github.com/your-username/dublin-room-radar.git
cd dublin-room-radar

# Install dependencies
pip install curl_cffi schedule beautifulsoup4 lxml flask
```

---

## Configuration

Open `daft_monitor.py` and edit the config block near the top of the file. There are only a few things you need to change before your first run.

### Gmail

```python
GMAIL_FROM   = "your.email@gmail.com"    # Gmail address to send FROM
GMAIL_TO     = "your.email@gmail.com"    # Gmail address to send TO (can be the same)
GMAIL_APP_PW = "xxxx xxxx xxxx xxxx"     # 16-character App Password from Google
```

To generate an App Password: Google Account → Security → 2-Step Verification → App Passwords → create one for Mail.

### Gender filter

```python
# Set to "male", "female", or "" for no filter (all listings regardless of preference)
GENDER_FILTER = "male"
```

| Value | What it returns |
|-------|-----------------|
| `"male"` | Only listings explicitly marked as suitable for males |
| `"female"` | Only listings explicitly marked as suitable for females |
| `""` | All listings regardless of gender preference |

> Note: the gender filter applies to Daft.ie's API filter. Listings that don't specify a preference are only included when `GENDER_FILTER = ""`.

### Price ceiling

```python
MAX_PRICE = 1250    # Maximum monthly rent in euros (no lower bound)
```

### Source toggles

Turn individual platforms on or off:

```python
ENABLE_DAFT      = True    # Daft.ie — primary, most reliable
ENABLE_MYHOME    = True    # MyHome.ie — best-effort HTML scrape
ENABLE_SPAREROOM = True    # SpareRoom.ie — works best from an Irish IP
ENABLE_RENT_IE   = True    # Rent.ie — often Cloudflare-gated
```

### Poll interval

```python
POLL_INTERVAL = 20    # Minutes between checks in daemon mode
```

---

## First-time setup — Transit data (optional but recommended)

For accurate per-listing bus information (e.g. "walk 4 min to Harold's Cross Village → 77X → UCD Glenomena, ≈22 min"), build the GTFS transit database once before running the monitor.

```bash
python3 gtfs_build_db.py
```

This downloads the TFI GTFS static feed (~144 MB), processes it in two passes, and writes `~/daft/gtfs_transit.db` (~1 MB). It takes approximately 2–3 minutes and only needs to be re-run if you want to refresh the bus route data (TFI updates their feed monthly).

**If the download is blocked** (common outside Ireland due to TLS fingerprinting), download it manually in your browser and move it into place:

1. Open in Safari or Chrome — it will auto-download:
   `https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip`
2. Move the file:
   ```bash
   mv ~/Downloads/GTFS_Realtime.zip ~/daft/gtfs.zip
   ```
3. Re-run the script — it will skip the download and go straight to processing:
   ```bash
   python3 gtfs_build_db.py
   ```

The monitor works without the transit database — listings just won't include bus directions.

---

## Running

### One-time check

Fetches listings, emails you a digest, then exits. Good for testing your config.

```bash
python3 daft_monitor.py --once
```

### Daemon mode (recommended)

Fetches on a schedule and also serves the local dashboard. Keeps running until you stop it.

```bash
python3 daft_monitor.py --daemon
```

Open the dashboard at `http://localhost:8765` anytime while the daemon is running.

### Dashboard only

Serves the dashboard without fetching any new listings. Useful if you want to browse and rate without triggering a search.

```bash
python3 daft_monitor.py --dashboard
```

### Clear the database

Wipes all seen history, ratings, and checked state. The next run will treat every current listing as new.

```bash
python3 daft_monitor.py --clear --once
```

### Run in the background (macOS)

```bash
nohup python3 ~/daft/daft_monitor.py --daemon > ~/daft/monitor.log 2>&1 &
echo $! > ~/daft/monitor.pid
```

Check the log:
```bash
tail -f ~/daft/monitor.log
```

Stop it:
```bash
kill $(cat ~/daft/monitor.pid)
```

---

## The email digest

When new listings appear the monitor sends a tabbed HTML email. Zone tabs appear at the top — click any tab to see only listings in that distance band. Each listing card shows:

- Price and area, with straight-line distance to UCD
- Platform badge (Daft / MyHome / SpareRoom / Rent.ie)
- **● NEW** badge for listings appearing for the first time, or **↻ seen before** for recurring ones
- Your existing rating (👍 / 😐 / 👎) if you've already rated it in the dashboard
- Bus directions to UCD (if GTFS data is available)
- Links to view the listing, open it on Google Maps, and jump to it in the dashboard to rate it

No email is sent if nothing new appeared since the last check.

---

## The dashboard

Open `http://localhost:8765` while the daemon is running.

| Control | What it does |
|---------|--------------|
| **○ / ✓** (top-left of card) | Toggle checked/unchecked |
| 👍 Like | Mark as interested |
| 😐 Neutral | Mark as undecided |
| 👎 Dislike | Mark as not interested — removes from future emails |
| Filter tabs | Show: Unchecked / All / 👍 Liked / ✓ Checked |

All actions save immediately to `~/daft/daft_seen.db`. Checked listings and disliked listings are excluded from future email digests. Unchecked listings that aren't disliked keep reappearing in emails with the "seen before" badge until you act on them.

The dashboard auto-refreshes every 5 minutes so new listings from the daemon appear without a manual reload.

---

## Platform notes

| Platform | Reliability | Notes |
|----------|-------------|-------|
| **Daft.ie** | ✅ Reliable | Internal API, 52 areas covered, gender filter applied at API level |
| **MyHome.ie** | ⚠️ Best-effort | HTML scrape; room-share inventory is smaller than Daft |
| **SpareRoom.ie** | ⚠️ Best-effort | Works better from an Irish IP; may time out from abroad |
| **Rent.ie** | ⚠️ Best-effort | Frequently returns a Cloudflare 403 from outside Ireland |

All three scraper sources degrade gracefully — if one fails, the others keep running and the script logs a warning. Daft.ie is the primary and most complete source; the others are supplementary.

---

## File structure

```
dublin-room-radar/
├── daft_monitor.py        # Main script — fetcher, email, dashboard
├── gtfs_build_db.py       # One-time GTFS preprocessing (run once)
├── README.md
└── LICENSE
```

Generated at runtime (in `~/daft/` by default):

```
~/daft/
├── daft_seen.db           # SQLite — listings, ratings, checked state
├── gtfs_transit.db        # SQLite — GTFS transit lookup (built by gtfs_build_db.py)
├── gtfs.zip               # TFI GTFS download cache
└── monitor.log            # Log output (if running in background)
```

---

## Customising the search areas

Areas and their Daft.ie geoFilter IDs are defined in the `DAFT_AREAS` dict and grouped into zones via `ZONES` at the top of `daft_monitor.py`. To add or remove an area, find its ID from the `daftlistings` Python library:

```python
from daftlistings import Location
print([(l.name, l.value["id"]) for l in Location if "DUBLIN" in l.name])
```

---

## Disclaimer

This tool makes automated requests to Daft.ie and other platforms. Automated scraping may be against their Terms of Service. It is intended for personal, non-commercial use only. Use responsibly — the built-in delays between requests are there for a reason. The author takes no responsibility for any consequences arising from use of this tool.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [Transport for Ireland](https://www.transportforireland.ie/) — GTFS static feed used for transit data
- [daftlistings](https://github.com/AnthonyBloomer/daftlistings) — for area ID reference
