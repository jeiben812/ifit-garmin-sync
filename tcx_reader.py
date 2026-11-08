"""Read iFit TCX exports into the dict shape fit_builder.build_fit() expects.

Output:
  {"start": ms, "end": ms, "type": "run"|"bike"|..., "title": str,
   "summary": {"total_meters": m, "total_calories": kcal},
   "stats": {"bpm": [{"offset": ms, "value": x}], "mps": [...], "watts": [...],
             "rpm": [...], "meters": [...], "elevation": [...]}}
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from lxml import etree


def _ln(el) -> str:
    return etree.QName(el).localname


def _child(el, name):
    for c in el:
        if _ln(c) == name:
            return c
    return None


def _num(el) -> float | None:
    if el is None or el.text is None:
        return None
    try:
        return float(el.text.strip())
    except ValueError:
        return None


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp() * 1000)


def read_tcx(path: Path) -> dict:
    root = etree.parse(str(path), etree.XMLParser(recover=True, remove_blank_text=True)).getroot()
    act = next((e for e in root.iter() if _ln(e) == "Activity"), None)
    if act is None:
        raise ValueError(f"{path.name}: no <Activity> in TCX")

    sport_attr = (act.get("Sport") or "").lower()
    notes = _child(act, "Notes")
    title = (notes.text.strip() if notes is not None and notes.text else "") or \
        path.stem.replace("_", " ").replace("-", " ").strip()

    stats: dict[str, list] = {k: [] for k in ("bpm", "mps", "watts", "rpm", "meters", "elevation")}
    start = end = None
    total_m = cals = 0.0
    lap_secs = 0.0

    for lap in (e for e in act.iter() if _ln(e) == "Lap"):
        if start is None and lap.get("StartTime"):
            start = _ms(lap.get("StartTime"))
        lap_secs += _num(_child(lap, "TotalTimeSeconds")) or 0
        total_m += _num(_child(lap, "DistanceMeters")) or 0
        cals += _num(_child(lap, "Calories")) or 0

    points = []
    for tp in (e for e in act.iter() if _ln(e) == "Trackpoint"):
        t = _child(tp, "Time")
        if t is None or not t.text:
            continue
        points.append((_ms(t.text), tp))
    points.sort(key=lambda p: p[0])
    if points:
        start = min(start or points[0][0], points[0][0])
        end = points[-1][0]
    if start is None:
        raise ValueError(f"{path.name}: no start time found")
    end = max(end or 0, start + int(lap_secs * 1000))

    for ts, tp in points:
        off = ts - start
        def add(key, val):
            if val is not None:
                stats[key].append({"offset": off, "value": val})
        hr = _child(tp, "HeartRateBpm")
        add("bpm", _num(_child(hr, "Value")) if hr is not None else None)
        add("meters", _num(_child(tp, "DistanceMeters")))
        add("elevation", _num(_child(tp, "AltitudeMeters")))
        add("rpm", _num(_child(tp, "Cadence")))
        ext = _child(tp, "Extensions")
        if ext is not None:
            for e in ext.iter():
                n = _ln(e).lower()
                if n == "speed":
                    add("mps", _num(e))
                elif n == "watts":
                    add("watts", _num(e))
                elif n == "runcadence":
                    add("rpm", _num(e))

    # Fallbacks when TCX lacks summary values
    if not total_m and stats["meters"]:
        total_m = stats["meters"][-1]["value"]
    # Derive speed from distance if no explicit speed series
    if not stats["mps"] and len(stats["meters"]) > 1:
        m = stats["meters"]
        for a, b in zip(m, m[1:]):
            dt = (b["offset"] - a["offset"]) / 1000
            if dt > 0:
                stats["mps"].append({"offset": b["offset"],
                                     "value": max(0.0, (b["value"] - a["value"]) / dt)})

    if "bik" in sport_attr or "cycl" in sport_attr:
        typ = "bike"
    elif "run" in sport_attr:
        typ = "run"
    else:
        typ = "other"

    return {
        "start": start, "end": end, "type": typ, "title": title,
        "summary": {"total_meters": total_m, "total_calories": cals},
        "stats": {k: v for k, v in stats.items() if v},
    }
