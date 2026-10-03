#!/usr/bin/env python3
"""
Fetch ICS + RSS events from do.basspistol.org and publish them as
NIP-52 calendar events (kind 31923) to Nostr relays, requesting
inclusion in a kind 31924 calendar.

Only relays that accept the event kind without requiring NIP-42 auth
are used. Relays that send an AUTH challenge are skipped.

Usage:
    publish.py             # normal run
    publish.py --dry-run   # log what would be published, no DB writes, no relay calls
"""

import hashlib
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from icalendar import Calendar

from pynostr.event import Event
from pynostr.key import PrivateKey
from pynostr.relay_manager import RelayManager

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.toml"


def load_config():
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def setup_logging(log_path: Path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


log = logging.getLogger("nostr-calendar")


# ---------------------------------------------------------------------------
# Feed fetching / parsing
# ---------------------------------------------------------------------------

def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "nostr-calendar/1.0"})
    resp.raise_for_status()
    return resp.text


def parse_ics(text: str) -> list[dict]:
    cal = Calendar.from_ical(text)
    return [dict(c) for c in cal.walk("VEVENT")]


def parse_rss(text: str) -> dict:
    feed = feedparser.parse(text)
    by_url = {}
    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue

        image = ""
        enclosures = entry.get("enclosures", [])
        if enclosures:
            image = enclosures[0].get("href", "")
        if not image:
            for l in entry.get("links", []):
                if l.get("rel") == "enclosure":
                    image = l.get("href", "")
                    break
        if not image:
            m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("description", ""))
            if m:
                image = m.group(1)

        categories = [c.get("term", "") for c in entry.get("tags", []) if c.get("term")]
        by_url[link] = {
            "image": image,
            "categories": categories,
            "description": entry.get("description", ""),
        }
    return by_url


# ---------------------------------------------------------------------------
# ICS field helpers
# ---------------------------------------------------------------------------

def ics_str(component: dict, key: str) -> str:
    val = component.get(key)
    if val is None:
        return ""
    if hasattr(val, "to_ical"):
        try:
            raw = val.to_ical().decode("utf-8", errors="replace")
        except Exception:
            raw = str(val)
    else:
        raw = str(val)
    raw = (
        raw.replace("\\,", ",")
           .replace("\\;", ";")
           .replace("\\n", "\n")
           .replace("\\N", "\n")
           .replace("\\\\", "\\")
    )
    return raw.strip()


def ics_dt(component: dict, key: str) -> int | None:
    val = component.get(key)
    if val is None:
        return None
    try:
        dt = val.dt
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        return int(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp())
    except Exception as e:
        log.warning("Could not parse %s: %s", key, e)
        return None


# ---------------------------------------------------------------------------
# Content / tags
# ---------------------------------------------------------------------------

def normalize_tag(tag: str) -> str:
    tag = tag.strip().lower()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^\w\-]", "", tag)
    return tag.strip("-")


def build_content(template: str, title: str, location: str, url: str) -> str:
    return template.format(title=title, location=location, url=url)


def make_event_id(ics_url: str, uid: str) -> str:
    m = re.match(r"^(\d+)@", uid)
    if m:
        return m.group(1)
    return hashlib.sha256(uid.encode()).hexdigest()[:16]


