# ifit-garmin-sync

Automatically copies iFit workouts (NordicTrack / ProForm treadmills, bikes, strength classes) into **Garmin Connect** as proper activities with the right sport type, heart rate, speed, distance, incline, power, and the iFit workout title and trainer description.

iFit has no public API and no Garmin integration. This tool uses the one path that is officially supported on both ends:

```
iFit ──(built-in Strava sync)──► Strava ──(official API)──► ifit-garmin-sync ──► Garmin Connect
                                                            │
                                     iFit TCX / JSON files ─┘  (optional manual inbox)
```

## Features

- **Polls Strava** for new activities that came from iFit (matched by device name, e.g. `iFIT`); your own Garmin/Zwift/etc. activities on Strava are ignored, so nothing loops back.
- **Builds a real FIT file** at 1-second resolution:
  - Treadmill → *Running / Treadmill*
  - Bike → *Cycling / Indoor Cycling*
  - Strength classes → *Training / Strength Training*
  - Other iFit classes → *Training*
- **Names the Garmin activity** `iFit: <workout title>` and copies the iFit description (including the trainer).
- **No duplicates:** tracks synced workouts in SQLite by Strava ID *and* start time; Garmin's own 409 "duplicate" response is handled too.
- **Inbox folder:** drop iFit `.tcx` exports, iFit activity-log `.json`, or a bulk export `.zip` and they are processed the same way.
- **Notifications** via [ntfy](https://ntfy.sh) for each synced workout, and **failure alerts** sent once per distinct error (re-armed after recovery).
- **Health heartbeat** to a [Gatus](https://github.com/TwiN/gatus) external endpoint each cycle.
- Runs as a small, non-root Docker container.

## Requirements

- An iFit account with **Strava connected** (iFit app → Settings → Connected Apps).
- A free **Strava API application**: <https://www.strava.com/settings/api>
  - Website: `http://localhost` · **Authorization Callback Domain:** `localhost`
- A Garmin Connect account.
- Python 3.12+ (for the one-time logins) and Docker (to run it as a service).

## Quick start (local)

```bash
git clone https://github.com/<you>/ifit-garmin-sync.git
cd ifit-garmin-sync
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
cp .env.example .env                              # then fill in the values
```

One-time logins (tokens are saved in `data/` and refresh themselves):

```bash
python sync.py --strava-login    # opens a browser to approve Strava read access
python sync.py --garmin-login    # prompts for an MFA code if your account uses one
```

Test without uploading, then sync for real:

```bash
python sync.py --dry-run -v      # shows what would be uploaded and which data series were found
python sync.py                   # upload new workouts
```

## Configuration (`.env` / environment)

| Variable | Required | Description |
|---|---|---|
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` | yes | From your Strava API application |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | first login | Only needed until a Garmin token is saved |
| `STRAVA_MATCH` | no | Comma-separated text that identifies iFit activities (default `ifit`) |
| `STRAVA_LOOKBACK_DAYS` | no | How far back to look each cycle (default `3`) |
| `NTFY_URL`, `NTFY_TOKEN` | no | ntfy topic URL (e.g. `https://ntfy.example.com/fitness`) and access token |
| `GATUS_PUSH_URL`, `GATUS_PUSH_TOKEN` | no | Gatus external-endpoint URL and token for the heartbeat |
| `DATA_DIR` | no | Tokens, `state.db`, generated FIT files (default `./data`, `/data` in Docker) |
| `INBOX_DIR` | no | Folder watched for `.tcx` / `.json` / `.zip` (default `./inbox`, `/data/inbox` in Docker) |
| `SYNC_INTERVAL` | no | Docker only: seconds between cycles (default `900`) |

## Command-line options

```
python sync.py [--dry-run] [--watch SECS] [--source all|strava|inbox]
               [--sport run|bike|strength|workout] [-v]
               [--strava-login] [--garmin-login]
```

## Running with Docker

1. Do the one-time logins locally (above) so `data/` contains `garmin_tokens/` and `strava_tokens.json`.
2. Copy `data/` to the host (e.g. `/srv/ifit-garmin-sync/data`) and make it owned by uid `1000`.
3. Add the service to your compose file. See [`deploy/compose-snippet.yml`](deploy/compose-snippet.yml):

```yaml
  ifit-garmin-sync:
    build: ./ifit-garmin-sync
    image: ifit-garmin-sync:local
    pull_policy: build
    container_name: ifit-garmin-sync
    restart: unless-stopped
    environment:
      TZ: America/New_York
      STRAVA_CLIENT_ID: ${STRAVA_CLIENT_ID}
      STRAVA_CLIENT_SECRET: ${STRAVA_CLIENT_SECRET}
      NTFY_URL: http://ntfy/fitness
      NTFY_TOKEN: ${NTFY_TOKEN}
    volumes:
      - /srv/ifit-garmin-sync/data:/data
```

`pull_policy: build` matters for tools like Komodo or Portainer that run `compose pull` before deploying: the image only exists locally.

Monitoring examples (ntfy write-only user, Gatus heartbeat) are in [`deploy/monitoring-snippets.md`](deploy/monitoring-snippets.md).

## How it works

| File | Role |
|---|---|
| `sync.py` | CLI, polling loop, de-duplication (SQLite), Garmin upload, naming, ntfy/Gatus |
| `strava_client.py` | Strava OAuth (browser approval + refresh) and activity/stream download |
| `fit_builder.py` | Converts per-second series into a FIT activity (records, lap, session) with `fit-tool` |
| `tcx_reader.py` | Reads iFit TCX exports |
| `json_reader.py` | Reads iFit activity-log JSON |

Garmin processes uploads asynchronously and often returns no activity ID, so the tool finds the new activity by its start time before renaming it.

## Limitations

- **Depends on iFit's Strava sync.** If iFit stops sending to Strava, nothing arrives. iFit only sends workouts completed *after* Strava is connected.
- **Data is only as good as what iFit sends.** Strava streams from iFit may not include incline or power for every machine; strength classes usually have no distance.
- **Garmin training metrics** (Training Effect, Load) may be limited on uploaded files compared with recording on a Garmin device.
- Uses the unofficial [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) library; Garmin login changes can break it until the library is updated.
- Heart rate: if you broadcast HR from a Garmin watch to the iFit console, iFit records it and it comes through here; there is no separate watch recording to merge.

## Not supported (by design)

Logging in to iFit directly. iFit's website blocks scripted logins, and this project does not try to get around that.

## License

MIT
