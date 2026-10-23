"""Logger and live dashboard for GQ GMC-320-series Geiger counters."""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .device import Counter, DeviceError
from .logger import Hub, Logger, correct_clock, describe, sync_history
from .server import DashboardServer
from .store import Store

log = logging.getLogger("geiger")

AGENT_LABEL = "local.geiger"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("GEIGER_DB", "data/geiger.db"),
        help="SQLite database path (default: data/geiger.db, or $GEIGER_DB)",
    )
    common.add_argument(
        "--device",
        default=os.environ.get("GEIGER_DEVICE"),
        help="serial port, e.g. /dev/cu.usbserial-210 (default: auto-detect)",
    )
    common.add_argument(
        "--http-port",
        type=int,
        default=int(os.environ.get("GEIGER_HTTP_PORT", 8320)),
        help="dashboard port (default: 8320)",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    parser = argparse.ArgumentParser(prog="geiger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[common], help="log continuously and serve the dashboard")
    sync = sub.add_parser(
        "sync", parents=[common], help="import the counter's flash history into gaps, then exit"
    )
    sub.add_parser("info", parents=[common], help="show the counter's details and current reading")
    sub.add_parser("set-clock", parents=[common], help="set the counter's clock to this Mac's time")
    agent = sub.add_parser(
        "install-agent", parents=[common], help="run the logger in the background at login (launchd)"
    )
    sub.add_parser("uninstall-agent", parents=[common], help="stop and remove the launchd agent")
    for p in (run, agent):
        p.add_argument(
            "--host", default="127.0.0.1", help="interface to serve on; 0.0.0.0 for your network"
        )
        p.add_argument(
            "--no-backfill", action="store_true", help="don't fill gaps from the counter's history"
        )
    for p in (run, agent, sync):
        p.add_argument(
            "--no-set-clock",
            action="store_true",
            help="leave the counter's clock alone (by default it's kept at this Mac's local time)",
        )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("pygmc").setLevel(logging.ERROR)
        if args.command != "run":  # one-shot commands print their own summary
            logging.getLogger("geiger.logger").setLevel(logging.WARNING)
    try:
        sys.exit(COMMANDS[args.command](args))
    except DeviceError as e:
        sys.exit(f"geiger: {e}")


def cmd_run(args) -> int:
    store = Store(args.db)
    hub = Hub()
    logger = Logger(
        store, hub, port=args.device, backfill=not args.no_backfill, set_clock=not args.no_set_clock
    )
    # bind before touching the counter so a port clash fails cleanly
    try:
        server = DashboardServer((args.host, args.http_port), store, logger, hub)
    except OSError as e:
        sys.exit(f"geiger: can't serve on port {args.http_port} ({e.strerror}); "
                 "is another logger running? Use --http-port to pick another.")
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    logger.start()
    log.info("dashboard: http://%s:%d", "localhost" if args.host == "127.0.0.1" else args.host,
             args.http_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.stop()
        logger.join(timeout=10)
        server.server_close()
    return 0


def cmd_sync(args) -> int:
    if _api(args, "/api/sync", post=True) is not None:
        print("The running logger is syncing; watch progress on the dashboard.")
        return 0
    store = Store(args.db)
    counter = Counter(args.device)
    try:
        describe(counter, store)

        def progress(done, total):
            print(f"\rreading flash: {done // 1024} KiB", end="", flush=True)

        result = sync_history(counter, store, progress if sys.stdout.isatty() else None)
        if sys.stdout.isatty():
            print()
        if result["error"]:
            print(f"sync failed: {result['error']}")
            return 1
        print(
            f"{result['records']} records in {result['duration']} s; "
            f"{result['imported']} imported, {result['skipped']} already stored or skipped"
        )
        # same as the logger: fix the clock only once its history is safely imported
        change = None if args.no_set_clock else correct_clock(counter, store)
        if change:
            print(f"set the counter's clock: was {-change[0]:+.1f} s from local time")
    finally:
        counter.close()
    return 0


def cmd_info(args) -> int:
    status = _api(args, "/api/status")
    if status is not None:
        device, reading = status["device"] or {}, status["reading"] or {}
        print(f"logger:  {status['state']} (http://localhost:{args.http_port})")
        print(f"device:  {device.get('version')}  serial {device.get('serial')}  {device.get('port')}")
        if reading:
            print(f"reading: {reading['cpm']} CPM  {reading['usv_h']} µSv/h")
        return 0
    store = Store(args.db)
    counter = Counter(args.device)
    try:
        calibration, voltage, offset = describe(counter, store)
        cpm = counter.cpm()
        info = counter.info
    finally:
        counter.close()
    print(f"device:      {info.version}  serial {info.serial}")
    print(f"port:        {info.port} @ {info.baudrate} baud")
    if calibration:
        print(f"reading:     {cpm} CPM  {calibration.usv_h(cpm):.3f} µSv/h")
        print("calibration: " + ", ".join(f"{c} CPM = {u:g} µSv/h" for c, u in calibration.points))
    else:
        print(f"reading:     {cpm} CPM (no calibration on the counter)")
    print(f"battery:     {voltage} V" if voltage else "battery:     unknown")
    print(f"clock:       {-offset:+.1f} s vs this Mac's local time")
    return 0


def cmd_set_clock(args) -> int:
    if _api(args, "/api/status") is not None:
        print("The logger is running and holds the serial port; stop it first.")
        return 1
    store = Store(args.db)
    counter = Counter(args.device)
    try:
        describe(counter, store)
        print("importing history recorded under the old clock…")
        if sync_history(counter, store)["error"]:
            print("history sync failed; clock left unchanged")
            return 1
        before, after = correct_clock(counter, store, force=True)
    finally:
        counter.close()
    print(f"counter clock was {-before:+.1f} s from local time; now {-after:+.1f} s")
    return 0


def cmd_install_agent(args) -> int:
    db = Path(args.db).resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    program = [sys.executable, "-m", "geiger", "run", "--db", str(db),
               "--http-port", str(args.http_port), "--host", args.host]
    if args.device:
        program += ["--device", args.device]
    if args.no_backfill:
        program.append("--no-backfill")
    if args.no_set_clock:
        program.append("--no-set-clock")
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": str(Path.cwd()),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(db.parent / "geiger.log"),
        "StandardErrorPath": str(db.parent / "geiger.log"),
    }
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], check=True)
    print(f"installed {AGENT_PLIST}")
    print(f"logging to {db}; log file {db.parent / 'geiger.log'}")
    print(f"dashboard: http://localhost:{args.http_port}")
    return 0


def cmd_uninstall_agent(args) -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    if AGENT_PLIST.exists():
        AGENT_PLIST.unlink()
        print(f"removed {AGENT_PLIST}")
    else:
        print("no agent installed")
    return 0


def _api(args, path: str, post: bool = False):
    """Call a running logger's API; None when no logger is listening."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{args.http_port}{path}",
        method="POST" if post else "GET",
        headers={"X-Geiger": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)
    except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError):
        return None


COMMANDS = {
    "run": cmd_run,
    "sync": cmd_sync,
    "info": cmd_info,
    "set-clock": cmd_set_clock,
    "install-agent": cmd_install_agent,
    "uninstall-agent": cmd_uninstall_agent,
}