def day_timestamps(start: int, end: int | None) -> list[int]:
    days = [start // 86400]
    if end is not None:
        last = end // 86400
        if last > days[0]:
            days.extend(range(days[0] + 1, last + 1))
    return days


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published (
            d_tag TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            event_id TEXT,
            published_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def content_hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def already_published(conn, d_tag: str, chash: str) -> bool:
    row = conn.execute(
        "SELECT content_hash FROM published WHERE d_tag = ?", (d_tag,)
    ).fetchone()
    return row is not None and row[0] == chash


def record_published(conn, d_tag: str, chash: str, event_id: str):
    conn.execute(
        "INSERT OR REPLACE INTO published (d_tag, content_hash, event_id, published_at) VALUES (?, ?, ?, ?)",
        (d_tag, chash, event_id, int(time.time())),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------

def make_calendar_event(cfg: dict, pubkey_hex: str, event_d_tags: list[str]) -> Event:
    """Build the kind 31924 calendar, listing every event via an `a` tag."""
    cal = cfg["calendar"]
    tags = [
        ["d", cal["d"]],
        ["title", cal["title"]],
    ]
    for d_tag in event_d_tags:
        tags.append(["a", f"31923:{pubkey_hex}:{d_tag}"])
    return Event(
        kind=31924,
        content=cal["description"],
        tags=tags,
    )


def make_calendar_event_nostr(cfg, pubkey_hex, title, start, end, location, url,
                               image, tags, d_tag, duration, template) -> Event:
    """Build a kind 31923 event."""
    cal_coord = f"31924:{pubkey_hex}:{cfg['calendar']['d']}"
    final_end = end if end is not None else start + duration

    ev_tags = [
        ["d", d_tag],
        ["title", title],
        ["start", str(start)],
        ["end", str(final_end)],
        ["a", cal_coord],
    ]
    if location:
        ev_tags.append(["location", location])
    if image:
        ev_tags.append(["image", image])
    for t in tags:
        ev_tags.append(["t", t])
    for day in day_timestamps(start, final_end):
        ev_tags.append(["D", str(day)])

    return Event(
        kind=31923,
        content=build_content(template, title, location, url),
        tags=ev_tags,
    )


# ---------------------------------------------------------------------------
# Relay filtering
# ---------------------------------------------------------------------------

def filter_relays_without_auth(relays: list[str], timeout: float = 8.0) -> list[str]:
    """Probe each relay for an AUTH challenge on connect.

    Returns the list of relays that did NOT send AUTH within the timeout.
    Relays that sent AUTH are logged and excluded.
    """
    accepted = []
    for url in relays:
        rm = RelayManager(timeout=timeout)
        rm.add_relay(url)
        log.info("Probing %s for AUTH requirement …", url)

        # Give the relay a moment to connect and (maybe) send AUTH.
        deadline = time.time() + timeout
        saw_auth = False
        while time.time() < deadline:
            relay = rm.relays.get(url)
            if relay is None:
                break
            pool = getattr(relay, "message_pool", None)
            queue = getattr(pool, "queue", None) if pool else None
            if queue:
                while queue:
                    msg = queue.pop(0)
                    if isinstance(msg, list) and len(msg) >= 1 and msg[0] == "AUTH":
                        log.info("Relay %s requires AUTH — skipping.", url)
                        saw_auth = True
                        break
                if saw_auth:
                    break
            time.sleep(0.25)

        try:
            rm.close_all_relay_connections()
        except Exception:
            pass

        if not saw_auth:
            log.info("Relay %s does not require AUTH — will publish.", url)
            accepted.append(url)
        time.sleep(0.5)

    return accepted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    setup_logging(SCRIPT_DIR / cfg.get("log_path", "publish.log"))

    if dry_run:
        log.info("=== DRY RUN — no DB writes, no relay publishing ===")

    secret = cfg["secret_key"]
    relays = cfg["relays"]
    ics_url = cfg["ics_url"]
    rss_url = cfg["rss_url"]
    duration = int(cfg.get("default_duration", 3600))
    template = cfg.get("content_template", "{title}\n📍 {location}\n🔗 {url}")
    rss_only = bool(cfg.get("rss_only", False))

    pk = PrivateKey.from_nsec(secret) if secret.startswith("nsec") else PrivateKey(bytes.fromhex(secret))
    pubkey_hex = pk.public_key.hex()

    log.info("Using pubkey %s", pubkey_hex)
    log.info("Configured relays: %s", ", ".join(relays))
    log.info("rss_only=%s", rss_only)

    log.info("Fetching ICS …")
    ics_text = fetch_text(ics_url)
    log.info("Fetching RSS …")
    rss_text = fetch_text(rss_url)

    log.info("Parsing ICS …")
    vevents = parse_ics(ics_text)
    log.info("Parsing RSS …")
    rss_map = parse_rss(rss_text)

    log.info("Found %d ICS events, %d RSS entries", len(vevents), len(rss_map))

    conn = init_db(SCRIPT_DIR / cfg.get("db_path", "events.db"))

    to_publish = []
    skipped = 0
    skipped_no_rss = 0
    dry_run_count = 0

    for vev in vevents:
        uid = ics_str(vev, "UID")
        if not uid:
            continue

        title = ics_str(vev, "SUMMARY")
        url = ics_str(vev, "URL")
        location = ics_str(vev, "LOCATION")
        start = ics_dt(vev, "DTSTART")
        end = ics_dt(vev, "DTEND")
        categories_ics = ics_str(vev, "CATEGORIES")

        if start is None:
            log.warning("Skipping %s — no DTSTART", uid)
            continue

        rss = rss_map.get(url, {})

        if rss_only and not rss:
            skipped_no_rss += 1
            continue

        image = rss.get("image", "")
        rss_cats = rss.get("categories", [])

        if categories_ics:
            raw_tags = [c.strip() for c in categories_ics.split(",") if c.strip()]
        else:
            raw_tags = rss_cats
        tags = [normalize_tag(t) for t in raw_tags if normalize_tag(t)]

        d_tag = make_event_id(ics_url, uid)
        chash = content_hash(title, start, end, location, url, image, ",".join(tags))

        if dry_run:
            log.info("DRY RUN %s: title=%r image=%r tags=%r start=%d end=%s",
                     d_tag, title, image, tags, start, end)
            dry_run_count += 1
            continue

        if already_published(conn, d_tag, chash):
            skipped += 1
            continue

        ev = make_calendar_event_nostr(
            cfg, pubkey_hex, title, start, end, location, url,
            image, tags, d_tag, duration, template,
        )
        to_publish.append((d_tag, chash, ev))

    if dry_run:
        log.info("DRY RUN complete. Would publish %d, skipped_no_rss=%d",
                 dry_run_count, skipped_no_rss)
        return

    log.info("Publishing %d new/changed events (skipped %d unchanged, %d without RSS)",
             len(to_publish), skipped, skipped_no_rss)

    # --- Filter relays: skip any that require AUTH ------------------------
    accepted_relays = filter_relays_without_auth(relays)
    if not accepted_relays:
        log.error("No relays accept publishing without AUTH. Aborting.")
        return

    log.info("Publishing to: %s", ", ".join(accepted_relays))

    # Build the full set of d-tags the calendar should reference:
    # everything already in the DB, plus everything about to be published.
    rows = conn.execute("SELECT d_tag FROM published").fetchall()
    all_d_tags = {row[0] for row in rows}
    for d_tag, _, _ in to_publish:
        all_d_tags.add(d_tag)
    all_d_tags = sorted(all_d_tags)

    log.info("Calendar will reference %d events", len(all_d_tags))

    cal_ev = make_calendar_event(cfg, pubkey_hex, all_d_tags)
    cal_ev.sign(pk.hex())

    # --- Publish ----------------------------------------------------------
    relay_manager = RelayManager(timeout=5)
    for r in accepted_relays:
        relay_manager.add_relay(r)

    relay_manager.publish_event(cal_ev)
    log.info("Queued calendar kind 31924 (d=%s) referencing %d events",
             cfg["calendar"]["d"], len(all_d_tags))

    for d_tag, chash, ev in to_publish:
        ev.sign(pk.hex())
        relay_manager.publish_event(ev)
        log.info("Queued event %s — %s", d_tag, ev.content.split("\n", 1)[0])

    relay_manager.run_sync()
    time.sleep(2)
    relay_manager.close_all_relay_connections()

    for d_tag, chash, ev in to_publish:
        record_published(conn, d_tag, chash, ev.id)

    log.info("Done. %d events published, calendar references %d.",
             len(to_publish), len(all_d_tags))


if __name__ == "__main__":
    main()