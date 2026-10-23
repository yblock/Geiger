# Geiger

A logger and dashboard for a GQ GMC-320S Geiger counter, built on
[pygmc](https://github.com/Wikilicious/pygmc).

It's made for a counter that lives away from the Mac and records on its own. Plug it in
now and then to watch live background levels and copy everything it recorded since the
last visit. Each session adds to one database on the Mac, so the dashboard shows your full
history whether or not the counter is connected.

## What you need

- A GQ GMC-320S. Other GMC-300/320-series counters should work but are untested. This one
  runs firmware Re 1.17.
- A USB cable that carries data. A charge-only cable powers the counter, but no serial
  port appears.
- [uv](https://docs.astral.sh/uv/), which installs Python 3.10 or later if you don't
  have it.
- A Mac. The logger itself would likely run on Linux, but `install-agent` is macOS-only.

Then, from this folder:

```bash
uv sync
```

## A typical session

1. Plug the counter into the Mac and switch it on.
2. Start the logger and open the dashboard at <http://localhost:8320>:

   ```bash
   uv run geiger run
   ```

3. On connecting, the logger:
   - copies everything the counter recorded while away. The dashboard shows progress.
     A nearly empty memory takes a couple of seconds; a full one takes a few minutes.
   - sets the counter's clock to the Mac's local time if it has drifted more than 3 s.
   - streams live readings, one per second.
4. Watch the live reading, browse history, export what you need.
5. Unplug whenever you like. Every reading is saved the moment it arrives, so there's
   nothing to stop or flush first. The dashboard switches to "Counter not connected" and
   keeps serving history until you press Ctrl-C in the terminal. Unplugging while history
   is being copied is harmless too: nothing from a partial copy is kept, and it starts over
   next time. The counter keeps recording to its own memory until the next visit.

The counter can be plugged in after the logger starts; it's picked up within about 30
seconds. To copy the records without opening the dashboard:

```bash
uv run geiger sync
```

To browse stored history with the counter unplugged, run `uv run geiger run` as usual. The
dashboard shows "Counter not connected" above everything you've already collected.

## Before leaving the counter on its own

- **Its save setting decides what you get back.** The dashboard's Counter panel shows it as
  "Counter's own log". You can change it on the counter itself.
  - Every second gives the most detail, and is how this counter is set now. It fills the
    1 MB memory in roughly 11 days.
  - Every minute lasts about 60 times longer.
  - Hourly average lasts longer still, at hour resolution.
  - The "above threshold" modes are not imported, because they only record now and then
    and their timestamps can't be trusted.
- **Its memory is never erased.** pygmc has no command to clear the counter's history, so
  every sync re-reads it from the start and keeps only what's new. What the counter does
  when its memory fills hasn't been tested, so sync before it gets there.
- **Its clock is kept for you.** The counter has no time zone or daylight saving. The logger
  resets its clock on each connection, and readings recorded under the old clock still get
  their correct times. Pass `--no-set-clock` to leave the clock alone; imported times are
  corrected either way.

## The dashboard

### Current reading

- **The dose rate in µSv/h**, with the CPM it comes from. The CPM is counts over the last
  minute, the same rolling figure the counter shows on its screen. For the first minute
  after connecting it's marked "≈" and estimated from the seconds collected so far.
- **How it compares with the counter's alarm level.** That's the threshold set on the
  counter, 0.5 µSv/h on this one.
- **Your 7-day average** for context, once there's at least an hour of data.

### Particle tape

Under the reading, each tick is one detected particle over the last two minutes, scrolling
like a chart recorder.

It's the quickest way to see a change. Move the counter or hold it near something, and the
tick rate visibly changes within seconds. The one-minute average takes a full minute to
catch up. The tape also shows how random radioactive decay is: clusters and long pauses
are normal.

### Dose rate over time

- **Range:** 10 min, 1 h, 6 h, 24 h, 7 days, 30 days, or all time.
- **Units:** µSv/h or CPM. Your choice of range and units is remembered in this browser.
- **What each point shows:** the 10-minute and 1-hour views plot the rolling one-minute rate,
  as the counter displays it. Longer views plot the average per minute, per hour, or longer,
  depending on the range. In those views a shaded band runs from the quietest to the busiest
  whole minute in each step, so brief spikes stay visible after averaging.
- **Gaps:** time with no data, while the counter was off or nothing was synced, shows as a
  break in the line, never as an invented value.
- **Alarm line:** the counter's alarm level is drawn when it's near the data; otherwise the
  caption says it's far above.
- **Exact values:** hover or tap for the time, the value in both units, and the minute
  range. With the chart focused, the arrow keys, Home, End and Esc do the same from the
  keyboard.
- **Refresh:** every 5 seconds on short ranges, slower on long ones.

### Stats for the selected range

| Stat | Meaning |
|---|---|
| Average | Mean rate over every second recorded in the range |
| Busiest minute | The highest whole-minute rate, and when it happened |
| Quietest minute | The lowest whole-minute rate |
| Dose received | The ambient dose at the counter's spot during the recorded time (dose rate × time) |
| Recorded | How much of the range has data, and the total counts behind it |

### Data table and CSV

**Data table** lists what the chart is plotting, newest first. **Download CSV** gives
per-minute rows for the selected range: UTC time, local time, counts, seconds recorded,
CPM, and µSv/h. That works for spreadsheets or comparing with other monitors.

### Counter panel

- **Sync device history** copies new records on demand while the counter is connected.
  Next to it is the result of the last sync.
- Below that are the counter's firmware, serial number, connection, battery voltage, clock
  status, calibration, save setting, alarm, and how far back the stored history goes.

The page follows your system's light or dark mode and works on a phone. To reach it from
other devices on your network, run `uv run geiger run --host 0.0.0.0`. There's no login,
so only do that on a network you trust.

## Reading the numbers

- **Background varies by place.** Rock, soil, building materials, altitude and radon all
  change it. Readings in the 0.05–0.2 µSv/h range are common. This counter has averaged
  about 16 CPM (0.10 µSv/h) where it has been so far.
- **Minute readings are noisy by nature.** At 16 CPM, one minute's count naturally varies by
  about ±4 (±25%). An hour's average is good to about ±3%. Compare places or days using the
  1-hour or longer averages, not individual minutes.
- **Things worth watching for:**
  - Rain can raise background for a few hours, as it washes radon decay products out of
    the air.
  - Basements usually read higher than upper floors.
  - A flight shows up as a broad rise, many times ground level.
  - Some objects read well above background: uranium glass, old orange-glazed ceramics,
    some granite.
- **µSv/h is an estimate.** A Geiger tube counts particles but can't measure their energy.
  The counter converts CPM with its own calibration factor, 0.0065 µSv/h per CPM on this
  one. That suits ordinary background gamma. "Dose received" is the ambient dose where the
  counter sat, not a personal dosimeter reading.

## Commands

All of these run as `uv run geiger <command>`.

| Command | What it does |
|---|---|
| `run` | Serve the dashboard, copy new records, and log live while the counter is connected |
| `sync` | Copy new records from the counter, set its clock, and exit |
| `info` | Print the model, firmware, current CPM and µSv/h, calibration, battery, and clock |
| `set-clock` | Set the counter's clock now. The logger normally does this itself; stop it first |
| `install-agent` / `uninstall-agent` | Optional: keep the logger running in the background at login (launchd) |

Options:
- **All commands:** `--db PATH` (default `data/geiger.db`), `--device /dev/cu.usbserial-…`
  (default: auto-detect), `--http-port` (default 8320), `-v` for debug logging.
- **`run` also takes:** `--host` (default `127.0.0.1`), `--no-backfill` (don't copy the
  counter's records), and `--no-set-clock`.
- **`sync` also takes:** `--no-set-clock`.

## Troubleshooting

| Message | What to check |
|---|---|
| "no GQ counter found" | The cable carries data. With the counter plugged in, `ls /dev/cu.usbserial*` should list a port |
| "no GMC counter answered" | The USB port is there but the counter didn't reply. Switch it on |
| "…is in use by another program" | Another `geiger` command, or another serial app such as GQ's own software, has the port. Quit it |
| "can't serve on port 8320" | A logger is already running in another terminal, or pick a port with `--http-port` |
| "Logger not reachable" on the page | The `geiger run` process stopped. Start it again; the page reconnects |
| A sync failed | Run it again. A loose cable is the usual cause |

To start the database over, stop the logger and move `data/geiger.db` to the Trash. The
counter's own memory is untouched, so the next run copies it all back.

## Where the data lives

Everything is in `data/geiger.db`, a SQLite file that's ignored by git. To back it up,
copy the file while the logger isn't running. Its three tables:

- `counts`: one row per recorded interval (start, seconds, counts, live or from the
  counter's memory).
- `minutes`: a per-minute rollup that SQLite triggers keep in step.
- `meta`: the counter's details, calibration, measured clock offsets, and the last sync
  result.

## How it works

- **Only one process talks to the counter.** The logger takes an exclusive lock on the serial
  port, so a second `geiger` process gets a clear "in use" error instead of garbled replies.
- **Live data** comes from the counter's 1-second heartbeat. Each second is stored as
  `(start, seconds, count)`, with the Mac's clock as the time reference.
- **Records from the counter's memory** are read page by page and parsed by pygmc. Their
  times are mapped to real time with the clock offsets the logger has measured, including
  the ones from before any clock change, and memory is always copied before the clock
  is changed.
- **Nothing is counted twice.** Only records that fall in gaps of what's already stored are
  imported. A margin of about 2 seconds is left where live and copied data meet.
- **Rates** are always `60 × counts ÷ seconds recorded`, so partial minutes and mixed
  resolutions (seconds, minutes, hours) average correctly.

The GMC-320S has quirks that the code works around; they're documented at the top of
[geiger/device.py](geiger/device.py):
- It ignores a command sent right after another.
- Its history pages carry a stray extra byte. pygmc's own reader sometimes misaligns on it.
- The top bits of its heartbeat words are status flags.

### API

The dashboard is a static page over a small local API:

| Endpoint | Returns |
|---|---|
| `GET /api/status` | Logger state, counter details, calibration, latest reading, last sync |
| `GET /api/series?start=&end=&points=&tz=` | Chart points plus stats for a range (unix seconds) |
| `GET /api/recent?seconds=` | Per-second counts for the last few minutes |
| `GET /api/events` | Server-sent events: `reading` each second, `status` on changes |
| `GET /api/export.csv?start=&end=` | Per-minute counts, CPM, and µSv/h |
| `POST /api/sync` | Start copying the counter's records (needs header `X-Geiger: 1`) |

## Development

```bash
uv run pytest
```

| Path | Contents |
|---|---|
| `geiger/device.py` | Serial access through pygmc, and the device quirks |
| `geiger/logger.py` | The worker thread: live stream, backfill, clock setting |
| `geiger/history.py` | Parsing the counter's memory and mapping its clock to real time |
| `geiger/store.py` | SQLite schema, gap-only import, chart series and stats |
| `geiger/calibration.py` | CPM to µSv/h, matching pygmc's conversion |
| `geiger/server.py`, `geiger/cli.py` | HTTP API and command line |
| `geiger/static/` | The dashboard (plain HTML, CSS and JavaScript, no build step) |
