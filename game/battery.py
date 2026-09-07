"""Battery monitoring for one car: percentage, idle vs. under-load voltage (sag), warnings, CSV history.

The cars run a single LiPo cell (~3.7 V nominal). The 0x1B battery response is the raw cell voltage, which
sags under motor load, so the percentage is estimated from the highest recent reading taken while stopped.
"""
from __future__ import annotations

import csv
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

# resting voltage -> percent, single-cell LiPo
CURVE = [(4200, 100), (4100, 90), (4000, 78), (3900, 60), (3800, 42), (3750, 30), (3700, 20),
         (3650, 12), (3600, 6), (3500, 2), (3400, 0)]

LOW_MV = 3680
CRITICAL_MV = 3550
WEAK_SAG_MV = 200      # idle-to-load drop that marks a tired cell
HISTORY_S = 90.0


def mv_to_percent(mv: float) -> int:
    if mv >= CURVE[0][0]:
        return 100
    for (v1, p1), (v0, p0) in zip(CURVE, CURVE[1:]):
        if v0 <= mv <= v1:
            return int(round(p0 + (p1 - p0) * (mv - v0) / (v1 - v0)))
    return 0


class BatteryMonitor:
    def __init__(self, name: str, log_path: Optional[Path] = None):
        self.name = name
        self.log_path = log_path
        self.readings: deque[tuple[float, int, int]] = deque(maxlen=400)  # (t, mv, commanded speed); speed -1 = charging
        self.mv: Optional[int] = None
        self.last_t = 0.0
        self.charging = False

    def update(self, mv: int, speed_cmd: int, charging: bool = False) -> None:
        now = time.monotonic()
        self.mv = mv
        self.last_t = now
        self.charging = charging
        self.readings.append((now, mv, -1 if charging else speed_cmd))
        if self.log_path:
            try:
                new = not self.log_path.exists()
                with self.log_path.open("a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["time", "car", "mv", "speed_cmd"])
                    w.writerow([datetime.now().isoformat(timespec="seconds"), self.name, mv, "charging" if charging else speed_cmd])
            except OSError:
                pass

    # ---- derived ----
    def _recent(self):
        cutoff = time.monotonic() - HISTORY_S
        return [r for r in self.readings if r[0] >= cutoff]

    @property
    def idle_mv(self) -> Optional[int]:
        """Best resting reading; readings taken while charging are inflated and ignored."""
        idle = [mv for _, mv, spd in self._recent() if spd == 0]
        if idle:
            return max(idle)
        rest = [mv for _, mv, spd in self._recent() if spd >= 0]
        return max(rest) if rest else None

    @property
    def load_mv(self) -> Optional[int]:
        load = [mv for _, mv, spd in self._recent() if spd >= 250]
        return min(load) if load else None

    @property
    def sag_mv(self) -> Optional[int]:
        i, l = self.idle_mv, self.load_mv
        if i is None or l is None:
            return None
        return max(0, i - l)

    @property
    def percent(self) -> Optional[int]:
        i = self.idle_mv
        return None if i is None else mv_to_percent(i)

    @property
    def status(self) -> str:
        """charging | ok | weak | low | critical | unknown"""
        if self.charging:
            return "charging"
        i = self.idle_mv
        if i is None:
            return "unknown"
        if i < CRITICAL_MV:
            return "critical"
        if i < LOW_MV:
            return "low"
        sag = self.sag_mv
        if sag is not None and sag > WEAK_SAG_MV:
            return "weak"
        return "ok"

    def summary(self) -> str:
        if self.mv is None:
            return "battery ?"
        if self.charging:
            return f"charging  {self.mv / 1000:.2f} V"
        pct = self.percent
        s = f"{self.mv / 1000:.2f} V"
        if pct is not None:
            s += f"  {pct:3d}%"
        sag = self.sag_mv
        if sag:
            s += f"  sag {sag / 1000:.2f} V"
        st = self.status
        if st != "ok":
            s += f"  [{st.upper()}]"
        return s

    def trend_mv_per_min(self) -> Optional[float]:
        """Slope of idle readings over the history window (negative = draining)."""
        pts = [(t, mv) for t, mv, spd in self._recent() if spd == 0]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 20:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mm = sum(mv for _, mv in pts) / n
        var = sum((t - mt) ** 2 for t, _ in pts)
        if var == 0:
            return None
        slope = sum((t - mt) * (mv - mm) for t, mv in pts) / var
        return slope * 60.0
