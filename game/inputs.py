"""Input sources: SDL game controllers (Xbox / DualSense / ...) and the keyboard, mapped to abstract actions."""
from __future__ import annotations

import logging

import pygame
from pygame._sdl2 import controller as sdlc

log = logging.getLogger("game.inputs")

DEADZONE = 0.15
TRIGGER_DEADZONE = 0.04
AXIS_MAX = 32767.0
KEY_THROTTLE_RAMP_S = 0.8

# edge-triggered actions
(STOP, UTURN, FIRE, MINE, LANE_LEFT, LANE_RIGHT, LIMIT_UP, LIMIT_DOWN,
 MENU_LEFT, MENU_RIGHT, SELECT, RELEASE, TOGGLE_AI, READY, BACK, SCAN, TOGGLE_MODE, RECOVER, BOOST) = range(19)

ACTION_NAMES = {STOP: "stop", UTURN: "u-turn", FIRE: "fire", MINE: "mine", LANE_LEFT: "lane left",
                LANE_RIGHT: "lane right", LIMIT_UP: "limit +", LIMIT_DOWN: "limit -", MENU_LEFT: "menu left",
                MENU_RIGHT: "menu right", SELECT: "select", RELEASE: "release", TOGGLE_AI: "AI/parked",
                READY: "ready", BACK: "back", SCAN: "scan track", TOGGLE_MODE: "battle/race mode", RECOVER: "retry off-track AI", BOOST: "straight boost"}


class InputSource:
    name = "?"
    short = "?"

    def poll(self, dt: float) -> None: ...
    def throttle(self) -> float: return 0.0
    def brake(self) -> float: return 0.0
    def steer(self) -> float: return 0.0
    def pressed(self, action: int) -> bool: return False
    def rumble(self, low: float, high: float, ms: int) -> None: ...
    def close(self) -> None: ...
    def alive(self) -> bool: return True


class Pad(InputSource):
    BUTTONS = {
        STOP: (pygame.CONTROLLER_BUTTON_B,),
        UTURN: (pygame.CONTROLLER_BUTTON_A,),
        FIRE: (pygame.CONTROLLER_BUTTON_X,),
        MINE: (pygame.CONTROLLER_BUTTON_Y,),
        LANE_LEFT: (pygame.CONTROLLER_BUTTON_LEFTSHOULDER, pygame.CONTROLLER_BUTTON_DPAD_LEFT),
        LANE_RIGHT: (pygame.CONTROLLER_BUTTON_RIGHTSHOULDER, pygame.CONTROLLER_BUTTON_DPAD_RIGHT),
        LIMIT_UP: (pygame.CONTROLLER_BUTTON_DPAD_UP,),
        LIMIT_DOWN: (pygame.CONTROLLER_BUTTON_DPAD_DOWN,),
        MENU_LEFT: (pygame.CONTROLLER_BUTTON_DPAD_LEFT, pygame.CONTROLLER_BUTTON_LEFTSHOULDER),
        MENU_RIGHT: (pygame.CONTROLLER_BUTTON_DPAD_RIGHT, pygame.CONTROLLER_BUTTON_RIGHTSHOULDER),
        SELECT: (pygame.CONTROLLER_BUTTON_A,),
        RELEASE: (pygame.CONTROLLER_BUTTON_B,),
        TOGGLE_AI: (pygame.CONTROLLER_BUTTON_X,),
        READY: (pygame.CONTROLLER_BUTTON_START,),
        RECOVER: (pygame.CONTROLLER_BUTTON_RIGHTSTICK,),
        BOOST: (pygame.CONTROLLER_BUTTON_LEFTSTICK,),
        BACK: (pygame.CONTROLLER_BUTTON_BACK,),
        SCAN: (pygame.CONTROLLER_BUTTON_Y,),
        TOGGLE_MODE: (pygame.CONTROLLER_BUTTON_RIGHTSTICK, pygame.CONTROLLER_BUTTON_LEFTSTICK),
    }

    def __init__(self, index: int, number: int):
        self.ctrl = sdlc.Controller(index)
        self.name = sdlc.name_forindex(index) or f"pad {index}"  # only safe once is_controller(index) is True
        self.short = f"P{number}"
        self.instance_id = self.ctrl.as_joystick().get_instance_id()
        self._now: dict[int, bool] = {}
        self._prev: dict[int, bool] = {}
        self._menu_axis_prev = 0
        self._alive = True

    def poll(self, dt: float) -> None:
        self._prev = self._now
        self._now = {a: any(self.ctrl.get_button(b) for b in btns) for a, btns in self.BUTTONS.items()}
        # left stick also moves the menu cursor
        x = self._axis(pygame.CONTROLLER_AXIS_LEFTX)
        step = -1 if x < -0.6 else (1 if x > 0.6 else 0)
        if step != self._menu_axis_prev and step != 0:
            self._now[MENU_LEFT if step < 0 else MENU_RIGHT] = True
            self._prev[MENU_LEFT if step < 0 else MENU_RIGHT] = False
        self._menu_axis_prev = step

    def pressed(self, action: int) -> bool:
        return self._now.get(action, False) and not self._prev.get(action, False)

    def _axis(self, a: int) -> float:
        return self.ctrl.get_axis(a) / AXIS_MAX

    def throttle(self) -> float:
        v = self._axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT)
        return 0.0 if v < TRIGGER_DEADZONE else min(1.0, v)

    def brake(self) -> float:
        v = self._axis(pygame.CONTROLLER_AXIS_TRIGGERLEFT)
        return 0.0 if v < TRIGGER_DEADZONE else min(1.0, v)

    def steer(self) -> float:
        v = self._axis(pygame.CONTROLLER_AXIS_LEFTX)
        if abs(v) < DEADZONE:
            return 0.0
        return (1 if v > 0 else -1) * min(1.0, (abs(v) - DEADZONE) / (1 - DEADZONE))

    def rumble(self, low: float, high: float, ms: int) -> None:
        try:
            self.ctrl.rumble(low, high, ms)
        except Exception:  # noqa: BLE001
            pass

    def mark_removed(self) -> None:
        self._alive = False

    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        try:
            self.ctrl.quit()
        except Exception:  # noqa: BLE001
            pass


