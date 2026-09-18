"""Regression coverage for honest lap counting and online corner-speed learning."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Piece, Track, START, STRAIGHT, TURN
from game.game import CarState, Game


class LearningLapTests(unittest.TestCase):
    def setUp(self):
        car = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.c = CarState(car, 0, Mock(), 800)
        self.track = Track([Piece(34, START), Piece(18, TURN, 'R'),
                            Piece(17, TURN, 'R'), Piece(39, STRAIGHT),
                            Piece(20, TURN, 'R'), Piece(23, TURN, 'R')])
        self.g = Game(Mock(), [self.c], self.track, True, None)
        self.g.mode = 'race'
        self.c.ai_difficulty = 'extreme'
        self.c.loc.on_track = True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.index = 0
        self.c.loc.speed = self.c._last_speed_sent = 800
        self.c.ai_has_driven = True
        self.now = 100.0

    def lap_position(self, p, direction=1):
        self.now += 0.5
        self.c.loc.index, self.c.loc.frac = int(p), p % 1
        self.c.loc.direction = direction
        self.g._track_laps(self.c, self.now)

    def full_lap(self):
        for p in (0.9, 1.4, 1.9, 2.4, 2.9, 3.4, 3.9, 4.4, 4.9, 5.4, 5.9, 0.2, 0.6):
            self.lap_position(p)

    def test_clean_full_lap_counts_once(self):
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.assertEqual(self.c.laps, 0)
        self.full_lap()
        self.assertEqual(self.c.laps, 1)
        self.assertIsNotNone(self.c.best_lap_s)

    def test_reverse_invalidates_lap_and_recross_does_not_score(self):
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.full_lap()
        best = self.c.best_lap_s
        self.lap_position(1.2)
        self.lap_position(0.8, -1)
        self.lap_position(0.3, -1)
        self.lap_position(0.35)
        self.lap_position(0.6)
        self.assertEqual(self.c.laps, 1)
        self.assertEqual(self.c.best_lap_s, best)
        self.assertIsNone(self.g.winner)
        self.full_lap()
        self.assertEqual(self.c.laps, 2)

    def test_reverse_report_between_frames_invalidates_lap(self):
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.c._reverse_reports += 1
        self.lap_position(0.4)
        self.lap_position(0.6)
        self.assertEqual(self.c.laps, 0)
        self.assertIsNone(self.c.best_lap_s)

    def test_finish_line_oscillation_and_teleport_do_not_count(self):
        for p in (0.3, 0.6, 0.4, 0.6, 0.4, 0.6, 3.5, 5.9, 0.3, 0.6):
            self.lap_position(p)
        self.assertEqual(self.c.laps, 0)
        self.assertIsNone(self.c.best_lap_s)

    def test_lost_localization_invalidates_partial_lap(self):
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.c.loc.on_track = False
        self.g._track_laps(self.c, self.now)
        self.c.loc.on_track = True
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.assertEqual(self.c.laps, 0)

    def spin(self, now):
        self.c.loc.direction = -1
        self.c.ai_last_corner_speed = 800
        self.c.ai_last_corner_t = now - 0.2
        self.g._learn_corner_recovery(self.c, now)

    def test_one_reduction_per_incident_and_again_after_recovery(self):
        self.spin(100)
        self.assertEqual(self.c.ai_corner_limit, 680)
        self.spin(105)
        self.assertEqual(self.c.ai_corner_limit, 680)
        self.c.loc.direction = 1
        self.g._learn_corner_recovery(self.c, 106)
        self.g._learn_corner_recovery(self.c, 109)
        self.spin(110)
        self.assertEqual(self.c.ai_corner_limit, 578)

    def test_initial_wrong_direction_is_not_a_speed_failure(self):
        self.c.ai_has_driven = False
        self.spin(100)
        self.assertIsNone(self.c.ai_corner_limit)

    def clean_corners(self, speed=680):
        self.c.loc.speed = self.c._last_speed_sent = speed
        for index in (0, 1, 2, 3, 4, 5):
            for frac in (0.1, 0.8):
                self.c.loc.index, self.c.loc.frac = index, frac
                self.g._learn_clean_corner(self.c, self.now)

    def test_three_clean_turns_probe_up_two_percent(self):
        self.c.ai_corner_limit = 680
        self.clean_corners()
        self.assertAlmostEqual(self.c.ai_corner_limit, 693.6)

    def test_slow_traffic_does_not_prove_higher_speed_is_safe(self):
        self.c.ai_corner_limit = 680
        self.clean_corners(speed=300)
        self.assertEqual(self.c.ai_corner_limit, 680)

    def test_probe_never_exceeds_difficulty_limit(self):
        self.c.ai_corner_limit = 795
        self.clean_corners(speed=800)
        self.assertEqual(self.c.ai_corner_limit, 800)

    def test_learning_affects_curves_and_braking_but_not_clear_straights(self):
        self.c.ai_corner_limit = 600
        self.c.loc.index, self.c.loc.frac = 1, 0.5
        self.assertEqual(self.g.ai_target_speed(self.c), 600)
        self.c.loc.index, self.c.loc.frac = 0, 0.98
        self.assertEqual(self.g.ai_target_speed(self.c), 600)
        self.c.loc.frac = 0.03
        self.assertEqual(self.g.ai_target_speed(self.c), 800)

    def test_grid_finish_bar_starts_first_lap_at_go(self):
        self.c.grid_state = 'placed'
        self.c.loc.frac = 0.5
        self.c.lights_idle = Mock()
        self.g.start()
        self.now = self.c.lap_start_t
        self.full_lap()
        self.assertEqual(self.c.laps, 1)

    def test_reverse_cannot_award_winning_lap(self):
        self.g.lap_target = 1
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.lap_position(1.2)
        self.lap_position(0.3, -1)
        self.lap_position(0.35)
        self.lap_position(0.6)
        self.assertIsNone(self.g.winner)
        self.full_lap()
        self.assertIs(self.g.winner, self.c)

    def test_missing_codes_get_bounded_search_then_stop(self):
        now = time.monotonic()
        self.c.loc.last_update = now - 2
        self.c.command_speed = Mock()
        self.assertTrue(self.g._recover_ai_track(self.c, now))
        self.c.command_speed.assert_called_with(250, now)
        self.g._recover_ai_track(self.c, now + 1.6)
        self.assertEqual(self.c.ai_track_recovery, 'waiting')
        self.c.command_speed.assert_called_with(0, now + 1.6)
        self.g._recover_ai_track(self.c, now + 20)
        self.c.command_speed.assert_called_with(0, now + 20)

    def test_reported_off_track_stops_immediately_and_learns_once(self):
        now = time.monotonic()
        self.c.loc.on_track = False
        self.c.ai_last_corner_t, self.c.ai_last_corner_speed = now - 0.2, 800
        self.c.command_speed = Mock()
        self.g._recover_ai_track(self.c, now)
        self.assertEqual(self.c.ai_track_recovery, 'waiting')
        self.c.command_speed.assert_called_with(0, now)
        self.assertEqual(self.c.ai_corner_limit, 680)
        self.g._recover_ai_track(self.c, now + 1)
        self.assertEqual(self.c.ai_corner_limit, 680)

    def test_explicit_retry_moves_then_fresh_codes_resume(self):
        now = time.monotonic()
        self.c.loc.on_track = False
        self.c.command_speed = Mock()
        self.g._recover_ai_track(self.c, now)
        self.g.running = self.c.ai = True
        self.g.retry_ai_recovery()
        start = self.c.ai_track_recovery_t
        self.g._recover_ai_track(self.c, start + 0.1)
        self.c.command_speed.assert_called_with(250, start + 0.1)
        self.c.loc.on_track = True
        self.c.loc.last_update = start + 0.2
        self.assertFalse(self.g._recover_ai_track(self.c, start + 0.3))
        self.assertEqual(self.c.ai_track_recovery, '')

    def test_old_codes_cannot_resume_waiting_car(self):
        now = time.monotonic()
        self.c.ai_track_recovery, self.c.ai_track_recovery_t = 'waiting', now
        self.c.loc.last_update = now - 0.1
        self.c.command_speed = Mock()
        self.assertTrue(self.g._recover_ai_track(self.c, now))
        self.c.command_speed.assert_called_with(0, now)

    def test_charging_and_disconnected_cars_do_not_search(self):
        now = time.monotonic()
        self.c.command_speed = Mock()
        self.c.car.on_charger = True
        self.assertTrue(self.g._recover_ai_track(self.c, now))
        self.c.command_speed.assert_called_with(0, now)
        self.g.running = self.c.ai = True
        self.g.retry_ai_recovery()
        self.assertEqual(self.c.ai_track_recovery, 'waiting')
        self.c.car.on_charger = False
        self.c.car.connected = False
        self.c.command_speed.reset_mock()
        self.g._recover_ai_track(self.c, now + 1)
        self.c.command_speed.assert_not_called()
        self.assertEqual(self.c.speed_sent(), 0)

    def test_delocalized_report_interrupts_automatic_search(self):
        now = time.monotonic()
        self.c.loc.last_update = now - 2
        self.c.command_speed = Mock()
        self.g._recover_ai_track(self.c, now)
        self.c.loc.on_track = False
        self.g._recover_ai_track(self.c, now + 0.1)
        self.c.command_speed.assert_called_with(0, now + 0.1)

    def test_stopped_car_does_not_crawl_into_traffic_when_codes_expire(self):
        now = time.monotonic()
        self.c.loc.last_update = now - 2
        self.c._last_speed_sent = 0
        self.c.command_speed = Mock()
        self.g._recover_ai_track(self.c, now)
        self.c.command_speed.assert_called_with(0, now)
        self.assertEqual(self.c.ai_track_recovery, 'waiting')

    def test_recovery_invalidates_lap(self):
        self.lap_position(0.3)
        self.lap_position(0.6)
        self.c.ai_track_recovery = 'searching'
        self.lap_position(1.0)
        self.assertIsNone(self.c.lap_start_t)
        self.assertEqual(self.c.laps, 0)

    def test_offtrack_learning_after_search_has_already_started(self):
        now = time.monotonic()
        self.c.command_speed = Mock()
        self.c.loc.last_update = now - 2
        self.c.ai_last_corner_t, self.c.ai_last_corner_speed = now - 2, 800
        self.g._recover_ai_track(self.c, now)
        self.assertIsNone(self.c.ai_corner_limit)
        self.c.loc.on_track = False
        self.g._recover_ai_track(self.c, now + 0.8)
        self.assertEqual(self.c.ai_corner_limit, 680)
        self.g._recover_ai_track(self.c, now + 1)
        self.assertEqual(self.c.ai_corner_limit, 680)

    def test_transient_offtrack_report_still_learns_and_invalidates_lap(self):
        from anki import protocol as P
        now = time.monotonic()
        self.c.ai_last_corner_t, self.c.ai_last_corner_speed = now - 0.2, 800
        self.c.on_message(P.MSG_VEHICLE_DELOCALIZED, 'delocalized', received_at=now)
        self.c.loc.on_track = True
        self.c.loc.last_update = now + 0.1
        self.assertFalse(self.g._recover_ai_track(self.c, now + 0.2))
        self.assertEqual(self.c.ai_corner_limit, 680)
        self.c.lap_start_t = now - 10
        self.g._track_laps(self.c, now + 0.2)
        self.assertIsNone(self.c.lap_start_t)

    def test_learning_survives_next_race_but_resets_with_track(self):
        self.c.ai_corner_limit = 600
        self.c.lights_idle = Mock()
        self.g.start()
        self.assertEqual(self.c.ai_corner_limit, 600)
        self.g.set_track(self.track)
        self.assertIsNone(self.c.ai_corner_limit)


if __name__ == '__main__':
    unittest.main()
