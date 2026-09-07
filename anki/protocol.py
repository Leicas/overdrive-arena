"""Anki Overdrive BLE protocol (from the public anki/drive-sdk).

Every message is: [size][msg_id][payload...], where size excludes the size byte.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

SERVICE_UUID = "be15beef-6186-407e-8381-0bd89c4d8df4"
READ_CHAR_UUID = "be15bee0-6186-407e-8381-0bd89c4d8df4"   # notifications car -> host
WRITE_CHAR_UUID = "be15bee1-6186-407e-8381-0bd89c4d8df4"  # host -> car

# host -> vehicle
MSG_DISCONNECT = 0x0D
MSG_PING_REQUEST = 0x16
MSG_VERSION_REQUEST = 0x18
MSG_BATTERY_LEVEL_REQUEST = 0x1A
MSG_SET_LIGHTS = 0x1D
MSG_SET_SPEED = 0x24
MSG_CHANGE_LANE = 0x25
MSG_CANCEL_LANE_CHANGE = 0x26
MSG_SET_OFFSET_FROM_ROAD_CENTER = 0x2C
MSG_TURN = 0x32
MSG_LIGHTS_PATTERN = 0x33
MSG_SET_CONFIG_PARAMS = 0x45
MSG_SDK_MODE = 0x90

# vehicle -> host
MSG_PING_RESPONSE = 0x17
MSG_VERSION_RESPONSE = 0x19
MSG_BATTERY_LEVEL_RESPONSE = 0x1B
MSG_LOCALIZATION_POSITION_UPDATE = 0x27
MSG_LOCALIZATION_TRANSITION_UPDATE = 0x29
MSG_LOCALIZATION_INTERSECTION_UPDATE = 0x2A
MSG_VEHICLE_DELOCALIZED = 0x2B
MSG_OFFSET_FROM_ROAD_CENTER_UPDATE = 0x2D
MSG_SPEED_UPDATE = 0x36                 # desired u16, accel u16, actual u16
MSG_STATUS_UPDATE = 0x3F                # on_track, on_charger, battery_low, battery_charged (one byte each)
MSG_LANE_CHANGE_UPDATE = 0x41
MSG_COLLISION_DETECTED = 0x4D
MSG_CYCLE_OVERTIME = 0x86

# advertising: first byte of the local name is a state byte
ADV_STATE_FULL_BATTERY = 0x10
ADV_STATE_LOW_BATTERY = 0x20
ADV_STATE_ON_CHARGER = 0x40

TURN_NONE, TURN_LEFT, TURN_RIGHT, TURN_UTURN, TURN_UTURN_JUMP = 0, 1, 2, 3, 4
TURN_TRIGGER_IMMEDIATE, TURN_TRIGGER_INTERSECTION = 0, 1

# Lane offsets in mm from road center (4-lane Overdrive track).
LANE_OFFSETS = (-68.0, -23.0, 23.0, 68.0)
MAX_ABS_OFFSET = 68.0


def _msg(msg_id: int, payload: bytes = b"") -> bytes:
    return bytes([1 + len(payload), msg_id]) + payload


def sdk_mode(on: bool = True, override_localization: bool = True) -> bytes:
    return _msg(MSG_SDK_MODE, bytes([1 if on else 0, 0x01 if override_localization else 0]))


def set_speed(speed_mm_s: int, accel_mm_s2: int = 1000, respect_limit: bool = False) -> bytes:
    speed_mm_s = max(-32768, min(32767, int(speed_mm_s)))
    return _msg(MSG_SET_SPEED, struct.pack("<hhB", speed_mm_s, int(accel_mm_s2), 1 if respect_limit else 0))


def set_offset_from_road_center(offset_mm: float) -> bytes:
    """Tell the car where it currently is relative to center (used before change_lane)."""
    return _msg(MSG_SET_OFFSET_FROM_ROAD_CENTER, struct.pack("<f", float(offset_mm)))


def change_lane(offset_mm: float, h_speed: int = 300, h_accel: int = 1000) -> bytes:
    offset_mm = max(-MAX_ABS_OFFSET, min(MAX_ABS_OFFSET, float(offset_mm)))
    return _msg(MSG_CHANGE_LANE, struct.pack("<HHfBB", int(h_speed), int(h_accel), offset_mm, 0, 0))


def cancel_lane_change() -> bytes:
    return _msg(MSG_CANCEL_LANE_CHANGE)


def turn(turn_type: int = TURN_UTURN, trigger: int = TURN_TRIGGER_IMMEDIATE) -> bytes:
    return _msg(MSG_TURN, bytes([turn_type, trigger]))


# set_lights mask: low nibble = which lights to change, high nibble = their new state
LIGHT_HEADLIGHTS, LIGHT_BRAKELIGHTS, LIGHT_FRONTLIGHTS, LIGHT_ENGINE = 0x01, 0x02, 0x04, 0x08


def set_lights(mask: int) -> bytes:
    return _msg(MSG_SET_LIGHTS, bytes([mask & 0xFF]))


def lights_on(channels: int) -> bytes:
    return set_lights((channels & 0x0F) | ((channels & 0x0F) << 4))


def lights_off(channels: int) -> bytes:
    return set_lights(channels & 0x0F)


# lights_pattern channels / effects (drive-sdk). Intensities 0..14, cycles 0..11.
LC_RED, LC_TAIL, LC_BLUE, LC_GREEN, LC_FRONTL, LC_FRONTR = range(6)
EFFECT_STEADY, EFFECT_FADE, EFFECT_THROB, EFFECT_FLASH, EFFECT_RANDOM = range(5)
MAX_LIGHT_INTENSITY = 14


def lights_pattern(channels: list[tuple[int, int, int, int, int]]) -> bytes:
    """channels: up to 3 tuples (channel, effect, start, end, cycles_per_10s)."""
    channels = channels[:3]
    payload = bytes([len(channels)])
    for ch, eff, start, end, cycles in channels:
        payload += bytes([ch & 0xFF, eff & 0xFF, min(MAX_LIGHT_INTENSITY, start), min(MAX_LIGHT_INTENSITY, end), min(255, cycles)])
    return _msg(MSG_LIGHTS_PATTERN, payload)


def engine_color(r: int, g: int, b: int, effect: int = EFFECT_STEADY, cycles: int = 0) -> bytes:
    """Set the RGB 'engine' light. r,g,b in 0..14."""
    return lights_pattern([(LC_RED, effect, r, r, cycles), (LC_GREEN, effect, g, g, cycles), (LC_BLUE, effect, b, b, cycles)])


def ping() -> bytes:
    return _msg(MSG_PING_REQUEST)


def version_request() -> bytes:
    return _msg(MSG_VERSION_REQUEST)


def battery_request() -> bytes:
    return _msg(MSG_BATTERY_LEVEL_REQUEST)


def disconnect() -> bytes:
    return _msg(MSG_DISCONNECT)


PARSEFLAGS_MASK_NUM_BITS = 0x0F
PARSEFLAGS_MASK_INVERTED_COLOR = 0x80
PARSEFLAGS_MASK_REVERSE_PARSING = 0x40   # car reads this piece's codes back-to-front
PARSEFLAGS_MASK_REVERSE_DRIVING = 0x20


@dataclass
class PositionUpdate:
    """0x27, body 15 bytes (firmware 11866): loc, piece, offset f32, speed u16, flags,
    last_recv_lane_change_id, last_exec_lane_change_id, last_desired_lane_change_speed u16, last_desired_speed u16."""
    location: int
    piece: int
    offset_mm: float
    speed_mm_s: int
    parsing_flags: int
    last_recv_lane_change_id: int = 0
    last_exec_lane_change_id: int = 0
    last_desired_lane_change_speed: int = 0
    last_desired_speed: int = 0

    @property
    def reverse_parsing(self) -> bool:
        return bool(self.parsing_flags & PARSEFLAGS_MASK_REVERSE_PARSING)


@dataclass
class VehicleStatus:
    """0x3F: sent after connect and whenever one of the flags changes."""
    on_track: bool
    on_charger: bool
    battery_low: bool
    battery_charged: bool


@dataclass
class SpeedUpdate:
    """0x36: echo of the last speed command plus the measured speed."""
    desired_mm_s: int
    accel_mm_s2: int
    actual_mm_s: int


@dataclass
class TransitionUpdate:
    """0x29, body 16 bytes (firmware 11866). Wheel distances are the cm each wheel travelled
    over the piece just left; left > right means that piece was a right-hand curve."""
    piece_idx: int
    prev_piece_idx: int
    offset_mm: float
    last_recv_lane_change_id: int
    last_exec_lane_change_id: int
    last_desired_lane_change_speed: int
    last_desired_speed: int
    uphill_counter: int
    downhill_counter: int
    left_wheel_cm: int
    right_wheel_cm: int

    @property
    def turn(self) -> str | None:
        """'R', 'L' or None (straight) for the piece just completed."""
        d = self.left_wheel_cm - self.right_wheel_cm
        if d >= 2:
            return "R"
        if d <= -2:
            return "L"
        return None


def parse(data: bytes) -> tuple[int, object]:
    """Return (msg_id, decoded) for a notification. Unknown messages decode to raw bytes."""
    if len(data) < 2:
        return -1, data
    msg_id = data[1]
    body = data[2:]
    try:
        if msg_id == MSG_LOCALIZATION_POSITION_UPDATE and len(body) >= 8:
            if len(body) >= 15:
                return msg_id, PositionUpdate(*struct.unpack_from("<BBfHBBBHH", body, 0))
            loc, piece, off, spd = struct.unpack_from("<BBfH", body, 0)
            flags = body[8] if len(body) > 8 else 0
            return msg_id, PositionUpdate(loc, piece, off, spd, flags)
        if msg_id == MSG_LOCALIZATION_TRANSITION_UPDATE and len(body) >= 16:
            return msg_id, TransitionUpdate(*struct.unpack_from("<bbfBBHHBBBB", body, 0))
        if msg_id == MSG_LOCALIZATION_TRANSITION_UPDATE and len(body) >= 8:
            piece, prev, off, dir_ = struct.unpack_from("<BBfB", body, 0)
            return msg_id, {"piece": piece, "prev_piece": prev, "offset_mm": off, "direction": dir_}
        if msg_id == MSG_BATTERY_LEVEL_RESPONSE and len(body) >= 2:
            return msg_id, {"battery_mv": struct.unpack_from("<H", body)[0]}
        if msg_id == MSG_VERSION_RESPONSE and len(body) >= 2:
            return msg_id, {"version": struct.unpack_from("<H", body)[0]}
        if msg_id == MSG_PING_RESPONSE:
            return msg_id, "pong"
        if msg_id == MSG_VEHICLE_DELOCALIZED:
            return msg_id, "delocalized"
        if msg_id == MSG_OFFSET_FROM_ROAD_CENTER_UPDATE and len(body) >= 4:
            return msg_id, {"offset_mm": struct.unpack_from("<f", body)[0]}
        if msg_id == MSG_STATUS_UPDATE and len(body) >= 4:
            return msg_id, VehicleStatus(bool(body[0] & 1), bool(body[1] & 1), bool(body[2] & 1), bool(body[3] & 1))
        if msg_id == MSG_SPEED_UPDATE and len(body) >= 6:
            return msg_id, SpeedUpdate(*struct.unpack_from("<HHH", body, 0))
        if msg_id == MSG_COLLISION_DETECTED:
            return msg_id, "collision"
    except struct.error:
        pass
    return msg_id, body


def adv_state(name: str | None) -> dict:
    """Decode the state byte that leads the advertised name: {'on_charger', 'low', 'full'}."""
    if not name:
        return {}
    b = ord(name[0])
    return {"on_charger": bool(b & ADV_STATE_ON_CHARGER), "low": bool(b & ADV_STATE_LOW_BATTERY),
            "full": bool(b & ADV_STATE_FULL_BATTERY)}


def parse_local_name(name: str | None) -> str:
    """Advertised name is: 1 state byte, 5 version bytes, then the model name."""
    if not name:
        return "?"
    if len(name) > 6 and not name[:1].isprintable():
        return name[6:].strip("\x00 ")
    return name