class Keyboard(InputSource):
    name = "keyboard"
    short = "KB"
    KEYS = {
        STOP: (pygame.K_SPACE,), UTURN: (pygame.K_u,), FIRE: (pygame.K_f, pygame.K_RCTRL), MINE: (pygame.K_g, pygame.K_RSHIFT),
        LANE_LEFT: (pygame.K_q,), LANE_RIGHT: (pygame.K_e,),
        LIMIT_UP: (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS, pygame.K_PAGEUP),
        LIMIT_DOWN: (pygame.K_MINUS, pygame.K_KP_MINUS, pygame.K_PAGEDOWN),
        MENU_LEFT: (pygame.K_LEFT, pygame.K_a), MENU_RIGHT: (pygame.K_RIGHT, pygame.K_d),
        SELECT: (pygame.K_RETURN, pygame.K_KP_ENTER), RELEASE: (pygame.K_BACKSPACE,), TOGGLE_AI: (pygame.K_TAB,),
        READY: (pygame.K_SPACE,),
        RECOVER: (pygame.K_r,),
        BOOST: (pygame.K_LSHIFT,),
        BACK: (pygame.K_ESCAPE,), SCAN: (pygame.K_t,), TOGGLE_MODE: (pygame.K_m,),
    }

    def __init__(self):
        self._throttle = 0.0
        self._keys = pygame.key.get_pressed()
        self._down: set[int] = set()

    def feed_keydown(self, key: int) -> None:
        self._down.add(key)

    def end_frame(self) -> None:
        self._down.clear()

    def poll(self, dt: float) -> None:
        self._keys = pygame.key.get_pressed()
        if self._keys[pygame.K_w] or self._keys[pygame.K_UP]:
            self._throttle = min(1.0, self._throttle + dt / KEY_THROTTLE_RAMP_S)
        else:
            self._throttle = 0.0

    def throttle(self) -> float:
        return self._throttle

    def brake(self) -> float:
        return 1.0 if (self._keys[pygame.K_s] or self._keys[pygame.K_DOWN]) else 0.0

    def steer(self) -> float:
        v = 0.0
        if self._keys[pygame.K_a] or self._keys[pygame.K_LEFT]:
            v -= 1.0
        if self._keys[pygame.K_d] or self._keys[pygame.K_RIGHT]:
            v += 1.0
        return v

    def pressed(self, action: int) -> bool:
        return any(k in self._down for k in self.KEYS[action])


def list_pads() -> list[Pad]:
    pads: list[Pad] = []
    for i in range(sdlc.get_count()):
        if sdlc.is_controller(i):  # name_forindex() segfaults on non-gamepad HID devices, so gate on this
            pads.append(Pad(i, len(pads) + 1))
        else:
            log.info("skipping non-gamepad device %d", i)
    return pads
