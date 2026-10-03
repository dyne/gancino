# nostr-calendar

Fetch events from an ICS + RSS feed and publish them as NIP-52 calendar
events (kind 31923) to Nostr relays, requesting inclusion in a kind 31924
calendar.

## Install

Clone this repository to `/opt/nostr-calendar`:

    sudo git clone <repo-url> /opt/nostr-calendar
    sudo chown -R "$USER:$USER" /opt/nostr-calendar
    cd /opt/nostr-calendar
    ./install.sh

The installer creates a virtualenv, installs dependencies, and copies
`config.toml.example` to `config.toml`.

## Configure

Edit `config.toml` and set at minimum:

- `secret_key` — your Nostr secret key (`nsec1...` or hex)
- `relays` — relays to publish to
- `calendar.d`, `calendar.title`, `calendar.description` — your calendar

Then lock down the file:

    chmod 600 config.toml

## Test

    /opt/nostr-calendar/venv/bin/python /opt/nostr-calendar/publish.py

Check the output and `publish.log`. On the first run, all events are
published; subsequent runs only publish new or changed events (dedupe
is tracked in `events.db`).

## Schedule with cron

    crontab -e

Add:

    */30 * * * * /opt/nostr-calendar/run.sh >> /opt/nostr-calendar/cron.log 2>&1

This runs every 30 minutes. Adjust the `*/30` to taste.

## Notes

- `config.toml` contains your secret key and is gitignored. Never commit it.
- The virtualenv is not relocatable — if you move the directory after
  installing, recreate the venv by re-running `./install.sh`.
- The only place the absolute install path appears is the crontab entry.