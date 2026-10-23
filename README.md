# Geiger

![The Geiger dashboard in dark mode. A desktop window shows the live dose rate of 0.099 µSv/h, a tick for each detected particle over the last two minutes, and a week of history with a tooltip on a rain-driven bump. Two phones show the live reading and the radiation levels table.](docs/dashboard.png)

<sub>Shown with made-up data from <code>geiger demo</code>.</sub>

A logger and dashboard for a GQ GMC-320S Geiger counter, built on
[pygmc](https://github.com/Wikilicious/pygmc).

It's made for a counter that lives away from the Mac and records on its own. Plug it in
now and then to watch live background levels and copy everything it recorded since the
last visit. Each session adds to one database on the Mac, so the dashboard shows your full
history whether or not the counter is connected. While it's plugged in, you can also check
how full its memory is and change its settings from the dashboard instead of its buttons.

## What you need

- A GQ GMC-320S. Other GMC-300/320-series counters should work but are untested. This one
  runs firmware Re 1.17.
- A USB cable that carries data. A charge-only cable powers the counter, but no serial
  port appears.
- [uv](https://docs.astral.sh/uv/), which installs Python 3.10 or later if you don't
  have it. The start script below offers to install uv for you.
- A Mac. The logger itself would likely run on Linux, but the start script and
  `install-agent` are macOS-only.

## Start

**Double-click `Start Geiger.command`** in this folder. It opens a Terminal window and
then:
- offers to install uv if it's missing, using Homebrew when you have it and uv's official
  installer otherwise. Nothing is installed unless you answer `y`.
- installs or updates Geiger's packages with `uv sync`.
- starts the logger and opens the dashboard in your browser.

If Geiger is already running, it just opens the dashboard. Close the window, or press
Ctrl-C in it, to stop the logger.

macOS may block the script the first time if the folder came from a downloaded zip rather
than `git clone`. In that case, right-click the file, choose Open, then confirm.

From a terminal, the same thing is:

```bash
uv sync
uv run geiger run
```

No counter? `uv run geiger demo` runs the dashboard with a simulated one and 30 days of
made-up history, in a temporary database that never touches your real one.

## A typical session

1. Plug the counter into the Mac and switch it on.
2. Start Geiger as above. The dashboard is at <http://localhost:8320>.
3. On connecting, the logger:
   - copies everything the counter recorded while away. The dashboard shows progress.
     A nearly empty memory takes a couple of seconds; a full one takes a few minutes.
   - sets the counter's clock to the Mac's local time if it has drifted more than 3 s.
   - streams live readings, one per second.
4. Watch the live reading, browse history, export what you need.
5. Unplug whenever you like. Every reading is saved the moment it arrives, so there's
   nothing to stop or flush first. The dashboard switches to "Counter not connected" and
   keeps serving history until you close the Terminal window or press Ctrl-C.
   Unplugging while history is being copied is harmless too: nothing from a partial copy
   is kept, and it starts over next time. The counter keeps recording to its own memory
   until the next visit.

The counter can be plugged in after the logger starts; it's picked up within about 30
seconds. To copy the records without opening the dashboard:

```bash
uv run geiger sync
```

To browse stored history with the counter unplugged, start Geiger as usual. The dashboard
shows "Counter not connected" above everything you've already collected.

## Before leaving the counter on its own

- **Its save setting decides what you get back.** Change it under **Counter settings →
  Save to memory** on the dashboard, or on the counter itself.
  - Every second gives the most detail, and is how this counter is set now. It fills the
    1 MB memory in roughly 11 days.
  - Every minute lasts about 60 times longer.
  - Hourly average lasts longer still, at hour resolution.
  - The "above threshold" modes are not imported, because they only record now and then
    and their timestamps can't be trusted.
- **Its memory is a ring buffer.** Once full, the counter overwrites its oldest records, so
  sync before then. The **Counter memory** panel shows how much room is left at the
  current setting. Syncing never erases anything: each sync re-reads the memory and keeps
  only what's new. Erasing is only possible from the counter's own menu, described in that
  panel.
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

### Radiation levels

A reference table puts readings in context, and marks the row the latest reading falls in:

| Level | Dose rate | Total dose |
|---|---|---|
| Safe | under 1 µSv/h | under 10 mSv a year |
| Concerning | 1–10 µSv/h | 10–100 mSv a year |
| Harmful | 10 µSv/h – 10 mSv/h | 100 mSv – 1 Sv |
| Deadly | over 10 mSv/h | over 1 Sv within days |

Each row explains what the level means, with comparisons such as a CT scan, a flight, or
the limit for radiation workers. The figures come from the
[CDC](https://www.cdc.gov/radiation-emergencies/hcp/clinical-guidance/ars.html),
[ICRP](https://www.icrp.org/publication.asp?id=ICRP%20Publication%20103), and
[Health Physics Society](https://hps.org/wp-content/uploads/2024/12/radiationrisk.pdf).
It's a rough guide, not safety advice.

### Counter panel

- **Sync device history** copies new records on demand while the counter is connected.
  Next to it is the result of the last sync.
- Below that are the counter's firmware, serial number, connection, battery voltage, clock
  status, calibration, alarm, and how far back the stored history goes.

### Counter memory

- **What's on the counter:** how full its memory is, how many records it holds and the
  dates they span, all as of the last sync.
- **How long it lasts:** the time left before it starts overwriting its oldest records at
  the current save setting. If it has already wrapped, how much time it holds, so you know
  how often to sync.
- **Downloads:** every record it held at the last sync, as a CSV with times, counts, CPM
  and µSv/h, or as a raw memory backup (`.bin`).
- **Erasing:** USB can only read the memory, so the panel gives the menu steps for erasing
  it on the counter, and says whether everything on it is already stored on the Mac.

### Counter settings

The same options as the counter's own menus, read from the counter when it connects:

| Group | Settings |
|---|---|
| Sound and alarm | Speaker, LED, alarm on/off, whether it triggers on CPM or µSv/h, and the level |
| Data saving | What it saves to memory (off, every second/minute/hour, or above a threshold), and the threshold |
| Display and power | Contrast, backlight level and timer, what large-font mode shows, power saving, motion detection, screen rotation, reverse display, battery type |
| Calibration | The three CPM ↔ µSv/h points behind every dose-rate reading |

Change any of them, then **Save to counter**. Nothing reaches the counter until you save,
and only the settings you changed are written.

Calibration is harder to change on purpose, because it changes every dose rate, past
readings included:
- **Locked by default.** Its fields are locked until you click **Change calibration…**,
  read why you probably shouldn't, and confirm you've measured against a reference source.
- **Confirmed on save.** Saving then shows each point's old and new values for a final
  confirmation.
- **Baseline.** The first time Geiger reads a counter, it keeps that counter's calibration
  as its baseline, by serial number, and never overwrites it. **Restore baseline** puts it
  back in one click.
- **API check.** A calibration change sent to the API without `"confirm_calibration": true`
  is refused. **Read from counter** picks up anything
changed with its buttons since it connected. Saving pauses the live reading for about five
seconds. The counter records those seconds in its own memory, so nothing is lost.

Contrast and backlight level are limited to the ranges the counter's own menu allows (10–40
and 0–10), so the screen can't be set too faint or dark to read. Baud rate is left out,
because changing it would cut the USB connection. The clock is kept in step automatically.

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
| `demo` | Run the dashboard with a simulated counter and made-up history, in a temporary database |
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
- `meta`: the counter's details, settings, a record of the last settings change (old and
  new block), calibration and each counter's baseline calibration, measured clock offsets,
  and the last sync result.

Next to it, `data/counter-memory.bin` is the counter's memory exactly as read at the last
sync.

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
- **The counter's memory is a ring buffer.** Until it first fills, reading stops at the
  first blank page. Once it has wrapped, its last page holds data. Then every page is read,
  and the newest and oldest stretches, separated by the erased gap the counter keeps just
  ahead of where it writes, are parsed separately.
- **Settings** live in a 256-byte block. GQ's protocol only lets you erase the whole block
  and rewrite it byte by byte, so a save goes like this:
  1. Read the current block.
  2. Change only the edited fields; every other byte is written back exactly as read,
     including ones whose meaning isn't known.
  3. Record both the old and new blocks in the database.
  4. Erase, rewrite, and apply.
  5. Read the block back and compare it, retrying once if needed.
  6. Mark the record done.

  If a save is ever cut short, say by unplugging mid-save, the open record is settled
  straight away or at the next connect. If the new block fully landed, it's applied once
  more; otherwise the old block is put back. This has happened on real hardware and worked.
  The layout comes from GQ's firmware and was checked field by field against this
  counter ([geiger/settings.py](geiger/settings.py)).

The GMC-320S has quirks that the code works around; they're documented at the top of
[geiger/device.py](geiger/device.py):
- It ignores a command sent right after another.
- It sometimes ignores "stop heartbeat" and keeps streaming, so stopping waits for a
  second of silence and resends until it gets one.
- Its history pages carry a stray extra byte. pygmc's own reader sometimes misaligns on it.
- The top bits of its heartbeat words are status flags.
- About a second after a settings change, it rewrites a few bytes of the block itself: its
  power state, its highest CPM, and a "last saved" timestamp.

### API

The dashboard is a static page over a small local API:

| Endpoint | Returns |
|---|---|
| `GET /api/status` | Logger state, counter details, calibration, latest reading, last sync |
| `GET /api/series?start=&end=&points=&tz=` | Chart points plus stats for a range (unix seconds) |
| `GET /api/recent?seconds=` | Per-second counts for the last few minutes |
| `GET /api/events` | Server-sent events: `reading` each second, `status` on changes |
| `GET /api/export.csv?start=&end=` | Per-minute counts, CPM, and µSv/h |
| `GET /api/settings` | The counter's settings, as last read |
| `GET /api/counter-memory.csv` | Every record in the counter's memory at the last sync |
| `GET /api/counter-memory.bin` | The raw memory image from the last sync |
| `POST /api/sync` | Start copying the counter's records |
| `POST /api/settings` | Write `{"changes": {setting: value}}` to the counter; returns its settings afterward. Calibration changes also need `"confirm_calibration": true` |
| `POST /api/calibration/restore` | Put the counter's calibration back to its saved baseline |
| `POST /api/settings/refresh` | Read the settings from the counter again |

POST requests need the header `X-Geiger: 1`, so other web pages can't send them.

## Development

```bash
uv run pytest
```

To regenerate the image at the top of this README after changing the dashboard, run the
script below. It needs Google Chrome and Node 22 or later, starts `geiger demo`, captures
the page in dark mode at 2x, and frames the captures with
`scripts/readme-image/collage.html`.

```bash
node scripts/readme-image/make.mjs
```

| Path | Contents |
|---|---|
| `geiger/device.py` | Serial access through pygmc, and the device quirks |
| `geiger/logger.py` | The worker thread: live stream, backfill, clock setting |
| `geiger/history.py` | Parsing the counter's memory and mapping its clock to real time |
| `geiger/settings.py` | The counter's settings layout, validation, and safe edits |
| `geiger/store.py` | SQLite schema, gap-only import, chart series and stats |
| `geiger/calibration.py` | CPM to µSv/h, matching pygmc's conversion |
| `geiger/demo.py` | The simulated counter and made-up history behind `geiger demo` |
| `geiger/server.py`, `geiger/cli.py` | HTTP API and command line |
| `geiger/static/` | The dashboard (plain HTML, CSS and JavaScript, no build step) |
| `scripts/readme-image/` | Generates `docs/dashboard.png` |

## License

[MIT](LICENSE). Built on [pygmc](https://github.com/Wikilicious/pygmc), also MIT.
