"""Turn an iFit activity log into a Garmin-friendly FIT activity file."""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (Activity, Event, EventType, FileType,
                                          Manufacturer, Sport, SubSport)

# iFit stat series name -> our field. Series are [{"offset": ms, "value": x}, ...]
SERIES = {
    "bpm": "hr", "mps": "speed", "watts": "power", "rpm": "cadence",
    "incline": "grade", "meters": "distance", "calories": "calories",
    "elevation": "altitude",
}


@dataclass
class Summary:
    sport: str            # "run" or "bike"
    start_ms: int
    end_ms: int
    records: int
    distance_m: float
    calories: float
    avg_hr: int | None
    max_hr: int | None
    avg_power: int | None
    series_found: list[str]


def detect_sport(log: dict, workout: dict) -> str:
    hay = " ".join(str(x) for x in (
        log.get("type"), log.get("equipment_type"), log.get("equipmentType"),
        workout.get("type"), workout.get("equipment_type"), workout.get("equipmentType"),
        workout.get("category"))).lower()
    if hay.split()[:1] and hay.split()[0] in ("strength", "workout"):
        return hay.split()[0]
    if any(k in hay for k in ("bike", "cycl", "ride", "spin")):
        return "bike"
    if any(k in hay for k in ("run", "tread", "walk", "incline", "hike")):
        return "run"
    stats = log.get("stats", {})
    # Treadmills report steps/incline without rpm; bikes report rpm/watts.
    return "bike" if stats.get("rpm") and not log.get("summary", {}).get("total_steps") else "run"


def _series(stats: dict) -> dict[str, list[tuple[int, float]]]:
    out = {}
    for key, name in SERIES.items():
        pts = stats.get(key)
        if isinstance(pts, list) and pts:
            out[name] = sorted((int(p["offset"]), float(p["value"]))
                               for p in pts if p.get("value") is not None)
    return out


def _at(series: list[tuple[int, float]], t: int) -> float | None:
    """Last known value at offset t (step function)."""
    i = bisect.bisect_right(series, (t, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


def build_fit(log: dict, workout: dict, path: str, sport: str | None = None) -> Summary:
    sport = sport or detect_sport(log, workout)
    start_ms = int(log["start"])
    end_ms = int(log["end"])
    if start_ms < 10**12:  # seconds -> ms safety
        start_ms, end_ms = start_ms * 1000, end_ms * 1000
    s = _series(log.get("stats", {}))
    summ = log.get("summary", {})

    # One record per second.
    recs, dist, hrs, pwrs = [], 0.0, [], []
    for off in range(0, end_ms - start_ms + 1, 1000):
        r = RecordMessage()
        r.timestamp = start_ms + off
        hr = _at(s["hr"], off) if "hr" in s else None
        if hr and hr > 0:
            r.heart_rate = int(hr)
            hrs.append(int(hr))
        spd = _at(s["speed"], off) if "speed" in s else None
        if spd is not None:
            r.speed = spd
            dist += spd  # integrate 1 s
        if "distance" in s:
            d = _at(s["distance"], off)
            if d is not None:
                dist = d
        r.distance = dist
        if "power" in s:
            p = _at(s["power"], off)
            if p is not None:
                r.power = int(p)
                pwrs.append(int(p))
        if "cadence" in s:
            c = _at(s["cadence"], off)
            if c is not None:
                r.cadence = int(c)
        if "grade" in s:
            g = _at(s["grade"], off)
            if g is not None:
                r.grade = g
        if "altitude" in s:
            a = _at(s["altitude"], off)
            if a is not None:
                r.altitude = a
        recs.append(r)

    total_m = float(summ.get("total_meters") or dist)
    cals = float(summ.get("total_calories") or 0)
    elapsed = (end_ms - start_ms) / 1000.0
    fsport, fsub = {
        "bike": (Sport.CYCLING, SubSport.INDOOR_CYCLING),
        "strength": (Sport.TRAINING, SubSport.STRENGTH_TRAINING),
        "workout": (Sport.TRAINING, SubSport.GENERIC),
    }.get(sport, (Sport.RUNNING, SubSport.TREADMILL))

    b = FitFileBuilder(auto_define=True, min_string_size=50)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.serial_number = 0x1F17
    fid.time_created = start_ms
    b.add(fid)

    ev = EventMessage()
    ev.event, ev.event_type, ev.timestamp = Event.TIMER, EventType.START, start_ms
    b.add(ev)
    b.add_all(recs)
    ev2 = EventMessage()
    ev2.event, ev2.event_type, ev2.timestamp = Event.TIMER, EventType.STOP_ALL, end_ms
    b.add(ev2)

    def fill(m):
        m.timestamp = end_ms
        m.start_time = start_ms
        m.total_elapsed_time = elapsed
        m.total_timer_time = elapsed
        m.total_distance = total_m
        if cals:
            m.total_calories = int(cals)
        if hrs:
            m.avg_heart_rate = round(sum(hrs) / len(hrs))
            m.max_heart_rate = max(hrs)
        if pwrs:
            m.avg_power = round(sum(pwrs) / len(pwrs))
            m.max_power = max(pwrs)
        m.sport, m.sub_sport = fsport, fsub
        return m

    lap = fill(LapMessage())
    lap.event, lap.event_type = Event.LAP, EventType.STOP
    b.add(lap)
    ses = fill(SessionMessage())
    ses.event, ses.event_type = Event.SESSION, EventType.STOP
    ses.first_lap_index, ses.num_laps = 0, 1
    b.add(ses)

    act = ActivityMessage()
    act.timestamp = end_ms
    act.total_timer_time = elapsed
    act.num_sessions = 1
    act.type = Activity.MANUAL
    act.event, act.event_type = Event.ACTIVITY, EventType.STOP
    b.add(act)

    b.build().to_file(path)
    return Summary(sport, start_ms, end_ms, len(recs), total_m, cals,
                   ses.avg_heart_rate if hrs else None, max(hrs) if hrs else None,
                   ses.avg_power if pwrs else None, sorted(s))
