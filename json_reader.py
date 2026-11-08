"""Read an iFit activity-log JSON saved from the iFit website (browser DevTools)."""
from __future__ import annotations

import json
from pathlib import Path


def _find_log(obj):
    """Depth-first search for the dict that holds start/end + per-second stats."""
    if isinstance(obj, dict):
        if "start" in obj and isinstance(obj.get("stats"), dict):
            return obj
        for v in obj.values():
            r = _find_log(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_log(v)
            if r:
                return r
    return None


def _find_title(obj, depth=0):
    if depth > 4 or not isinstance(obj, (dict, list)):
        return None
    items = obj.values() if isinstance(obj, dict) else obj
    if isinstance(obj, dict):
        for k in ("title", "workout_title", "name"):
            if isinstance(obj.get(k), str) and obj[k].strip():
                return obj[k].strip()
    for v in items:
        t = _find_title(v, depth + 1)
        if t:
            return t
    return None


def _ms(v) -> int:
    if isinstance(v, str):
        from datetime import datetime
        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
    v = int(v)
    return v * 1000 if v < 10**12 else v


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    log = _find_log(data)
    if not log:
        raise ValueError(f"{path.name}: no iFit activity log (start + stats) found in JSON")
    start = _ms(log["start"])
    end = _ms(log["end"]) if log.get("end") else start + int(float(log.get("duration", 0)) * 1000)
    title = _find_title(log) or _find_title(data) or path.stem
    return {
        "start": start, "end": end,
        "type": log.get("type") or log.get("equipment_type") or "",
        "title": title,
        "summary": log.get("summary") or {},
        "stats": log["stats"],
    }
