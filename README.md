# Overdrive Arena

Drive [Anki Overdrive](https://en.wikipedia.org/wiki/Anki_(company)) cars from a Windows PC with Xbox / PS5
controllers or the keyboard, watch them live on a map scanned from your own track, and race or battle with
virtual weapons. Pure Python: `bleak` for Bluetooth LE, `pygame` for input and rendering.

The cars are long out of production and the official app is gone from the stores. This project talks to the
cars directly, with the protocol verified on real hardware (firmware 11866) and documented below.

![Battle](docs/battle.png)

| Pairing | Track scan |
|---|---|
| ![Pairing](docs/pairing.png) | ![Scan](docs/scan.png) |

| Starting grid | Race finish |
|---|---|
| ![Grid](docs/grid.png) | ![Race win](docs/race-win.png) |

## Features

- **Controllers**: any gamepad SDL recognises (Xbox, DualSense, Switch Pro...) plus the keyboard, one per car.
  Pads can join or leave while the app runs.
- **Track scanning**: the cars drive one lap and the loop is rebuilt from the track codes (piece ids, curve
  direction from wheel travel, closure check). Saved as `track.json`.
- **Live map**: cars drawn on the track with heading, lane, trails and effects, from the cars' own position
  reports plus dead reckoning in between.
- **Starting grid**: every car drives itself to the start line in its own lane and stops on the finish-line
  bar; 3-2-1-GO releases them together.
- **Two modes**: BATTLE (blasters, mines, HP, kills) or RACE (laps only, first to N laps wins).
- **AI drivers** for cars nobody claims; they keep to the track direction.
- **Battery and charger tracking**: voltage, estimated %, sag under load (finds tired cells), CHARGING /
  CHARGED when a car sits on its pad, CSV history.
- **Robust links**: automatic reconnection when a car reboots, gentle default acceleration for old batteries.

## Setup

Windows 10/11 with a Bluetooth LE adapter, Python 3.11+.

```bash
git clone https://github.com/Leicas/overdrive-arena.git
cd overdrive-arena
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Cars must be powered on. Turn controllers on before starting (or later, they hot-plug).

## How to play

1. **Connect**: every advertising car is connected (limit with `--max-cars N` or `--car <address>`).
2. **Pairing screen**: one card per car with a **driver slot** that reads either a player badge (P1, P2, KB),
   **AI** or **PARKED**. Every controller and the keyboard has its own cursor badge floating above the cards.
   - Move the cursor: D-pad / left stick (keyboard: Left/Right). The car under a cursor blinks its headlights.
   - **A** (Enter): take the car you are hovering. Press again, or **B** (Backspace), to release it.
   - **X** (Tab): switch a car nobody has taken between AI and parked.
   - **SCAN TRACK** (Y / T / click): every connected car drives a lap; the first to close the loop provides the
     map. Cars that read no track code within 8 s are flagged "NOT ON THE TRACK". Do this once per layout.
   - **BATTLE / RACE** (M / stick click / click) picks the mode; the **laps** button cycles 3 / 5 / 10 / 20.
   - **RACE** (Start / Space / click): enabled once at least one car has a player. The controllers legend and
     the "players / AI / parked" line tell you who drives what.
3. **Starting grid**: each participant drives to the start line in its own lane (turning around first if it
   faces the wrong way), crawls the last half piece at 200 mm/s and stops on the finish-line bar. Cars that
   arrive in the wrong lane do one more lap; cars that read no codes within 12 s, or sit on their charger,
   are left out. Then **3-2-1-GO**.
4. **Race**. **Back** (Esc) stops everything and returns to pairing. **F11** toggles fullscreen.

| Action              | Xbox            | PS5           | Keyboard          |
|---------------------|-----------------|---------------|-------------------|
| Throttle / brake    | RT / LT         | R2 / L2       | W / S             |
| Steer (analog)      | Left stick X    | Left stick X  | A / D             |
| Snap lane           | LB / RB, D-pad  | L1 / R1       | Q / E             |
| Fire blaster        | X               | Square        | F                 |
| Drop mine           | Y               | Triangle      | G                 |
| U-turn              | A               | Cross         | U                 |
| Emergency stop      | B               | Circle        | Space             |
| Speed limit +/- 100 | D-pad up/down   | D-pad up/down | + / -             |
| Back / quit         | View            | Share         | Esc               |

### Battle weapons

- **Blaster** (1 s cooldown): hits the nearest car ahead within 1.4 m along the track, any lane.
  20 damage, victim slowed to 45 % for 1.5 s.
- **Mine** (4 s cooldown, 2 active): dropped in your lane, arms after 1 s, lives 45 s. A car passing within
  12 cm in that lane takes 35 damage and is stopped for 1.2 s.
- 100 HP. At 0 the car is destroyed: stopped 3 s with flashing lights, then respawns. Kills are counted.
- Hits flash the victim's lights and rumble its controller.

### Race mode

Weapons off. Cards show `LAP n / target` and a progress bar; ranking by laps then position. The first car to
the target gets a "X WINS" banner, the others roll to a stop, and the app returns to pairing.
`--mode race --laps 10` pre-selects.

### What the screen shows

- **Map**: car sprites with trails, hit rings, explosions, floating damage; mines pulse once armed; cars that
  are off the track, on a charger or have lost their link are listed in the map corner.
- **Cards**: rank badge, driver, status chip (LINING UP / ON THE LINE / SLOWED / STUNNED / DESTROYED /
  WRONG WAY / OFF TRACK / ON CHARGER), HP or lap progress, speed bar and limit, lane indicator, weapon dials,
  laps with last and best time, battery row.
- **Kill feed** in player colours, race clock.

### Battery and charger

The car reports its single-cell LiPo voltage (polled every 4 s) and a status word with on-track / on-charger /
battery-low / battery-charged flags. The app shows voltage, an estimated percentage from a resting-voltage
curve, and the **sag** between idle and under-load voltage: above 0.20 V the cell is tired (`WEAK CELL`) and
that car is the one most likely to reboot under hard acceleration. `LOW` below 3.68 V idle, `CRITICAL` below
3.55 V. On the pad: `CHARGING`, then `CHARGED`. Every reading goes to `battery_log.csv`.

## Options

```
--max-speed N     initial speed limit mm/s (default 800; the cars do ~1500)
--accel N         acceleration mm/s^2 (default 600; harder launches reboot cars with weak batteries)
--ai-speed N      AI cruising speed (default 450)      --no-ai   unclaimed cars stay parked
--mode battle|race   --laps N                          --keyboard-only
--max-cars N   --car ADDRESS (repeatable)   --scan SECONDS
```

## Tips

- **Bluetooth**: the cars share the PC adapter with wireless pads. With two Xbox pads on Bluetooth the car
  links got noticeably flakier; put the pads on USB / the Xbox dongle, or give the cars their own BLE dongle.
- **Weak batteries**: keep `--accel` low. A car that reboots shows LINK LOST and is reconnected automatically.
- **Lanes**: the car only knows the lateral offset the host tells it, so the app syncs it from the track codes
  before every lane command and re-tries sticky lane goals. Track codes are missed below ~250 mm/s.

## Repository layout

```
app.py              state machine: connect -> pairing -> scan -> grid -> countdown -> race
anki/protocol.py    message encoders / decoders (verified byte layouts)
anki/vehicle.py     one car over BLE: discover, connect, reconnect, notifications
anki/ble_worker.py  BLE on its own thread (pygame's COM apartment breaks bleak on the main thread)
anki/track.py       piece tables, track scanner, layout, per-car localizer
anki/data/          offsetInfo.json: location id -> lane / progress table
game/inputs.py      gamepads and keyboard -> abstract actions
game/game.py        driving, AI, grid, weapons, laps, damage
game/battery.py     battery monitor
game/render.py      map, HUD, screens
tools/              small CLI scripts used while reverse-engineering (scan, drive, lane, lights, lap logs)
tests/              recorded lap + offline replay and render scripts
```

## Protocol notes (firmware 11866, verified on X52 / Mammoth / Dynamo)

Service `be15beef-6186-407e-8381-0bd89c4d8df4`, write `...bee1`, notify `...bee0`.
Messages are `[len][id][payload]`; see `anki/protocol.py`.

- After connect send SDK mode (0x90) **and** "offset from road center = 0" (0x2C). Until then the car
  reports offset 654321 (unknown) and lane changes do nothing.
- The car does **not** correct its believed offset from the track codes. Read the real lane from the
  location id (table below) and push it back with 0x2C before a `change_lane`.
- **0x27 position** (15-byte body): `loc u8, piece u8, offset f32, speed u16, flags u8, last_recv_lane_change_id u8,
  last_exec_lane_change_id u8, last_desired_lane_change_speed u16, last_desired_speed u16`. Flag 0x40 = reverse parsing.
- **0x29 transition** (16-byte body): `piece_idx i8, prev i8, offset f32, last_recv_lc u8, last_exec_lc u8,
  lane_change_speed u16, desired_speed u16, uphill u8, downhill u8, left_wheel_cm u8, right_wheel_cm u8`.
  Wheel distances cover the piece just left: left > right means a right-hand curve. Bars are detected even at
  crawl speed; codes are missed below ~250 mm/s.
- **0x3F status**: `on_track, on_charger, battery_low, battery_charged` (one byte each). Sent after connect and
  on change. The advertised name's first byte also has an on-charger bit (0x40).
- **0x36 speed update**: `desired u16, accel u16, actual u16`. **0x4D**: collision detected. **0x1B**: battery mV.
- Piece ids: straights 36 39 40 48 51; curves 17 18 20 23 24 27; start piece = 34 (first half) then 33 with a
  bar between the halves (the finish line); intersection 10; jump ramp 43/58; landing 46/63; F&F specials 53 54 57.
  Ids repeat within a track.
- Location ids encode lane and progress (`anki/data/offsetInfo.json`). The table's offset sign is opposite to
  the car's unless the reverse-parsing flag is set. Straights have 3 progress slots per lane, curves 2-3.
- Geometry: a straight is 560 mm; a curve's centerline radius is ~280 mm (from wheel travel), so pieces lay
  out on a 560 mm grid and four curves make a circle one straight wide.
- Advertising: manufacturer id 0xBEEF (bleak reports 0xEFBE), model id at byte 1 (0x10 X52, 0x12 Mammoth,
  0x13 Dynamo, others in `anki/vehicle.py`). The local name is often missing from the scan response.

## Credits

- [anki/drive-sdk](https://github.com/anki/drive-sdk) for the original message definitions.
- [MasterAirscrachDev/Anki-Partydrive](https://github.com/MasterAirscrachDev/Anki-Partydrive) for the
  location-id table (`anki/data/offsetInfo.json`) and the status-message layout.
- Everyone who kept these little cars alive after the app disappeared.

## Licence

MIT, see `LICENSE`. Anki and Overdrive are trademarks of their respective owners; this is an unaffiliated
hobby project.
