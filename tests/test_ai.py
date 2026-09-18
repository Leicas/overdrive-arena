"""Offline race eligibility and AI driving regression tests."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Piece, Track, STRAIGHT, TURN, UNKNOWN
from game.game import CarState, Game


class AITests(unittest.TestCase):
    def setUp(self):
        vehicle = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.car = CarState(vehicle, 0, Mock(), 800)
        self.track = Track([Piece(36, STRAIGHT), Piece(39, STRAIGHT), Piece(17, TURN, 'R')])
        self.game = Game(Mock(), [self.car], self.track, True, 450)
        self.car.loc.index = 0
        self.car.loc.on_track = True
        self.car.loc.last_update = time.monotonic()
        self.car.loc.frac = 0.1

    def test_extreme_automatic_ceiling_and_explicit_limit(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.car.extreme_max_speed = 2000
        self.track.pieces[:] = [Piece(36, STRAIGHT) for _ in range(10)]
        self.assertEqual(self.game.ai_target_speed(self.car), 2000)
        self.car.extreme_max_speed = 900
        self.assertEqual(self.game.ai_target_speed(self.car), 900)
        self.car.ai_difficulty = 'hard'
        self.assertEqual(self.game.ai_target_speed(self.car), 800)

    def test_extreme_learned_grip_scales_with_radius(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.car.extreme_max_speed = 2000
        self.car.loc.index = 2
        self.car.loc.lane_mm = self.car.target_offset = 0
        radius = self.game.corner_radius(self.car, self.track.pieces[2])
        self.car.ai_corner_limit = 600
        self.car.ai_corner_radius = radius
        self.assertAlmostEqual(self.game.ai_target_speed(self.car), 600)
        self.car.ai_corner_radius = radius / 2
        self.assertAlmostEqual(self.game.ai_target_speed(self.car), 600 * 2 ** .5)

    def test_extreme_learned_corner_slows_approach_not_long_straights(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.car.extreme_max_speed = 2000
        self.car.loc.index, self.car.loc.frac = 1, .9
        before = self.game.ai_target_speed(self.car)
        self.car.ai_corner_limit = 500
        self.assertLess(self.game.ai_target_speed(self.car), before)
        self.track.pieces[:] = [Piece(36, STRAIGHT) for _ in range(12)] + [Piece(17, TURN, 'R')]
        self.car.loc.index, self.car.loc.frac = 0, 0
        self.assertEqual(self.game.ai_target_speed(self.car), 2000)

    def test_ai_only_and_eligibility(self):
        self.assertTrue(self.game.can_start([]))
        self.game.assign_roles()
        self.assertTrue(self.car.participates())
        self.car.ai_pref = False
        self.assertFalse(self.game.can_start([]))
        self.assertTrue(self.game.can_start([self.car]))
        self.car.ai_pref = True
        self.game.ai_enabled = False
        self.assertFalse(self.game.can_start([]))
        self.car.car.on_charger = True
        self.assertFalse(self.game.can_start([self.car]))
        self.car.car.on_charger = False
        self.car.car.connected = False
        self.assertFalse(self.game.can_start([self.car]))

    def test_pairing_ready_and_mouse_allow_ai_only(self):
        import pygame
        from app import App
        from game import inputs as I
        for click in (None, (5, 5)):
            app = App.__new__(App)
            app.game, app.cars, app.claims = self.game, [self.car], {}
            app.pads = []
            app.keyboard = Mock()
            app.keyboard.pressed.side_effect = lambda action: action == I.READY
            app.keyboard.alive.return_value = True
            app.cursors = {}
            app.click = click
            app.renderer = SimpleNamespace(buttons={'race': pygame.Rect(0, 0, 20, 20)})
            app.start_race = Mock()
            app.update_identify_lights = Mock()
            self.assertTrue(app.pairing_tick())
            app.start_race.assert_called_once()

    def test_per_car_levels_and_cycle(self):
        speeds = []
        for level in ('easy', 'normal', 'hard'):
            self.car.ai_difficulty = level
            speeds.append(self.game.ai_target_speed(self.car))
        self.assertEqual(speeds, [360, 450, 562.5])
        self.car.cycle_ai()
        self.assertEqual(self.car.ai_difficulty, "extreme")
        self.car.cycle_ai()
        self.assertFalse(self.car.ai_pref)
        self.car.cycle_ai()
        self.assertTrue(self.car.ai_pref)
        self.assertEqual(self.car.ai_difficulty, 'easy')
        self.car.cycle_ai()
        self.assertEqual(self.car.ai_difficulty, 'normal')

    def test_curve_braking_and_exit(self):
        straight = self.game.ai_target_speed(self.car)
        self.car.loc.index, self.car.loc.frac = 1, 0.95
        approach = self.game.ai_target_speed(self.car)
        self.car.loc.index = 2
        curve = self.game.ai_target_speed(self.car)
        self.assertLess(approach, straight)
        self.assertEqual(approach, curve)
        self.car.loc.index, self.car.loc.frac = 0, 0.05
        self.assertGreater(self.game.ai_target_speed(self.car), curve)

    def test_default_hard_reaches_car_limit_on_current_oval(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'hard'
        self.track.pieces[1].kind = TURN
        self.car.loc.frac = 0.03
        self.assertEqual(self.game.ai_target_speed(self.car), 800)
        self.car.max_speed = 1000
        self.track.pieces[1].kind = STRAIGHT
        self.assertEqual(self.game.ai_target_speed(self.car), 1000)
        self.car.loc.index = 2
        self.assertLess(self.game.ai_target_speed(self.car), 1000)

    def test_hard_carries_more_speed_through_turns(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'hard'
        self.car.loc.index = 2
        self.assertEqual(self.game.ai_target_speed(self.car), 672)
        self.car.loc.index, self.car.loc.frac = 1, 0.6
        self.assertGreater(self.game.ai_target_speed(self.car), 672)

    def test_race_does_not_randomly_change_clear_lanes(self):
        self.car.command_speed = Mock()
        self.car.set_lane = Mock()
        self.game.mode = 'race'
        self.car.loc.lane_mm = self.car.target_offset = 68
        self.game._ai(self.car, 0.02, time.monotonic())
        self.car.set_lane.assert_not_called()

    def test_default_difficulty_speeds(self):
        self.game.ai_speed = None
        speeds = []
        for level in ('easy', 'normal', 'hard'):
            self.car.ai_difficulty = level
            speeds.append(self.game.ai_target_speed(self.car))
        self.assertEqual(speeds, [512, 640, 800])

    def test_wraparound_lookahead(self):
        self.track.pieces[0].kind = TURN
        self.track.pieces[2].kind = STRAIGHT
        self.car.loc.index, self.car.loc.frac = 2, 0.99
        self.assertEqual(self.game.ai_target_speed(self.car), 382.5)

    def test_limits_and_missing_localization(self):
        self.car.max_speed = 200
        self.assertLessEqual(self.game.ai_target_speed(self.car), 200)
        self.car.max_speed = 800
        self.car.loc.last_update = 0
        self.assertLessEqual(self.game.ai_target_speed(self.car), 300)
        self.car.loc.last_update = time.monotonic()
        self.track.pieces[0].kind = UNKNOWN
        self.assertEqual(self.game.ai_target_speed(self.car), 382.5)

    def add_traffic(self, frac, lane, speed=300):
        vehicle = SimpleNamespace(model='Traffic', battery_mv=0, connected=True, on_charger=False)
        other = CarState(vehicle, len(self.game.cars), Mock(), 800)
        other.set_track(self.track)
        other.loc.index, other.loc.frac = 0, frac
        other.loc.on_track, other.loc.last_update = True, time.monotonic()
        other.loc.lane_mm, other.loc.speed = lane, speed
        other.target_offset = lane
        self.game.cars.append(other)
        return other

    def test_race_traffic_slows_when_lanes_blocked(self):
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.car.loc.speed = 600
        self.add_traffic(0.5, -23, 200)
        self.add_traffic(0.1, -68, 600)
        self.add_traffic(0.1, 23, 600)
        target = self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertLessEqual(target, 200)
        self.assertEqual(self.car.target_offset, -23)

    def test_race_traffic_passes_into_clear_adjacent_lane(self):
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.car.loc.speed = 300
        self.add_traffic(0.8, -23, 250)
        self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertIn(self.car.target_offset, (-68, 23))

    def test_fast_rear_car_blocks_merge(self):
        self.car.loc.frac = 0.5
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.car.loc.speed = 300
        self.add_traffic(0.95, -23, 300)
        self.add_traffic(0.05, -68, 1000)
        self.add_traffic(0.5, 23, 300)
        self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertEqual(self.car.target_offset, -23)

    def test_pending_lane_change_reserves_space(self):
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.car.loc.speed = 300
        self.add_traffic(0.8, -23, 250)
        other = self.add_traffic(0.1, 68, 300)
        other.target_offset = 23
        self.add_traffic(0.1, -68, 300)
        self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertEqual(self.car.target_offset, -23)

    def test_traffic_gap_wraps_finish_and_ignores_clear_lane(self):
        other = self.add_traffic(0.1, 68, 0)
        self.car.loc.index, self.car.loc.frac = 2, 0.9
        self.assertGreater(self.game._traffic_distance(self.car, other), 0)
        self.assertLess(self.game._traffic_distance(self.car, other), 120)
        self.assertEqual(self.game.ai_race_traffic(self.car, 800, time.monotonic()), 800)

    def test_shorter_line_uses_inside_of_right_and_left_turns(self):
        for turn, expected in (('R', 23), ('L', -68)):
            self.track.pieces[2].turn = turn
            self.car.loc.lane_mm = self.car.target_offset = -23
            self.car.ai_next_lane_t = 0
            self.game.ai_race_traffic(self.car, 800, time.monotonic())
            self.assertEqual(self.car.target_offset, expected)

    def test_shorter_line_waits_for_adjacent_traffic(self):
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.add_traffic(0.1, 23, 300)
        self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertEqual(self.car.target_offset, -23)

    def test_shorter_line_holds_lane_for_balanced_chicane(self):
        self.track.pieces[1].kind, self.track.pieces[1].turn = TURN, 'L'
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.game.ai_race_traffic(self.car, 800, time.monotonic())
        self.assertEqual(self.car.target_offset, -23)

    def test_extreme_keeps_full_speed_through_default_inner_curve(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.car.loc.index = 2
        self.car.loc.lane_mm = self.car.target_offset = 68
        self.assertEqual(self.game.ai_target_speed(self.car), 800)

    def test_extreme_brakes_for_tighter_radius_at_higher_limit(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.car.max_speed = 1200
        self.car.loc.index = 2
        self.car.loc.lane_mm = self.car.target_offset = 68
        inside = self.game.ai_target_speed(self.car)
        self.car.loc.lane_mm = self.car.target_offset = -68
        outside = self.game.ai_target_speed(self.car)
        self.assertLess(inside, outside)
        self.assertLess(outside, 1200)
        self.car.loc.index, self.car.loc.frac = 1, 0.99
        self.car.target_offset = 68
        self.assertAlmostEqual(self.game.ai_target_speed(self.car), inside)

    def test_extreme_still_slows_for_unknown_track_and_traffic(self):
        self.game.ai_speed = None
        self.car.ai_difficulty = 'extreme'
        self.track.pieces[0].kind = UNKNOWN
        self.assertLessEqual(self.game.ai_target_speed(self.car), 300)
        self.track.pieces[0].kind = STRAIGHT
        self.car.loc.lane_mm = self.car.target_offset = -23
        self.add_traffic(0.2, -23, 0)
        self.assertEqual(self.game.ai_race_traffic(self.car, 800, time.monotonic()), 0)

    def test_extreme_cli_selection(self):
        from app import parse_args
        self.assertEqual(parse_args(['--ai-difficulty', 'extreme']).ai_difficulty, 'extreme')

    def test_curve_does_not_change_lanes(self):
        self.car.loc.index = 2
        self.car.command_speed = Mock()
        self.car.set_lane = Mock()
        self.game.mode = 'race'
        self.car.loc.lane_mm = self.car.target_offset = 68
        self.game._ai(self.car, 0.02, time.monotonic())
        self.car.set_lane.assert_not_called()
        self.car.command_speed.assert_called_once()


if __name__ == '__main__':
    unittest.main()
