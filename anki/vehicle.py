"""Async wrapper around one Anki Overdrive car over BLE (bleak)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice

from . import protocol as P

log = logging.getLogger("anki.vehicle")

MODEL_NAMES = {
    0x08: "Ground Shock", 0x09: "Skull", 0x0A: "Thermo", 0x0B: "Nuke", 0x0C: "Guardian",
    0x0E: "Big Bang", 0x0F: "Free Wheel", 0x10: "X52", 0x11: "X52 Ice", 0x12: "Mammoth",
    0x13: "Dynamo", 0x14: "Nuke Phantom",
}


def model_from_mfr(manufacturer_data: dict) -> tuple[int, str]:
    """Anki mfr data (company id 0xBEEF stripped by bleak): [id_lo, id_hi?, model, ...]. Model id sits at index 1."""
    for _cid, raw in manufacturer_data.items():
        if len(raw) >= 2:
            mid = raw[1]
            return mid, MODEL_NAMES.get(mid, f"model 0x{mid:02X}")
    return -1, "unknown"


ANKI_COMPANY_ID = 0xEFBE  # bleak reports the 0xBEEF marker little-endian
ADV_ON_CHARGER: dict[str, bool] = {}   # address -> on-charger bit from the last advertisement seen


def is_anki_adv(adv) -> bool:
    if P.SERVICE_UUID in [u.lower() for u in (adv.service_uuids or [])]:
        return True
    if ANKI_COMPANY_ID in (adv.manufacturer_data or {}):
        return True
    return bool(adv.local_name and "drive" in adv.local_name.lower())


async def discover(timeout: float = 5.0) -> list[tuple[BLEDevice, str]]:
    """Return [(device, model_name)] for every advertising Anki car, strongest signal first."""
    seen: dict[str, tuple[BLEDevice, int, str, Optional[bool]]] = {}

    def cb(device, adv):
        if is_anki_adv(adv):
            _, model = model_from_mfr(adv.manufacturer_data)
            st = P.adv_state(adv.local_name)
            prev = seen.get(device.address)
            charger = st.get("on_charger") if st else (prev[3] if prev else None)
            seen[device.address] = (device, adv.rssi, model, charger)

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    ordered = sorted(seen.values(), key=lambda t: -t[1])
    out = []
    for d, _rssi, m, charger in ordered:
        if charger is not None:
            ADV_ON_CHARGER[d.address] = charger
        out.append((d, m))
    return out


class Vehicle:
    def __init__(self, device: BLEDevice, model: str = "?"):
        self.device = device
        self.model = model
        self.client: Optional[BleakClient] = None
        self.on_message: Optional[Callable[[int, object], None]] = None
        self.on_timed_message: Optional[Callable[[int, object, float], None]] = None
        self.speed = 0
        self.lane_offset = 0.0
        self.battery_mv: Optional[int] = None
        self.version: Optional[int] = None
        self.last_position: Optional[P.PositionUpdate] = None
        self.status: Optional[P.VehicleStatus] = None      # from 0x3F
        self.actual_speed: Optional[int] = None            # from 0x36
        self.adv_on_charger: Optional[bool] = None         # from the advertisement, before we connect
        self._write_lock = asyncio.Lock()

    @property
    def address(self) -> str:
        return self.device.address

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    @property
    def on_charger(self) -> Optional[bool]:
        """True/False from the live status message, else the advertised hint, else None."""
        if self.status is not None:
            return self.status.on_charger
        return ADV_ON_CHARGER.get(self.address)

    async def connect(self, sdk_mode: bool = True, timeout: float = 15.0) -> None:
        client = BleakClient(self.device, timeout=timeout, disconnected_callback=self._on_disconnect)
        await client.connect()
        await client.start_notify(P.READ_CHAR_UUID, self._notify)
        self.client = client  # only publish the client once services are discovered and notifications run
        if sdk_mode:
            await self.send(P.sdk_mode(True))
            # Car reports offset 654321 ("unknown") until told where it is; lane changes are relative to this.
            await self.send(P.set_offset_from_road_center(0.0))
        log.info("connected %s (%s)", self.address, self.model)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.send(P.set_speed(0, 2000))
                await asyncio.sleep(0.1)
                await self.send(P.disconnect())
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def reconnect(self, sdk_mode: bool = True) -> None:
        """Drop whatever is left of the old client and connect again."""
        old = self.client
        self.client = None
        if old is not None:
            try:
                await old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        # after a reboot the car re-advertises; pick up the fresh device object before connecting
        fresh = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        if fresh is None:
            raise BleakError(f"{self.address} not advertising")
        self.device = fresh
        await self.connect(sdk_mode=sdk_mode)

    def _on_disconnect(self, _client) -> None:
        log.warning("disconnected %s", self.address)

    def _notify(self, _char, data: bytearray) -> None:
        received_at = time.monotonic()
        msg_id, decoded = P.parse(bytes(data))
        if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE:
            self.last_position = decoded  # type: ignore[assignment]
        elif msg_id == P.MSG_BATTERY_LEVEL_RESPONSE:
            self.battery_mv = decoded["battery_mv"]  # type: ignore[index]
        elif msg_id == P.MSG_VERSION_RESPONSE:
            self.version = decoded["version"]  # type: ignore[index]
        elif msg_id == P.MSG_STATUS_UPDATE and isinstance(decoded, P.VehicleStatus):
            self.status = decoded
        elif msg_id == P.MSG_SPEED_UPDATE and isinstance(decoded, P.SpeedUpdate):
            self.actual_speed = decoded.actual_mm_s
        if self.on_message:
            self.on_message(msg_id, decoded)
        if self.on_timed_message:
            self.on_timed_message(msg_id, decoded, received_at)

    async def send(self, payload: bytes) -> None:
        if not self.connected:
            return
        async with self._write_lock:
            await self.client.write_gatt_char(P.WRITE_CHAR_UUID, payload, response=False)  # type: ignore[union-attr]

    # --- high level -------------------------------------------------------
    async def set_speed(self, speed_mm_s: int, accel: int = 1000) -> None:
        self.speed = int(speed_mm_s)
        await self.send(P.set_speed(self.speed, accel))

    async def stop(self) -> None:
        await self.set_speed(0, 2500)

    async def change_lane(self, offset_mm: float, h_speed: int = 300, h_accel: int = 1000) -> None:
        """Move to an absolute offset from road center. Requires the car to be moving and localized."""
        offset_mm = max(-P.MAX_ABS_OFFSET, min(P.MAX_ABS_OFFSET, offset_mm))
        self.lane_offset = offset_mm
        await self.send(P.change_lane(offset_mm, h_speed, h_accel))

    async def u_turn(self) -> None:
        await self.send(P.turn(P.TURN_UTURN, P.TURN_TRIGGER_IMMEDIATE))

    async def set_lights(self, mask: int) -> None:
        await self.send(P.set_lights(mask))

    async def request_status(self) -> None:
        await self.send(P.version_request())
        await self.send(P.battery_request())
