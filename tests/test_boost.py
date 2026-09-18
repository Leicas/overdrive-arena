"""Boost constraints and full player throttle."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from anki.track import Track, Piece, START, TURN
from game.game import CarState, Game
from game import inputs as I

class BoostTests(unittest.TestCase):
    def setUp(self):
        v = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.c = CarState(v, 0, Mock(), 800)
        self.g = Game(Mock(), [self.c], Track([Piece(34, START), Piece(18, TURN, 'R')]), True, None)
        self.g.running = True
        self.c.ai_difficulty = 'extreme'
        self.now = time.monotonic()
        self.c.loc.index, self.c.loc.frac, self.c.loc.on_track = 0, 0.05, True
        self.c.loc.speed = 800
        self.c.loc.last_update = self.now
        self.c.loc.lane_mm = self.c.target_offset = 23

    def test_clear_straight_boosts_and_cools_down(self):
        self.assertTrue(self.g.request_boost(self.c, self.now))
        target = self.g.boost_target(self.c, 800, self.now)
        self.assertGreater(target, 800)
        self.assertLessEqual(target, 1200)
        self.assertFalse(self.g.request_boost(self.c, self.now + 0.1))
        self.assertEqual(self.g.boost_target(self.c, 800, self.now + 0.7), 800)

    def test_boost_never_raises_curve_target(self):
        self.g.request_boost(self.c, self.now)
        self.c.loc.index = 1
        self.assertEqual(self.g.ai_target_speed(self.c, 1200), self.g.ai_target_speed(self.c))
        self.assertEqual(self.g.boost_target(self.c, 800, self.now), 800)
        self.assertEqual(self.c.boost_until, 0)

    def test_no_boost_with_stale_telemetry_or_lane_change(self):
        self.c.loc.last_update = self.now - 0.5
        self.assertFalse(self.g.request_boost(self.c, self.now))
        self.c.loc.last_update = self.now
        self.c.target_offset = -23
        self.assertFalse(self.g.request_boost(self.c, self.now))

    def test_no_boost_when_traffic_ahead(self):
        v = SimpleNamespace(model='Other', battery_mv=0, connected=True, on_charger=False)
        other = CarState(v, 1, Mock(), 800)
        other.set_track(self.g.track)
        other.loc.index, other.loc.frac, other.loc.on_track = 0, 0.4, True
        other.loc.last_update = self.now
        other.loc.lane_mm = other.target_offset = 23
        self.g.cars.append(other)
        self.assertFalse(self.g.request_boost(self.c, self.now))

    def test_player_full_throttle_reaches_2000_without_changing_ai_limit(self):
        source = Mock()
        source.pressed.return_value = False
        source.throttle.return_value, source.brake.return_value, source.steer.return_value = 1, 0, 0
        self.c.source = source
        self.c.command_speed = Mock()
        self.g._human(self.c, 0.02, self.now)
        self.c.command_speed.assert_called_with(2000, self.now)
        source.pressed.side_effect = lambda action: action == I.LIMIT_DOWN
        self.g._human(self.c, 0.02, self.now + 1)
        self.c.command_speed.assert_called_with(1900, self.now + 1)
        self.c.source = None
        self.assertEqual(self.c.max_speed, 800)
        self.c.source = source
        self.assertEqual(self.c.max_speed, 1900)

if __name__ == '__main__':
    unittest.main()
