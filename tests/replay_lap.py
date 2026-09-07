"""Replay a recorded hex log through the scanner, then through a localizer on the built track."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anki import protocol as P
from anki.track import TrackScanner, CarLocalizer, Track

msgs = [P.parse(bytes.fromhex(line)) for line in Path(sys.argv[1] if len(sys.argv) > 1 else "tests/x52_lap.hex").read_text().split("\n") if line.strip()]
sc = TrackScanner()
for mid, dec in msgs:
    sc.on_message(mid, dec)
    if sc.done:
        break
print("scanner:", sc.state)
assert sc.done, "scanner did not finish"
t = sc.track
print(t.describe())
for i, p in enumerate(t.pieces):
    print(f"  {i}: id={p.id:>3} {p.kind:<9} turn={p.turn} rev={p.reversed} cell={p.cell} h_in={p.heading_in} h_out={p.heading_out}")
print("bounds mm:", t.bounds())
js = t.to_json(); t2 = Track.from_json(js); assert t2.describe() == t.describe(); print("json roundtrip ok")

loc = CarLocalizer(t)
print("\nlocalizer replay:")
for mid, dec in msgs:
    loc.on_message(mid, dec)
    if mid in (P.MSG_LOCALIZATION_POSITION_UPDATE, P.MSG_LOCALIZATION_TRANSITION_UPDATE):
        w = loc.world() or (float("nan"), float("nan"))
        tag = "POS " if mid == 0x27 else "TRAN"
        extra = f"piece={dec.piece} loc={dec.location} rev={dec.reverse_parsing}" if mid == 0x27 else f"wheels={dec.left_wheel_cm}/{dec.right_wheel_cm} turn={dec.turn}"
        print(f"  {tag} idx={loc.index} frac={loc.frac:.2f} lane={loc.lane_mm:+6.1f} dir={loc.direction:+d} sub={loc.sub_id} xy=({w[0]:7.0f},{w[1]:7.0f})  {extra}")
