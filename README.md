# Gancino

Fetch events from a Gancio's ICS + RSS feed and publish them as [NIP-52](https://github.com/nostr-protocol/nips/blob/master/52.md)
calendar events (kind `31923`) to Nostr relays, requesting inclusion in a
kind `31924` calendar.

---

## How it works

Every run:

1. Fetches the ICS feed (event list with UTC start/end times).
2. Fetches the RSS feed (the "announcement stream", with images and tags).
3. Merges them by event URL. The ICS is the source of truth for time,
   location, UID and tags; the RSS contributes the image and, if the ICS
   lacks categories, the tags.
4. Publishes each event as a kind `31923` Nostr event, referencing the
   calendar (kind `31924`) via an `a` tag.
5. Skips events that haven't changed since the last run, using a content
   hash stored in a local SQLite database.

Events are **addressable** (`d` tag = ICS UID), so republishing replaces
the previous version on the relay rather than creating duplicates.

### `rss_only` mode

When `rss_only = true` (recommended), only events that appear in the RSS
feed are published. The RSS is treated as the "announcement stream":
newly-created events appear there, and recurring events reappear once the
current occurrence ends. This avoids back-filling the Nostr calendar with
every event in the ICS, including old ones that may be stale or cancelled.

With `rss_only = false`, every event in the ICS is published. Events
without an RSS match simply have no image.

**Note:** As of late 2026, the ICS and RSS feeds expose roughly the same
set of events, so `rss_only` now acts more as a safety filter than a
content filter. It is still recommended, since it keeps the calendar
tied to the announcement stream if the feeds ever diverge again.

### Calendar pruning

The kind `31924` calendar lists its events via `a` tags. Events are
removed from that listing automatically once their start time is more
than **24 hours in the past**. The underlying kind `31923` events are
not deleted — they remain on the relay — but they stop appearing in the
calendar listing.

The `start_ts` for each event is stored in the SQLite database. Older
databases are migrated automatically: a `start_ts` column is added, and
existing rows are backfilled the next time the script sees the
corresponding event in the feeds. No events are republished during
backfill.

---

## Requirements

- Linux (tested on Raspberry Pi OS)
- Python 3.11+ (3.12 or 3.13 recommended — see the note on 3.14 below)
- Outbound internet access to the feed URLs and relays

### Python 3.14 note

`coincurve` (a `pynostr` dependency) has no prebuilt wheel for Python 3.14
at the time of writing, so it must build from source. The installer pins
`coincurve==20.0.0` and constrains its build backend to
`scikit-build-core<0.10` to work around two upstream build bugs. This
works but is slow. **If you can, install Python 3.12 or 3.13** — prebuilt
wheels exist and installation is much faster.

---

## Install

Clone to `/opt/gancino`:

```bash
sudo git clone <repo-url> /opt/gancino
sudo chown -R "$USER:$USER" /opt/gancino
cd /opt/gancino
./install.sh
```

The installer:

