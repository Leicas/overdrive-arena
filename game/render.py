"""pygame rendering: live track map with effects, HUD cards, pairing and scanning screens."""
from __future__ import annotations

import math
import random
import time
from collections import deque
from typing import Optional

import pygame

from anki.track import START, TURN, Track
from .game import MAX_HP, Game, CarState, BLASTER_COOLDOWN_S, MINE_COOLDOWN_S, MINE_ARM_S

BG = (14, 15, 19)
BG_GRID = (22, 23, 29)
PANEL = (24, 25, 31)
CARD = (32, 34, 42)
CARD_HI = (40, 42, 52)
ROAD = (52, 54, 62)
ROAD_EDGE = (110, 114, 128)
LANE_LINE = (84, 87, 100)
TEXT = (236, 237, 240)
DIM = (156, 162, 176)
FAINT = (96, 102, 118)
OK = (110, 220, 130)
WARN = (255, 176, 70)
BAD = (255, 92, 92)
GOLD = (255, 214, 92)

ROAD_HALF_MM = 112.0
LANES_MM = (-68.0, -23.0, 23.0, 68.0)

BATTERY_COLORS = {"ok": OK, "weak": WARN, "low": WARN, "critical": BAD, "unknown": FAINT, "charging": (120, 200, 255)}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def with_alpha(color, a: int) -> tuple:
    return (color[0], color[1], color[2], max(0, min(255, int(a))))


def glow(surface: pygame.Surface, pos: tuple[int, int], radius: int, color, strength: float = 1.0) -> None:
    """Soft radial glow blitted additively."""
    r = max(2, int(radius))
    s = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
    steps = 4
    for i in range(steps, 0, -1):
        rr = int(r * i / steps)
        a = int(26 * strength * (1 - (i - 1) / steps))
        pygame.draw.circle(s, with_alpha(color, a), (r, r), rr)
    surface.blit(s, (pos[0] - r, pos[1] - r))


