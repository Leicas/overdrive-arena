"""Start-line overshoot handling without physical cars."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from anki.track import Track, Piece, START, TURN
from game.game import CarState, Game

class GridTests(unittest.TestCase):
    def setUp(self):
        v = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.c = CarState(v, 0, Mock(), 800)
        self.g = Game(Mock(), [self.c], Track([Piece(34, START), Piece(18, TURN, 'R')]), True, None)
        self.c.grid_state = 'driving'
        self.c.grid_t0 = time.monotonic()
        self.c.loc.index, self.c.loc.on_track = 0, True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.lane_mm = self.c.grid_lane = 23
        self.c.command_speed = Mock()
        self.c.hard_stop = Mock()
        self.c.apply_lane = Mock()
        self.c.lights_idle = Mock()

    def test_overshoot_retries_only_once_until_next_lap(self):
        self.c.loc.frac = 0.85
        self.g.grid_tick(0)
        for _ in range(10):
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_laps, 1)
        self.c.hard_stop.assert_not_called()
        self.c.loc.index, self.c.loc.frac = 1, 0.2
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 0, 0.52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.c.hard_stop.assert_called_once()

    def test_missing_bar_transition_does_not_park_beyond_line(self):
        self.c.loc.frac = 0.4
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac = 1, 0.1
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.c.hard_stop.assert_not_called()

    def test_failed_attempt_limit_excludes_car(self):
        self.c.grid_laps = 2
        self.c.loc.frac = 0.85
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'missed start')
        self.g.start()
        self.assertFalse(self.c.participates())

    def test_wrong_lane_also_requires_full_retry(self):
        self.c.loc.lane_mm = -68
        self.c.loc.frac, self.c.loc.sub_id = 0.52, 33
        for _ in range(10):
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_laps, 1)
        self.assertEqual(self.c.grid_state, 'retry lap')

if __name__ == '__main__':
    unittest.main()
