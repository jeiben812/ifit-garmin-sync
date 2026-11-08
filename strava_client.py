"""Official Strava API client: pull iFit-originated activities as fit_builder logs."""
from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

import requests

API = "https://www.strava.com/api/v3"
REDIRECT_PORT = 8765
STREAM_KEYS = "time,heartrate,velocity_smooth,watts,cadence,distance,altitude,grade_smooth"


class StravaError(RuntimeError):
    pass


class StravaClient:
    def __init__(self, client_id: str | None, client_secret: str | None, token_file: Path):
        if not (client_id and client_secret):
            raise StravaError("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET not set in .env")
        self.cid, self.secret, self.token_file = client_id, client_secret, token_file
        self.tokens = json.loads(token_file.read_text()) if token_file.exists() else {}

    # ---------- auth ----------
    def _save(self, data: dict) -> None:
        if "access_token" not in data:
            raise StravaError(f"Strava token error: {data}")
        self.tokens = data
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(data, indent=2))

    def interactive_login(self) -> None:
        """One-time browser approval; catches the redirect on localhost."""
        code_box: dict = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code_box.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Strava authorized. You can close this tab.")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": self.cid, "response_type": "code",
            "redirect_uri": f"http://localhost:{REDIRECT_PORT}/",
            "approval_prompt": "auto", "scope": "activity:read_all"})
        print("Opening browser to approve access. If it doesn't open, visit:\n" + url)
        webbrowser.open(url)
        for _ in range(300):
            if code_box:
                break
            time.sleep(1)
        srv.server_close()
        if "code" not in code_box:
            raise StravaError(f"No authorization code received: {code_box or 'timed out'}")
        r = requests.post("https://www.strava.com/oauth/token", timeout=30, data={
            "client_id": self.cid, "client_secret": self.secret,
            "code": code_box["code"], "grant_type": "authorization_code"})
        self._save(r.json())

    def _token(self) -> str:
        t = self.tokens
        if not t.get("refresh_token"):
            raise StravaError("Not logged in to Strava; run: python sync.py --strava-login")
        if t.get("expires_at", 0) - 120 > time.time():
            return t["access_token"]
        r = requests.post("https://www.strava.com/oauth/token", timeout=30, data={
            "client_id": self.cid, "client_secret": self.secret,
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        data = r.json()
        data.setdefault("athlete", t.get("athlete"))
        self._save(data)
        return data["access_token"]

    def get(self, path: str, **params):
        r = requests.get(f"{API}{path}", params=params, timeout=60,
                         headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 429:
            raise StravaError("Strava rate limit hit; will retry next run")
        if r.status_code >= 400:
            raise StravaError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- data ----------
    def recent(self, after_epoch: int, per_page: int = 30) -> list[dict]:
        return self.get("/athlete/activities", after=after_epoch, per_page=per_page)

    def to_log(self, summary: dict) -> dict:
        aid = summary["id"]
        detail = self.get(f"/activities/{aid}")
        streams = self.get(f"/activities/{aid}/streams", keys=STREAM_KEYS, key_by_type="true")
        start = int(datetime.fromisoformat(
            detail["start_date"].replace("Z", "+00:00")).timestamp() * 1000)
        times = streams.get("time", {}).get("data") or []
        mapping = {"heartrate": "bpm", "velocity_smooth": "mps", "watts": "watts",
                   "cadence": "rpm", "distance": "meters", "altitude": "elevation",
                   "grade_smooth": "incline"}
        stats = {}
        for skey, ikey in mapping.items():
            vals = streams.get(skey, {}).get("data")
            if vals and times:
                stats[ikey] = [{"offset": int(t) * 1000, "value": v}
                               for t, v in zip(times, vals) if v is not None]
        st = (detail.get("sport_type") or detail.get("type") or "").lower()
        if "ride" in st or "cycl" in st:
            typ = "bike"
        elif "run" in st or "walk" in st or "hike" in st:
            typ = "run"
        elif "weight" in st or "strength" in st or "strength" in (detail.get("name") or "").lower():
            typ = "strength"
        else:
            typ = "workout"  # Strava "Workout", yoga, HIIT, etc.
        return {
            "start": start,
            "end": start + int(detail.get("elapsed_time", 0)) * 1000,
            "type": typ,
            "title": detail.get("name") or "iFit Workout",
            "description": detail.get("description") or "",
            "summary": {"total_meters": detail.get("distance") or 0,
                        "total_calories": detail.get("calories") or 0},
            "stats": stats,
        }


def is_ifit(a: dict, match: str) -> bool:
    """True if the Strava activity came from iFit (not Garmin, Zwift, etc.)."""
    hay = " ".join(str(a.get(k) or "") for k in ("device_name", "external_id", "name")).lower()
    return any(m.strip() and m.strip().lower() in hay for m in match.split(","))
