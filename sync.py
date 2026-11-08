"""iFit -> Garmin Connect, via Strava (official API) and/or iFit TCX exports.

Drop iFit TCX exports (or the bulk-export .zip) into the inbox folder, then:
  python sync.py --strava-login     # one-time Strava approval (browser)
  python sync.py --dry-run          # fetch + build FIT files, no upload
  python sync.py --garmin-login     # one-time interactive Garmin login (MFA prompt)
  python sync.py                    # upload everything new in the inbox
  python sync.py --watch 60         # keep running, check inbox every 60 s
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from fit_builder import build_fit
from tcx_reader import read_tcx
from json_reader import read_json

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
INBOX = Path(os.getenv("INBOX_DIR", ROOT / "inbox"))
for d in (DATA / "fit", DATA / "processed", DATA / "failed", INBOX):
    d.mkdir(parents=True, exist_ok=True)


_errors: list[str] = []          # errors in the current cycle (for the heartbeat)
_alerted: set[str] = set()       # failure alerts already sent (avoid repeat spam)


def notify(title: str, msg: str, priority: str = "default") -> None:
    url = os.getenv("NTFY_URL")
    if not url:
        return
    headers = {"Title": title, "Priority": priority, "Tags": "runner"}
    if os.getenv("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    try:
        requests.post(url, data=msg.encode(), timeout=10, headers=headers)
    except requests.RequestException:
        pass


def fail(where: str, e: Exception) -> None:
    """Log an error; alert once per distinct error until it clears."""
    msg = f"{where}: {e}"
    print(f"!! {msg}", file=sys.stderr, flush=True)
    _errors.append(msg)
    if msg not in _alerted:
        _alerted.add(msg)
        notify("iFit to Garmin failed", msg, "high")


def heartbeat() -> None:
    """Report cycle health to a Gatus external endpoint, if configured."""
    url = os.getenv("GATUS_PUSH_URL")
    if not url:
        return
    params = {"success": "false" if _errors else "true"}
    if _errors:
        params["error"] = _errors[0][:200]
    headers = {}
    if os.getenv("GATUS_PUSH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GATUS_PUSH_TOKEN']}"
    try:
        requests.post(url, params=params, headers=headers, timeout=10)
    except requests.RequestException:
        pass


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DATA / "state.db")
    c.execute("""CREATE TABLE IF NOT EXISTS synced(
        key TEXT PRIMARY KEY, garmin_id TEXT, title TEXT, sport TEXT,
        started TEXT, source TEXT, synced_at TEXT)""")
    return c


def garmin(interactive: bool = False):
    from garminconnect import Garmin
    g = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"),
               prompt_mfa=(lambda: input("Garmin MFA code: ")) if interactive else None)
    g.login(str(DATA / "garmin_tokens"))
    return g


def find_by_start(g, start_ms: int, tries: int = 6) -> str | None:
    """Locate a Garmin activity by start time (uploads are processed async)."""
    day = datetime.utcfromtimestamp(start_ms / 1000)
    lo = (day.date()).isoformat()
    hi = (day.date().fromordinal(day.date().toordinal() + 1)).isoformat()
    for i in range(tries):
        for act in g.get_activities_by_date(lo, hi) or []:
            gmt = act.get("startTimeGMT")
            if gmt:
                t = datetime.fromisoformat(gmt).replace(tzinfo=None)
                if abs((t - day).total_seconds()) <= 90:
                    return str(act["activityId"])
        time.sleep(5)
    return None


def upload(g, path: Path, start_ms: int) -> str:
    try:
        resp = g.upload_activity(str(path))
    except Exception as e:
        if "409" in str(e) or "duplicate" in str(e).lower():
            gid = find_by_start(g, start_ms, tries=1)
            if gid:
                return gid
        raise
    body = resp.json() if hasattr(resp, "json") else resp
    res = (body or {}).get("detailedImportResult", {})
    ok = res.get("successes") or []
    if ok and ok[0].get("internalId"):
        return str(ok[0]["internalId"])
    fails = res.get("failures") or []
    if fails and "duplicate" not in str(fails).lower():
        raise RuntimeError(f"Garmin upload rejected: {fails}")
    # Accepted for async processing, or duplicate: find the activity by start time.
    gid = find_by_start(g, start_ms)
    if not gid:
        raise RuntimeError(f"Upload accepted but activity not found yet: {body}")
    return gid


def expand_zips() -> None:
    for z in INBOX.glob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if n.lower().endswith((".tcx", ".json")) and not n.startswith("__MACOSX"):
                    target = INBOX / Path(n).name
                    if not target.exists():
                        target.write_bytes(zf.read(n))
        shutil.move(str(z), DATA / "processed" / z.name)


def push(a, g_holder, con, log: dict, key: str, src: str, stem: str) -> bool:
    """Build FIT for one workout and upload it. Returns True if uploaded."""
    if con.execute("SELECT 1 FROM synced WHERE key IN (?,?)",
                   (key, f"start:{log['start']}")).fetchone():
        return False
    fit = DATA / "fit" / (stem + ".fit")
    s = build_fit(log, {"title": log["title"]}, str(fit), a.sport)
    when = datetime.fromtimestamp(s.start_ms / 1000).strftime("%Y-%m-%d %H:%M")
    name = f"iFit: {log['title']}"[:100]
    print(f"- {src}\n    {when} | {name} | {s.sport} | "
          f"{(s.end_ms - s.start_ms)//60000} min | {s.distance_m/1609.34:.2f} mi | "
          f"{s.calories:.0f} kcal | HR avg {s.avg_hr} max {s.max_hr} | "
          f"power avg {s.avg_power} | data: {', '.join(s.series_found)}")
    if a.dry_run:
        return False
    if not g_holder:
        g_holder.append(garmin())
    g = g_holder[0]
    gid = upload(g, fit, s.start_ms)
    g.set_activity_name(gid, name)
    desc = log.get("description") or ""
    g.set_activity_description(gid, (desc + "\n\n" if desc else "") + f"Synced from {src}.")
    for k in (key, f"start:{log['start']}"):
        con.execute("INSERT OR REPLACE INTO synced VALUES(?,?,?,?,?,?,datetime('now'))",
                    (k, gid, name, s.sport, when, src))
    con.commit()
    print(f"    -> Garmin activity {gid}")
    notify("iFit to Garmin", f"{name}\n{when}, {s.distance_m/1609.34:.2f} mi")
    return True


def run_strava(a, g_holder: list) -> int:
    from strava_client import StravaClient, is_ifit
    sc = StravaClient(os.getenv("STRAVA_CLIENT_ID"), os.getenv("STRAVA_CLIENT_SECRET"),
                      DATA / "strava_tokens.json")
    con = db()
    since = int(time.time()) - 86400 * int(os.getenv("STRAVA_LOOKBACK_DAYS", "3"))
    match = os.getenv("STRAVA_MATCH", "ifit")
    count = 0
    for act in sorted(sc.recent(since), key=lambda x: x.get("start_date", "")):
        key = f"strava:{act['id']}"
        if con.execute("SELECT 1 FROM synced WHERE key=?", (key,)).fetchone():
            continue
        try:
            detail = sc.get(f"/activities/{act['id']}")
            if not is_ifit(detail, match):
                if a.dry_run or a.verbose:
                    print(f"  (skip non-iFit Strava activity {act['id']}: "
                          f"{detail.get('name')!r}, device={detail.get('device_name')!r}, "
                          f"external_id={detail.get('external_id')!r})")
                continue
            log = sc.to_log(act)
            if push(a, g_holder, con, log, key, f"Strava activity {act['id']}", f"strava_{act['id']}"):
                count += 1
        except Exception as e:
            g_holder.clear()  # force a fresh Garmin session next time
            fail(f"Strava {act['id']}", e)
    return count


def run_once(a, g_holder: list) -> int:
    _errors.clear()
    n = 0
    if os.getenv("STRAVA_CLIENT_ID") and a.source in ("all", "strava"):
        try:
            n += run_strava(a, g_holder)
        except Exception as e:
            fail("Strava", e)
    if a.source in ("all", "inbox"):
        try:
            n += run_inbox(a, g_holder)
        except Exception as e:
            fail("Inbox", e)
    if not _errors:
        _alerted.clear()  # healthy again: re-arm alerts
    if not a.dry_run:
        heartbeat()
    return n


def run_inbox(a, g_holder: list) -> int:
    expand_zips()
    files = sorted([*INBOX.glob("*.tcx"), *INBOX.glob("*.json")], key=lambda p: p.stat().st_mtime)
    if not files:
        return 0
    con = db()
    count = 0
    for f in files:
        try:
            log = read_json(f) if f.suffix.lower() == ".json" else read_tcx(f)
            if push(a, g_holder, con, log, f"file:{f.name}", f"iFit export {f.name}", f.stem):
                count += 1
            elif not a.dry_run:
                print(f"= {f.name}: already synced, skipping")
            if not a.dry_run:
                shutil.move(str(f), DATA / "processed" / f.name)
        except Exception as e:
            g_holder.clear()
            fail(f.name, e)
            if not a.dry_run:
                shutil.move(str(f), DATA / "failed" / f.name)
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build FIT files, don't upload")
    ap.add_argument("--garmin-login", action="store_true", help="interactive Garmin login only")
    ap.add_argument("--sport", choices=["run", "bike", "strength", "workout"], help="force sport type for this run")
    ap.add_argument("--watch", type=int, metavar="SECS", help="poll forever")
    ap.add_argument("--strava-login", action="store_true", help="one-time Strava approval")
    ap.add_argument("--source", choices=["all", "strava", "inbox"], default="all")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.strava_login:
        from strava_client import StravaClient
        sc = StravaClient(os.getenv("STRAVA_CLIENT_ID"), os.getenv("STRAVA_CLIENT_SECRET"),
                          DATA / "strava_tokens.json")
        sc.interactive_login()
        ath = sc.tokens.get("athlete") or {}
        print("Strava login OK as", ath.get("firstname", ""), ath.get("lastname", ""))
        return 0

    if a.garmin_login:
        g = garmin(interactive=True)
        print("Garmin login OK as", g.get_full_name())
        return 0

    g_holder: list = []
    if a.watch:
        print(f"Watching Strava + {INBOX} every {a.watch}s", flush=True)
        while True:
            try:
                n = run_once(a, g_holder)
                if n:
                    print(f"{datetime.now():%Y-%m-%d %H:%M} uploaded {n}", flush=True)
            except Exception as e:  # never let the loop die
                fail("cycle", e)
            time.sleep(a.watch)
    n = run_once(a, g_holder)
    print("Dry run done; FIT files in data/fit." if a.dry_run else f"Done. {n} uploaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