- creates a virtualenv in `venv/`
- installs dependencies with the build constraints for Python 3.14
- copies `config.toml.example` to `config.toml` (if it doesn't exist)
- locks `config.toml` to mode `600`
- prints next steps

---

## Configure

Edit `config.toml`:

```toml
secret_key = "nsec1..."            # or 64-char hex

relays = [
    "wss://nos.lol",
]

ics_url = "https://do.basspistol.org/feed/ics?show_recurrent=true"
rss_url = "https://do.basspistol.org/feed/rss?show_recurrent=true"

rss_only = true                    # only publish events advertised in RSS

[calendar]
d = "basspistol-do"
title = "Basspistol Do 🗓️"
description = "Underground events promoted by the people, for the people."
```

Then:

```bash
chmod 600 config.toml
```

`config.toml` contains your secret key and is **gitignored**. Never commit it.
`config.toml.example` is the committed template.

### Calendar (kind 31924)

The `[calendar]` block defines the kind `31924` event that your published
events request inclusion in. Each published event carries an `a` tag of
the form:

```
31924:<your-pubkey>:<calendar.d>
```

And the calendar itself lists every active event with a matching `a` tag:

```
31923:<your-pubkey>:<event.d>
```

You don't need to publish the calendar separately first — the script
publishes it on every run (idempotent, since it's addressable via `d`).

### Relay selection

The script probes each configured relay for a NIP-42 `AUTH` challenge
before publishing. Relays that require authentication are **skipped** for
that run; only relays that accept the events without auth are used. If
none of the configured relays accept publishing, the script aborts
without publishing anything.

---

## Test

Always dry-run first. It fetches and parses the feeds, logs what would be
published (including images and tags), and touches neither the database
nor the relays:

```bash
/opt/gancino/venv/bin/python /opt/gancino/publish.py --dry-run
```

Sample output:

```
INFO DRY RUN 17943: title='Blokes fantasma 33 anys resistint' image='https://...jpg' tags=['gràcia', 'concert', ...] start=1791021600 end=1791079200
...
INFO DRY RUN complete. Would publish 20, skipped_no_rss=0
```

Then run for real:

```bash
/opt/gancino/venv/bin/python /opt/gancino/publish.py
```

Logs go to both stdout and `publish.log`.

---

## Schedule with cron

```bash
crontab -e
```

Add:

```
*/30 * * * * /opt/gancino/run.sh >> /opt/gancino/cron.log 2>&1
```

`run.sh` resolves its own directory, `cd`s into it, and invokes the
virtualenv Python — so it's safe to call from cron regardless of the
starting working directory.

Adjust `*/30` to taste. Every 15–30 minutes is plenty; the RSS feed is
truncated server-side, so polling more often doesn't recover older events.

---

## Files

| File | Purpose |
|---|---|
| `publish.py` | Main script. `--dry-run` for a no-op test. |
| `run.sh` | Cron wrapper. Resolves its own directory. |
| `install.sh` | Creates venv, installs deps, seeds config. |
| `requirements.txt` | Python dependencies. |
| `config.toml.example` | Configuration template (committed). |
| `config.toml` | Your configuration (gitignored, mode 600). |
| `events.db` | SQLite dedupe + state table (gitignored). |
| `publish.log` | Log file (gitignored). |
| `cron.log` | Cron output (gitignored). |

---

## Dedupe behavior

`events.db` stores, per event: the `d` tag, a content hash, the published
event id, the publish timestamp, and the event's start time (`start_ts`).
On each run:

- If the event is absent → publish.
- If the `d` tag exists but the content hash differs → republish
  (addressable event replaces the previous version on the relay).
- If both match → skip. If `start_ts` was missing for the row, it's
  backfilled from the current ICS data.

The content hash covers title, start, end, location, URL, image, and tags.

To force a full republish, delete `events.db`. Relays will replace the
existing events; nothing is duplicated.

### Schema migration

If an existing `events.db` predates the `start_ts` column, the script adds
it automatically on startup (`ALTER TABLE published ADD COLUMN`). Existing
rows keep their data; `start_ts` is filled in opportunistically the next
time the script processes the matching event. Rows with a `NULL`
`start_ts` are excluded from the calendar listing until backfilled.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'feedparser'`**
You're running the script with system Python. Use the virtualenv:

```bash
/opt/gancino/venv/bin/python /opt/gancino/publish.py
```

or activate it first:

```bash
source /opt/gancino/venv/bin/activate
./publish.py
```

**`coincurve` fails to build**
See the Python 3.14 note above. The installer handles it, but if you're
installing manually you need `PIP_CONSTRAINT=build-constraints.txt` with
`scikit-build-core<0.10` inside it.

**Only some events have images**
Expected. Images come from the RSS feed. If the RSS has fewer entries than
the ICS (which is no longer typical with the current endpoints), only the
RSS-covered events will carry an image.

**Event times look wrong**
The ICS feed uses UTC (`DTSTART:...Z`) and is authoritative. The RSS
renders times in the feed server's local timezone — ignore it.

**A relay doesn't accept writes**
The script already skips relays that require NIP-42 `AUTH`. If a relay
still silently drops events, check `publish.log` for WebSocket errors —
some relays reject specific event kinds or impose tag limits.

**The calendar keeps growing**
It shouldn't — events more than 24 hours past their start time are pruned
from the listing on each run. If the listing seems stuck, check that the
legacy rows have been backfilled (see **Schema migration** above).

**Timeout warnings on each run**
`pynostr` logs a WebSocket timeout while connecting. These are cosmetic:
the events and calendar are queued before the timeout fires and are
delivered once the socket is ready. The script finishes in a few seconds
regardless.

---

## License

Copyright (C) 2026  Dyne.org

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
