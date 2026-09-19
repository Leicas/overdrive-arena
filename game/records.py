"""Persistent map records, separated by mode, driver type and race length."""
import hashlib
import json
import logging
import math
from pathlib import Path

log = logging.getLogger(__name__)


class Records:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries = {}
        self.error = ""
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
                    raise ValueError("invalid records format")
                for key, entry in data["entries"].items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("car"), str):
                        raise ValueError("invalid record")
                    if not isinstance(entry.get("seconds"), (int, float)) or not math.isfinite(entry["seconds"]) or entry["seconds"] <= 0:
                        raise ValueError("invalid time")
                self.entries = data["entries"]
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self.error = "Records could not be loaded; original file preserved"
                log.warning("%s: %s", self.error, exc)

    @staticmethod
    def map_key(track):
        # Exclude layout caches; identical rescans retain their records.
        pieces = [(p.kind, p.turn, p.reversed, p.cells) for p in track.pieces]
        return hashlib.sha256(json.dumps(pieces).encode()).hexdigest()[:16]

    def key(self, track, mode, driver, kind, laps=0):
        return f"{self.map_key(track)}:{mode}:{driver}:{kind}:{laps}"

    def get(self, track, mode, driver, kind, laps=0):
        return self.entries.get(self.key(track, mode, driver, kind, laps)) if track else None

    def record(self, track, mode, car, kind, seconds, laps=0):
        if not track or not math.isfinite(seconds) or seconds <= 0:
            return
        driver = "human" if car.source is not None else "ai"
        vehicle = getattr(car, "car", None)
        car_id = str(getattr(vehicle, "address", None) or car.name)
        source = car.source
        controller_id = str(getattr(source, "record_id", None) or
                            f"{getattr(source, 'short', 'player')}:{getattr(source, 'name', 'controller')}") if source else "ai"
        controller = f"{getattr(source, 'short', 'Player')} · {getattr(source, 'name', 'Controller')}" if source else "AI"
        entry = {"seconds": seconds, "car": car.name, "car_id": car_id,
                 "driver": car.driver_label(), "controller": controller, "controller_id": controller_id}
        scopes = [(driver, "overall", driver),
                  (f"{driver}-car", "car", car_id)]
        if source is not None:
            scopes += [("human-controller", "controller", controller_id),
                       ("human-pair", "pair", car_id + "|" + controller_id)]
        changed = False
        for category, scope, identity in scopes:
            group = category if scope == "overall" else category + "-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            key = self.key(track, mode, group, kind, laps)
            old = self.entries.get(key)
            if old and old["seconds"] <= seconds:
                continue
            self.entries[key] = {**entry, "scope": scope, "identity": identity, "driver_type": driver}
            changed = True
        if not changed:
            return
        if self.path and not self.error:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"version": 1, "entries": self.entries}, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except OSError as exc:
                self.error = "Records could not be saved"
                log.warning("%s: %s", self.error, exc)

    def leaderboard(self, track, mode, scope, laps):
        """Best lap and matching race-length time per identity, fastest first."""
        if not track:
            return []
        prefix = f"{self.map_key(track)}:{mode}:"
        rows = {}
        for key, entry in self.entries.items():
            if not key.startswith(prefix) or entry.get("scope") != scope:
                continue
            kind, target = key.rsplit(":", 2)[-2:]
            if kind == "race" and target != str(laps):
                continue
            identity = (entry["identity"], entry["driver_type"])
            row = rows.setdefault(identity, {"label": entry["car"] if scope == "car" else entry["controller"],
                                            "driver_type": entry["driver_type"], "lap": None, "race": None})
            row[kind] = entry
        return sorted(rows.values(), key=lambda r: (r["lap"]["seconds"] if r["lap"] else float("inf"),
                                                   r["race"]["seconds"] if r["race"] else float("inf"), r["label"]))