def draw_battery_icon(surface, x: int, y: int, w: int, h: int, pct: Optional[int], color) -> None:
    pygame.draw.rect(surface, DIM, (x, y, w, h), 1, border_radius=2)
    pygame.draw.rect(surface, DIM, (x + w, y + h // 4, 3, h // 2), border_radius=1)
    if pct is not None:
        fill = int((w - 4) * pct / 100)
        if fill > 0:
            pygame.draw.rect(surface, color, (x + 2, y + 2, fill, h - 4), border_radius=1)


class CarView:
    """Per-car display state: smoothed position and a fading trail."""

    def __init__(self):
        self.x: Optional[float] = None
        self.y: Optional[float] = None
        self.trail: deque[tuple[float, float, float]] = deque(maxlen=22)
        self.hp_lag = float(MAX_HP)

    def update(self, target: Optional[tuple[float, float]], now: float, dt: float, hp: int) -> None:
        if target is not None:
            if self.x is None or self.y is None or math.hypot(target[0] - self.x, target[1] - self.y) > 400:
                self.x, self.y = target
            else:
                k = min(1.0, dt * 9.0)
                self.x = lerp(self.x, target[0], k)
                self.y = lerp(self.y, target[1], k)
            if not self.trail or math.hypot(self.x - self.trail[-1][0], self.y - self.trail[-1][1]) > 12:
                self.trail.append((self.x, self.y, now))
        self.hp_lag = max(hp, self.hp_lag - dt * 60) if self.hp_lag > hp else float(hp)


class MapView:
    def __init__(self, track: Track, rect: pygame.Rect, margin: int = 46):
        self.track = track
        self.rect = rect
        x0, y0, x1, y1 = track.bounds()
        x0 -= ROAD_HALF_MM; y0 -= ROAD_HALF_MM; x1 += ROAD_HALF_MM; y1 += ROAD_HALF_MM
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        self.scale = min((rect.width - 2 * margin) / w, (rect.height - 2 * margin) / h)
        self.ox = rect.x + (rect.width - w * self.scale) / 2 - x0 * self.scale
        self.oy = rect.y + (rect.height - h * self.scale) / 2 - y0 * self.scale
        self._road: Optional[pygame.Surface] = None
        self.views: dict[int, CarView] = {}
        self.last_t = time.monotonic()

    def to_screen(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        return int(self.ox + x_mm * self.scale), int(self.oy + y_mm * self.scale)

    # ---- static road layer ----
    def _draw_road(self, surf: pygame.Surface) -> None:
        t = self.track
        # background grid
        for gx in range(0, surf.get_width(), 40):
            pygame.draw.line(surf, BG_GRID, (gx, 0), (gx, surf.get_height()))
        for gy in range(0, surf.get_height(), 40):
            pygame.draw.line(surf, BG_GRID, (0, gy), (surf.get_width(), gy))
        # drop shadow + asphalt
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            left = [self.to_screen(*t.world_point(i, k / steps, -ROAD_HALF_MM)) for k in range(steps + 1)]
            right = [self.to_screen(*t.world_point(i, k / steps, ROAD_HALF_MM)) for k in range(steps + 1)]
            shadow = [(x + 4, y + 5) for x, y in left + right[::-1]]
            pygame.draw.polygon(surf, (10, 10, 13), shadow)
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            left = [self.to_screen(*t.world_point(i, k / steps, -ROAD_HALF_MM)) for k in range(steps + 1)]
            right = [self.to_screen(*t.world_point(i, k / steps, ROAD_HALF_MM)) for k in range(steps + 1)]
            pygame.draw.polygon(surf, ROAD, left + right[::-1])
        # dashed lane lines
        for i, p in enumerate(t.pieces):
            steps = 24 if p.kind == TURN else 12
            for lane in LANES_MM:
                pts = [self.to_screen(*t.world_point(i, k / steps, lane)) for k in range(steps + 1)]
                for k in range(0, steps, 2):
                    pygame.draw.line(surf, LANE_LINE, pts[k], pts[k + 1], 1)
        # edges
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            for edge in (-ROAD_HALF_MM, ROAD_HALF_MM):
                pts = [self.to_screen(*t.world_point(i, k / steps, edge)) for k in range(steps + 1)]
                pygame.draw.lines(surf, ROAD_EDGE, False, pts, 2)
        # piece joints
        for i, p in enumerate(t.pieces):
            a = self.to_screen(*t.world_point(i, 0.0, -ROAD_HALF_MM))
            b = self.to_screen(*t.world_point(i, 0.0, ROAD_HALF_MM))
            pygame.draw.line(surf, (70, 72, 84), a, b, 1)
        # start / finish
        for i, p in enumerate(t.pieces):
            if p.kind != START:
                continue
            n = 8
            for row in range(2):
                for j in range(n):
                    a = -ROAD_HALF_MM + j * (2 * ROAD_HALF_MM / n)
                    b = a + 2 * ROAD_HALF_MM / n
                    f0, f1 = 0.47 + row * 0.03, 0.50 + row * 0.03
                    quad = [self.to_screen(*t.world_point(i, f0, a)), self.to_screen(*t.world_point(i, f0, b)),
                            self.to_screen(*t.world_point(i, f1, b)), self.to_screen(*t.world_point(i, f1, a))]
                    pygame.draw.polygon(surf, (235, 235, 235) if (j + row) % 2 == 0 else (28, 28, 32), quad)
            ax, ay = self.to_screen(*t.world_point(i, 0.66, 0))
            bx, by = self.to_screen(*t.world_point(i, 0.90, 0))
            pygame.draw.line(surf, (130, 136, 150), (ax, ay), (bx, by), 3)
            ang = math.atan2(by - ay, bx - ax)
            pygame.draw.polygon(surf, (130, 136, 150), [(bx, by),
                                                        (bx - 11 * math.cos(ang - 0.5), by - 11 * math.sin(ang - 0.5)),
                                                        (bx - 11 * math.cos(ang + 0.5), by - 11 * math.sin(ang + 0.5))])

    def road(self) -> pygame.Surface:
        if self._road is None:
            s = pygame.Surface(self.rect.size)
            s.fill(PANEL)
            saved = (self.ox, self.oy)
            self.ox -= self.rect.x; self.oy -= self.rect.y
            self._draw_road(s)
            self.ox, self.oy = saved
            self._road = s
        return self._road

    # ---- dynamic layer ----
    def draw(self, screen: pygame.Surface, game: Game, font: pygame.font.Font, small: pygame.font.Font) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self.last_t)
        self.last_t = now
        screen.blit(self.road(), self.rect.topleft)
        clip = screen.get_clip()
        screen.set_clip(self.rect)

        # mines
        for m in game.mines:
            x, y = self.to_screen(m.x, m.y)
            armed = now - m.placed_at > MINE_ARM_S
            pulse = 0.5 + 0.5 * math.sin(now * 6)
            if armed:
                glow(screen, (x, y), 16 + int(6 * pulse), (255, 120, 40), 0.9)
            pygame.draw.circle(screen, (255, 130, 50) if armed else (140, 90, 50), (x, y), 6)
            pygame.draw.circle(screen, (60, 25, 10), (x, y), 3)
            pygame.draw.circle(screen, m.owner.color, (x, y), 9, 1)

        # trails
        for c in game.cars:
            v = self.views.setdefault(c.index, CarView())
            v.update(c.world() if c.localized() else None, now, dt, c.hp)
            pts = list(v.trail)
            for i in range(1, len(pts)):
                age = now - pts[i][2]
                if age > 1.2:
                    continue
                a = int(120 * (1 - age / 1.2))
                w = 3 if i > len(pts) - 6 else 2
                s = pygame.Surface((abs(int((pts[i][0] - pts[i - 1][0]) * self.scale)) + 6,
                                    abs(int((pts[i][1] - pts[i - 1][1]) * self.scale)) + 6), pygame.SRCALPHA)
                p0 = self.to_screen(*pts[i - 1][:2]); p1 = self.to_screen(*pts[i][:2])
                ox, oy = min(p0[0], p1[0]) - 3, min(p0[1], p1[1]) - 3
                pygame.draw.line(s, with_alpha(c.color, a), (p0[0] - ox, p0[1] - oy), (p1[0] - ox, p1[1] - oy), w)
                screen.blit(s, (ox, oy))

        # lasers
        for l in game.lasers:
            a = self.to_screen(l.x0, l.y0)
            b = self.to_screen(l.x1, l.y1)
            life = max(0.0, l.until - now)
            pygame.draw.line(screen, l.color, a, b, 5 if l.hit else 2)
            pygame.draw.line(screen, (255, 255, 255), a, b, 1)
            if l.hit:
                glow(screen, b, 22, l.color, 1.2)
                pygame.draw.circle(screen, (255, 255, 255), b, 6 + int(10 * (1 - life / 0.25)), 2)

        # cars
        for c in game.cars:
            v = self.views[c.index]
            if v.x is None or not c.localized():
                continue
            x, y = self.to_screen(v.x, v.y)
            ang = c.loc.heading()  # type: ignore[union-attr]
            dead = c.is_dead(now)
            color = (80, 80, 86) if dead else c.color
            if not dead:
                glow(screen, (x, y), 24, c.color, 0.8)
            # body: rounded rectangle rotated along heading
            L, W = 22, 12
            cs, sn = math.cos(ang), math.sin(ang)
            def pt(dx, dy):
                return (x + dx * cs - dy * sn, y + dx * sn + dy * cs)
            body = [pt(L / 2, -W / 2 + 3), pt(L / 2 - 3, -W / 2), pt(-L / 2 + 2, -W / 2), pt(-L / 2, -W / 2 + 2),
                    pt(-L / 2, W / 2 - 2), pt(-L / 2 + 2, W / 2), pt(L / 2 - 3, W / 2), pt(L / 2, W / 2 - 3)]
            pygame.draw.polygon(screen, (12, 12, 15), [(px + 2, py + 3) for px, py in body])
            pygame.draw.polygon(screen, color, body)
            pygame.draw.polygon(screen, (20, 20, 24), body, 2)
            roof = [pt(4, -W / 2 + 3), pt(-5, -W / 2 + 3), pt(-5, W / 2 - 3), pt(4, W / 2 - 3)]
            pygame.draw.polygon(screen, (28, 30, 36), roof)
            # headlights
            for dy in (-W / 2 + 3, W / 2 - 3):
                hx, hy = pt(L / 2, dy)
                pygame.draw.circle(screen, (255, 250, 210), (int(hx), int(hy)), 2)
            if now < c.stun_until:
                pygame.draw.circle(screen, WARN, (x, y), 20, 2)
            elif now < c.penalty_until:
                pygame.draw.circle(screen, with_alpha(WARN, 160)[:3], (x, y), 18, 1)
            label = small.render(c.name, True, TEXT)
            lx, ly = x - label.get_width() // 2, y - 30
            pygame.draw.rect(screen, (0, 0, 0), (lx - 4, ly - 1, label.get_width() + 8, label.get_height() + 2), border_radius=4)
            screen.blit(label, (lx, ly))
            bw = 40
            pygame.draw.rect(screen, (30, 30, 36), (x - bw // 2, y + 16, bw, 5), border_radius=2)
            pygame.draw.rect(screen, BAD, (x - bw // 2, y + 16, int(bw * v.hp_lag / MAX_HP), 5), border_radius=2)
            pygame.draw.rect(screen, OK if c.hp > 40 else (255, 150, 90), (x - bw // 2, y + 16, int(bw * c.hp / MAX_HP), 5), border_radius=2)

        # effects
        for e in game.fx:
            t = (now - e.t0) / e.duration
            x, y = self.to_screen(e.x, e.y)
            if e.kind == "ring":
                pygame.draw.circle(screen, e.color, (x, y), int(8 + 34 * t), max(1, int(4 * (1 - t))))
            elif e.kind == "boom":
                glow(screen, (x, y), int(20 + 50 * t), e.color, 2.0 * (1 - t))
                for k in range(10):
                    rnd = random.Random(k + int(e.t0 * 1000))
                    a = rnd.uniform(0, math.tau); spd = rnd.uniform(30, 90)
                    px, py = x + math.cos(a) * spd * t, y + math.sin(a) * spd * t
                    pygame.draw.circle(screen, e.color if k % 2 else (255, 230, 160), (int(px), int(py)), max(1, int(4 * (1 - t))))
                pygame.draw.circle(screen, (255, 255, 255), (x, y), int(6 + 40 * t), max(1, int(3 * (1 - t))))
            elif e.kind == "text":
                img = font.render(e.text, True, e.color)
                img.set_alpha(int(255 * (1 - t)))
                screen.blit(img, (x + 16, y - 30 - int(30 * t)))
            elif e.kind == "spawn":
                pygame.draw.circle(screen, e.color, (x, y), int(40 * (1 - t)), 2)

        screen.set_clip(clip)

        # off-track cars, parked in a corner of the map
        off = [c for c in game.cars if not c.localized()]
        if off:
            x0, y0 = self.rect.x + 16, self.rect.bottom - 16 - 22 * len(off)
            for i, c in enumerate(off):
                y = y0 + i * 22
                pygame.draw.circle(screen, (70, 70, 78), (x0 + 8, y + 8), 7)
                pygame.draw.circle(screen, c.color, (x0 + 8, y + 8), 7, 2)
                st = c.track_status()
                msg = {"link lost": "link lost", "off track": "NOT ON THE TRACK", "idle": "stopped, position unknown",
                       "charging": "on the charging pad"}.get(st, st)
                col = WARN if st == "off track" else ((120, 200, 255) if st == "charging" else DIM)
                screen.blit(small.render(f"{c.name}: {msg}", True, col), (x0 + 22, y))


class Renderer:
    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self.font = pygame.font.SysFont("consolas", 16)
        self.small = pygame.font.SysFont("consolas", 13)
        self.title = pygame.font.SysFont("segoeui,consolas", 30, bold=True)
        self.big = pygame.font.SysFont("segoeui,consolas", 22, bold=True)
        self.mono_big = pygame.font.SysFont("consolas", 22, bold=True)
        self.map_view: Optional[MapView] = None
        self.map_key: Optional[tuple] = None
        self.buttons: dict[str, pygame.Rect] = {}   # clickable areas of the current screen

    # ---- helpers ----
    def text(self, s: str, pos, color=TEXT, font=None) -> int:
        f = font or self.font
        img = f.render(s, True, color)
        self.screen.blit(img, pos)
        return img.get_height()

    def text_right(self, s: str, right: int, y: int, color=TEXT, font=None) -> None:
        f = font or self.font
        img = f.render(s, True, color)
        self.screen.blit(img, (right - img.get_width(), y))

    def map_rect(self) -> pygame.Rect:
        w, h = self.screen.get_size()
        return pygame.Rect(12, 12, int(w * 0.62), h - 24)

    def panel_rect(self) -> pygame.Rect:
        w, h = self.screen.get_size()
        mr = self.map_rect()
        return pygame.Rect(mr.right + 12, 12, w - mr.right - 24, h - 24)

    def ensure_map(self, track: Optional[Track]) -> Optional[MapView]:
        if track is None:
            self.map_view = None
            self.map_key = None
            return None
        key = (id(track), self.screen.get_size())
        if self.map_view is None or self.map_key != key:
            self.map_view = MapView(track, self.map_rect())
            self.map_key = key
        return self.map_view

    def draw_no_track(self, msg: str) -> None:
        r = self.map_rect()
        pygame.draw.rect(self.screen, PANEL, r, border_radius=10)
        self.text("No track map yet", (r.x + 28, r.y + 28), DIM, self.title)
        y = r.y + 80
        for line in msg.split("\n"):
            y += self.text(line, (r.x + 28, y), DIM) + 6

    def chip(self, x: int, y: int, label: str, color) -> int:
        img = self.small.render(label, True, (18, 18, 22))
        w = img.get_width() + 12
        pygame.draw.rect(self.screen, color, (x, y, w, 18), border_radius=9)
        self.screen.blit(img, (x + 6, y + 2))
        return w

    def battery_row(self, c: CarState, x: int, y: int, w: int) -> None:
        b = c.battery
        st = b.status
        col = BATTERY_COLORS.get(st, FAINT)
        if st == "charging":
            # animated fill while on the pad
            pct = int((time.monotonic() * 40) % 100)
            draw_battery_icon(self.screen, x, y + 2, 26, 12, pct, col)
            mv = f"{b.mv / 1000:.2f} V" if b.mv else "?.?? V"
            full = c.car.status is not None and c.car.status.battery_charged
            self.text(f"{mv}  charging", (x + 36, y), col, self.small)
            self.chip(x + w - 90, y - 2, "CHARGED" if full else "CHARGING", OK if full else col)
            return
        draw_battery_icon(self.screen, x, y + 2, 26, 12, b.percent, col)
        mv = f"{b.mv / 1000:.2f} V" if b.mv else "?.?? V"
        pct = f"{b.percent:3d}%" if b.percent is not None else "  ?%"
        s = f"{mv} {pct}"
        sag = b.sag_mv
        if sag:
            s += f"  sag {sag / 1000:.2f}"
        self.text(s, (x + 36, y), DIM, self.small)
        if st != "ok" and st != "unknown":
            label = {"weak": "WEAK CELL", "low": "LOW", "critical": "CRITICAL"}[st]
            self.chip(x + w - 90, y - 2, label, col)

    # ---- screens ----
    def frame(self) -> None:
        self.screen.fill(BG)

    def draw_car_icon(self, cx: int, cy: int, color, scale: float = 2.2, dead: bool = False) -> None:
        L, W = 22 * scale, 12 * scale
        col = (80, 80, 86) if dead else color
        body = [(cx + L / 2, cy - W / 2 + 3 * scale), (cx + L / 2 - 3 * scale, cy - W / 2), (cx - L / 2 + 2 * scale, cy - W / 2),
                (cx - L / 2, cy - W / 2 + 2 * scale), (cx - L / 2, cy + W / 2 - 2 * scale), (cx - L / 2 + 2 * scale, cy + W / 2),
                (cx + L / 2 - 3 * scale, cy + W / 2), (cx + L / 2, cy + W / 2 - 3 * scale)]
        pygame.draw.polygon(self.screen, (12, 12, 15), [(x + 3, y + 4) for x, y in body])
        pygame.draw.polygon(self.screen, col, body)
        pygame.draw.polygon(self.screen, (20, 20, 24), body, 2)
        pygame.draw.rect(self.screen, (28, 30, 36), (cx - 5 * scale, cy - W / 2 + 3 * scale, 9 * scale, W - 6 * scale), border_radius=3)
        for dy in (-W / 2 + 3 * scale, W / 2 - 3 * scale):
            pygame.draw.circle(self.screen, (255, 250, 210), (int(cx + L / 2), int(cy + dy)), int(2 * scale))

    def token(self, x: int, y: int, source, color, filled: bool = True) -> int:
        """Small player badge like [P1] / [KB]. Returns its width."""
        img = self.small.render(source.short, True, (18, 18, 22) if filled else color)
        w = img.get_width() + 14
        if filled:
            pygame.draw.rect(self.screen, color, (x, y, w, 20), border_radius=6)
        else:
            pygame.draw.rect(self.screen, color, (x, y, w, 20), 2, border_radius=6)
        self.screen.blit(img, (x + 7, y + 3))
        return w

    def draw_pairing(self, game: Game, sources: list, cursors: dict, claims: dict, status: str, controls: str) -> None:
        self.frame()
        W, H = self.screen.get_size()
        sources = [s for s in sources if s.alive()]
        src_color = {}
        palette = [(255, 214, 92), (120, 200, 255), (200, 150, 255), (140, 240, 170), (255, 160, 120)]
        for i, s in enumerate(sources):
            src_color[s] = palette[i % len(palette)]

        # header
        self.text("PAIRING", (28, 18), TEXT, self.title)
        self.text("Every controller has a cursor. Move it onto a car and press A / Enter to drive that car. "
                  "Cars nobody takes are driven by the AI (X / Tab switches a car between AI and parked).", (28, 60), DIM, self.small)

        # car cards
        n = max(1, len(game.cars))
        pad = 22
        card_w = min(330, (W - 56 - pad * (n - 1)) // n)
        card_h = 300
        total = n * card_w + (n - 1) * pad
        x0 = (W - total) // 2
        top = 130
        for i, c in enumerate(game.cars):
            x = x0 + i * (card_w + pad)
            box = pygame.Rect(x, top, card_w, card_h)
            owner = next((s for s, cc in claims.items() if cc is c), None)
            hovering = [s for s in sources if cursors.get(s, 0) == c.index and claims.get(s) is not c]
            # cursors floating above the card
            tx = x
            for s in hovering:
                tx += self.token(tx, top - 28, s, src_color[s]) + 6
                pygame.draw.polygon(self.screen, src_color[s], [(tx - 20, top - 6), (tx - 12, top - 6), (tx - 16, top - 1)])
            pygame.draw.rect(self.screen, CARD_HI if (owner or hovering) else CARD, box, border_radius=14)
            if hovering:
                pygame.draw.rect(self.screen, src_color[hovering[0]], box, 3, border_radius=14)
            pygame.draw.rect(self.screen, c.color, (box.x, box.y, box.width, 8), border_top_left_radius=14, border_top_right_radius=14)
            self.draw_car_icon(box.centerx, box.y + 56, c.color, 2.4)
            self.text(c.name, (box.x + 18, box.y + 92), TEXT, self.big)
            st = c.track_status()
            label = {"on track": "connected  ·  on track", "idle": "connected  ·  stopped (position unknown until it drives)",
                     "off track": "connected  ·  NOT ON THE TRACK", "link lost": "LINK LOST - reconnecting",
                     "charging": "connected  ·  ON ITS CHARGER (take it off to race)"}[st]
            col = OK if st == "on track" else (DIM if st == "idle" else ((120, 200, 255) if st == "charging" else WARN))
            self.text(label, (box.x + 18, box.y + 124), col, self.small)
            self.battery_row(c, box.x + 18, box.y + 146, box.width - 36)

            # driver slot
            sy = box.y + 184
            self.text("DRIVER", (box.x + 18, sy), FAINT, self.small)
            slot = pygame.Rect(box.x + 18, sy + 18, box.width - 36, 44)
            if owner is not None:
                pygame.draw.rect(self.screen, src_color[owner], slot, border_radius=10)
                self.text(f"{owner.short}  {owner.name}"[:30], (slot.x + 12, slot.y + 11), (18, 18, 22), self.font)
                hint = "A/Enter or B/Backspace: release"
            elif game.ai_enabled and c.ai_pref:
                pygame.draw.rect(self.screen, (52, 56, 70), slot, border_radius=10)
                self.text("AI", (slot.x + 12, slot.y + 10), TEXT, self.big)
                self.text("computer drives this car", (slot.x + 52, slot.y + 14), DIM, self.small)
                hint = "A/Enter: take it   X/Tab: park it"
            else:
                pygame.draw.rect(self.screen, (44, 46, 56), slot, 2, border_radius=10)
                self.text("PARKED", (slot.x + 12, slot.y + 10), FAINT, self.big)
                self.text("stays still", (slot.x + 108, slot.y + 14), FAINT, self.small)
                hint = "A/Enter: take it   X/Tab: give it to the AI"
            self.text(hint, (box.x + 18, box.y + card_h - 26), FAINT, self.small)

        # controllers legend
        ly = top + card_h + 36
        self.text("CONTROLLERS", (28, ly), FAINT, self.small)
        ly += 20
        for s in sources:
            w = self.token(28, ly, s, src_color[s])
            car = claims.get(s)
            if car is not None:
                what = f"{s.name}  ->  drives {car.name}"
                col = TEXT
            else:
                what = f"{s.name}  ->  hovering {game.cars[cursors.get(s, 0)].name if game.cars else '-'} (press A / Enter to take it)"
                col = DIM
            self.text(what, (28 + w + 10, ly + 2), col, self.small)
            ly += 26
        pads = [s for s in sources if s.short != "KB"]
        if not pads:
            self.text("no gamepad detected - turn one on, it joins automatically", (28, ly + 2), WARN, self.small)
            ly += 26

        # summary + status
        players = [f"{s.short}->{c.name}" for s, c in claims.items()]
        ais = [c.name for c in game.cars if c not in claims.values() and game.ai_enabled and c.ai_pref]
        parked = [c.name for c in game.cars if c not in claims.values() and not (game.ai_enabled and c.ai_pref)]
        summary = f"players: {', '.join(players) or 'none'}    AI: {', '.join(ais) or 'none'}    parked: {', '.join(parked) or 'none'}"
        self.text(summary, (28, ly + 10), DIM, self.font)
        if status:
            self.text(status, (28, ly + 36), WARN, self.font)
        # big buttons: RACE and SCAN TRACK (keyboard / pad shortcuts shown, also mouse-clickable)
        self.buttons = {}
        by = ly + 58
        race_ok = bool(claims)
        self.buttons["race"] = self.button(28, by, 240, 46, "RACE", "Start / Space",
                                           OK if race_ok else (60, 62, 74), enabled=race_ok)
        scan_car = next((c for c in game.cars if c.localized()), None)
        if scan_car is None and sources:
            scan_car = game.cars[cursors.get(sources[0], 0)] if game.cars else None
        connected = any(c.car.connected for c in game.cars)
        self.buttons["scan"] = self.button(28 + 240 + 16, by, 240, 46, "SCAN TRACK", "Y / T",
                                           (120, 170, 255), enabled=connected)
        # game mode selector
        mx = 28 + 2 * (240 + 16)
        race_mode = game.mode == "race"
        self.buttons["mode"] = self.button(mx, by, 200, 46, "RACE" if race_mode else "BATTLE", "M", (200, 150, 255) if race_mode else (255, 150, 90))
        self.buttons["laps"] = self.button(mx + 208, by, 84, 46, f"{game.lap_target}", "laps", (110, 112, 130) if race_mode else (70, 72, 84), enabled=race_mode)
        self.text("laps only, no weapons" if race_mode else "blasters, mines, kills", (mx, by + 52), FAINT, self.small)
        if not race_ok:
            self.text("take a car (A / Enter) to enable", (28, by + 52), FAINT, self.small)
        if scan_car is not None:
            self.text("all cars drive one lap", (28 + 256, by + 52), FAINT, self.small)

        # track map status + mini map bottom-right
        mini_rect = pygame.Rect(W - 28 - 360, H - 28 - 200 - 46, 360, 200)
        pygame.draw.rect(self.screen, CARD, mini_rect, border_radius=8)
        if game.track is not None:
            key = ("mini", id(game.track), mini_rect.size)
            if getattr(self, "_mini_key", None) != key:
                self._mini = MapView(game.track, mini_rect, margin=18)
                self._mini_key = key
            self._mini.rect = mini_rect
            self._mini.draw(self.screen, game, self.font, self.small)
            pygame.draw.rect(self.screen, (60, 62, 74), mini_rect, 1, border_radius=8)
            self.text(f"TRACK MAP  ·  {len(game.track)} pieces  ·  saved as track.json",
                      (mini_rect.x, mini_rect.y - 20), DIM, self.small)
        else:
            pygame.draw.rect(self.screen, WARN, mini_rect, 2, border_radius=8)
            self.text("NO TRACK MAP", (mini_rect.x + 16, mini_rect.y + 16), WARN, self.big)
            self.text("Put a car on the track and press", (mini_rect.x + 16, mini_rect.y + 56), DIM, self.small)
            self.text("SCAN TRACK (Y / T). Without a map the", (mini_rect.x + 16, mini_rect.y + 74), DIM, self.small)
            self.text("race has no positions and no weapons.", (mini_rect.x + 16, mini_rect.y + 92), DIM, self.small)

        y = H - 14 - 15 * (controls.count("\n") + 1)
        for line in controls.split("\n"):
            y += self.text(line, (28, y), FAINT, self.small) + 1

    def draw_scan(self, game: Game, scanners: dict, elapsed: float) -> None:
        self.frame()
        mv = self.ensure_map(game.track)
        if mv:
            mv.draw(self.screen, game, self.font, self.small)
        else:
            self.draw_no_track("scanning...")
        pr = self.panel_rect()
        pygame.draw.rect(self.screen, PANEL, pr, border_radius=10)
        x, y = pr.x + 18, pr.y + 14
        y += self.text("SCANNING TRACK", (x, y), TEXT, self.title) + 4
        y += self.text(f"all cars drive one lap   ·   elapsed {elapsed:3.0f}s", (x, y), DIM, self.small) + 14
        for c, sc in scanners.items():
            box = pygame.Rect(x, y, pr.width - 36, 92)
            pygame.draw.rect(self.screen, CARD, box, border_radius=10)
            pygame.draw.rect(self.screen, c.color, (box.x, box.y, 7, box.height), border_top_left_radius=10, border_bottom_left_radius=10)
            self.text(c.name, (box.x + 18, box.y + 8), TEXT, self.big)
            with sc.lock:
                state = sc.state
                lap = list(sc.lap)
                seen = sc.pieces_seen
            if state == "on the charger":
                col, label = (120, 200, 255), "ON ITS CHARGER - skipped"
            elif state == "not on the track":
                col, label = WARN, "NOT ON THE TRACK - read no codes, stopped"
            elif sc.done:
                col, label = OK, state
            elif seen == 0:
                col, label = DIM, "driving, waiting for the first track code..."
            else:
                col, label = TEXT, state
            self.text(label, (box.x + 18, box.y + 40), col, self.small)
            seq = "  ".join(("?" if e["id"] is None else str(e["id"])) + (e["turn"] or "") for e in lap[-12:])
            self.text(seq or "-", (box.x + 18, box.y + 62), FAINT, self.small)
            y += box.height + 8
        self.text("the first car to close a lap wins; cars reading nothing for 8 s are flagged as off the track",
                  (x, y + 6), FAINT, self.small)
        self.text("Back / Esc: cancel", (x, pr.bottom - 30), FAINT, self.small)

    def draw_race(self, game: Game, controls: str, overlay: Optional[str] = None, sub: str = "") -> None:
        self.frame()
        mv = self.ensure_map(game.track)
        if mv:
            mv.draw(self.screen, game, self.font, self.small)
        else:
            self.draw_no_track("No map: weapons and positions are disabled.\nGo back (Esc / Back) and scan the track.")
        pr = self.panel_rect()
        pygame.draw.rect(self.screen, PANEL, pr, border_radius=10)
        x, y = pr.x + 18, pr.y + 14
        now = time.monotonic()
        self.text("RACE" if game.mode == "race" else "BATTLE", (x, y), TEXT, self.title)
        if game.mode == "race":
            self.text(f"first to {game.lap_target} laps", (x + 92, y + 14), DIM, self.small)
        if game.race_start_t:
            t = now - game.race_start_t
            self.text_right(f"{int(t // 60):02d}:{t % 60:05.2f}", pr.right - 18, y + 8, DIM, self.mono_big)
        y += 44
        ranks = {c.index: i + 1 for i, c in enumerate(game.ranking())}
        n = len(game.cars)
        h = min(128, (pr.height - 250) // max(1, n) - 8)
        for c in game.cars:
            box = pygame.Rect(x, y, pr.width - 36, h)
            dead = c.is_dead(now)
            pygame.draw.rect(self.screen, CARD, box, border_radius=10)
            pygame.draw.rect(self.screen, c.color, (box.x, box.y, 7, h), border_top_left_radius=10, border_bottom_left_radius=10)
            # rank badge
            pygame.draw.circle(self.screen, GOLD if ranks[c.index] == 1 else (70, 72, 84), (box.x + 32, box.y + 20), 12)
            img = self.small.render(str(ranks[c.index]), True, (18, 18, 22) if ranks[c.index] == 1 else TEXT)
            self.screen.blit(img, (box.x + 32 - img.get_width() // 2, box.y + 13))
            self.text(c.name, (box.x + 52, box.y + 6), (120, 120, 126) if dead else TEXT, self.big)
            self.text(c.driver_label(), (box.x + 52, box.y + 34), DIM, self.small)
            # status chip
            cx = box.x + 52 + self.small.size(c.driver_label())[0] + 10
            if game.phase == "race" and game.wrong_way(c):
                self.chip(cx, box.y + 33, "WRONG WAY", BAD)
            elif c.grid_state == "off track":
                self.chip(cx, box.y + 33, "LEFT OUT (off track)", WARN)
            elif c.grid_state == "charging" or (c.charging and not dead):
                self.chip(cx, box.y + 33, "ON CHARGER", (120, 200, 255))
            elif game.phase in ("grid", "countdown") and c.grid_state:
                gs = c.grid_state
                self.chip(cx, box.y + 33, {"driving": "TO THE GRID", "approach": "LINING UP", "placed": "ON THE LINE", "off track": "LEFT OUT (off track)",
                                           "u-turn": "TURNING AROUND"}.get(gs, gs.upper()),
                          OK if gs == "placed" else (WARN if gs == "off track" else (120, 120, 130)))
            elif dead:
                self.chip(cx, box.y + 33, "DESTROYED", BAD)
            elif now < c.stun_until:
                self.chip(cx, box.y + 33, "STUNNED", WARN)
            elif now < c.penalty_until:
                self.chip(cx, box.y + 33, "SLOWED", WARN)
            elif not c.localized():
                st = c.track_status()
                self.chip(cx, box.y + 33, {"link lost": "LINK LOST", "off track": "OFF TRACK", "idle": "STOPPED", "charging": "ON CHARGER"}.get(st, st.upper()),
                          WARN if st == "off track" else ((120, 200, 255) if st == "charging" else (120, 120, 130)))
            bx, by, bw = box.x + 20, box.y + 54, box.width - 40
            if game.mode == "battle":
                # cooldown dials + K/D
                self._dial(box.right - 40, box.y + 26, 15, max(0.0, c.fire_ready_at - now) / BLASTER_COOLDOWN_S, c.color, "F")
                self._dial(box.right - 82, box.y + 26, 15, max(0.0, c.mine_ready_at - now) / MINE_COOLDOWN_S, (255, 140, 60), "M")
                self.text_right(f"K {c.kills}  D {c.deaths}", box.right - 104, box.y + 18, DIM, self.small)
                # HP bar with lag
                v = mv.views.get(c.index) if mv else None
                lag = v.hp_lag if v else c.hp
                pygame.draw.rect(self.screen, (40, 40, 48), (bx, by, bw, 9), border_radius=4)
                pygame.draw.rect(self.screen, (150, 60, 60), (bx, by, int(bw * lag / MAX_HP), 9), border_radius=4)
                pygame.draw.rect(self.screen, OK if c.hp > 40 else (255, 150, 90), (bx, by, int(bw * c.hp / MAX_HP), 9), border_radius=4)
                self.text_right(f"{c.hp}", box.right - 20, by - 14, DIM, self.small)
            else:
                # lap progress bar towards the target
                self.text_right(f"LAP {c.laps} / {game.lap_target}", box.right - 20, box.y + 12, TEXT, self.big)
                prog = c.loc.progress() if c.localized() and c.loc else None
                n = len(game.track.pieces) if game.track else 1
                frac = min(1.0, (c.laps + ((prog or 0.0) / n if prog is not None else 0.0)) / max(1, game.lap_target))
                pygame.draw.rect(self.screen, (40, 40, 48), (bx, by, bw, 9), border_radius=4)
                pygame.draw.rect(self.screen, c.color, (bx, by, int(bw * frac), 9), border_radius=4)
            # speed + lane
            sy = by + 15
            spd = c.speed_sent()
            pygame.draw.rect(self.screen, (40, 40, 48), (bx, sy, bw - 90, 5), border_radius=2)
            pygame.draw.rect(self.screen, c.color, (bx, sy, int((bw - 90) * min(1.0, spd / max(1, c.max_speed))), 5), border_radius=2)
            self.text(f"{spd:>4} / {c.max_speed} mm/s", (bx, sy + 8), DIM, self.small)
            for i, lane in enumerate(LANES_MM):
                lx = box.right - 20 - (4 - i) * 18
                on = abs(c.target_offset - lane) < 12
                pygame.draw.rect(self.screen, c.color if on else (55, 57, 68), (lx, sy - 1, 14, 8), border_radius=2)
            self.text_right("lane", box.right - 20, sy + 8, FAINT, self.small)
            # laps + battery
            ly = sy + 26
            if h >= 118:
                lap = f"lap {c.laps}"
                if c.last_lap_s:
                    lap += f"  last {c.last_lap_s:.2f}s"
                if c.best_lap_s:
                    lap += f"  best {c.best_lap_s:.2f}s"
                self.text(lap, (bx, ly), DIM, self.small)
                ly += 18
            self.battery_row(c, bx, ly, bw)
            y += h + 8
        # kill feed
        y += 8
        for t, msg, col in list(game.events)[:8]:
            age = now - t
            if y > pr.bottom - 60:
                break
            img = self.small.render(msg, True, col if age < 4 else DIM)
            img.set_alpha(255 if age < 6 else 140)
            self.screen.blit(img, (x, y))
            y += 17
        y = pr.bottom - 14 - 15 * (controls.count("\n") + 1)
        for line in controls.split("\n"):
            y += self.text(line, (x, y), FAINT, self.small) + 1
        if overlay:
            mr = self.map_rect()
            big = pygame.font.SysFont("segoeui,consolas", 96 if len(overlay) <= 3 else 44, bold=True)
            img = big.render(overlay, True, TEXT)
            shade = pygame.Surface((img.get_width() + 80, img.get_height() + 60), pygame.SRCALPHA)
            shade.fill((10, 10, 14, 170))
            ox, oy = mr.centerx - shade.get_width() // 2, mr.centery - shade.get_height() // 2
            self.screen.blit(shade, (ox, oy))
            self.screen.blit(img, (mr.centerx - img.get_width() // 2, mr.centery - img.get_height() // 2 - 8))
            if sub:
                simg = self.font.render(sub, True, DIM)
                self.screen.blit(simg, (mr.centerx - simg.get_width() // 2, oy + shade.get_height() - 30))

    def button(self, x: int, y: int, w: int, h: int, label: str, hint: str, color, enabled: bool = True) -> pygame.Rect:
        rect = pygame.Rect(x, y, w, h)
        if enabled:
            pygame.draw.rect(self.screen, color, rect, border_radius=10)
            fg, sub = (18, 18, 22), (30, 32, 40)
        else:
            pygame.draw.rect(self.screen, (44, 46, 56), rect, border_radius=10)
            pygame.draw.rect(self.screen, (60, 62, 74), rect, 2, border_radius=10)
            fg, sub = FAINT, FAINT
        self.text(label, (x + 16, y + 8), fg, self.big)
        self.text_right(hint, x + w - 14, y + 16, sub, self.small)
        return rect

    def _dial(self, cx: int, cy: int, r: int, frac: float, color, label: str) -> None:
        pygame.draw.circle(self.screen, (44, 46, 56), (cx, cy), r)
        if frac <= 0:
            pygame.draw.circle(self.screen, color, (cx, cy), r, 3)
        else:
            rect = pygame.Rect(cx - r, cy - r, 2 * r, 2 * r)
            pygame.draw.arc(self.screen, color, rect, math.pi / 2, math.pi / 2 + math.tau * (1 - frac), 3)
        img = self.small.render(label, True, TEXT if frac <= 0 else FAINT)
        self.screen.blit(img, (cx - img.get_width() // 2, cy - img.get_height() // 2))

    def draw_message(self, title: str, lines: list[str]) -> None:
        self.frame()
        w, h = self.screen.get_size()
        self.text(title, (40, h // 2 - 60), TEXT, self.title)
        y = h // 2 - 10
        for l in lines:
            y += self.text(l, (40, y), DIM) + 4
        pygame.display.flip()
