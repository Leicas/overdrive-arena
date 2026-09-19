"""Controller flow, persistent records and sensor/prediction race regressions."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from anki import protocol as P
from anki.position_filter import PositionFilter
from anki.track import CarLocalizer, Piece, Track, START, STRAIGHT, TURN, FNF, locate
from game.game import CarState, Game
from game.records import Records
from game import inputs as I
from app import App, PAIRING, RACE, GRID


def circuit():
    return Track([Piece(34, START), Piece(36, STRAIGHT), Piece(17, TURN, 'R'),
                  Piece(18, TURN, 'R'), Piece(39, STRAIGHT), Piece(20, TURN, 'R'),
                  Piece(23, TURN, 'R')])


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.track = circuit()
        vehicle = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.car = CarState(vehicle, 0, Mock(), 2000)
        self.game = Game(Mock(), [self.car], self.track, True, None)
        self.game.mode = 'race'
        self.now = time.monotonic()

    def fix(self, index, frac, now, speed=2000):
        piece = self.track.pieces[index]
        pid = 33 if piece.kind == START and frac >= .5 else piece.id
        printed = (frac % .5) * 2 if piece.kind == START else frac
        pos = P.PositionUpdate(0, pid, 0, speed, 0)
        with patch('anki.track.locate', return_value=(0, printed)):
            self.car.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, pos, received_at=now)

    def test_prediction_crosses_piece_edge_but_delayed_transition_does_not_double_advance(self):
        self.fix(1, .98, self.now)
        self.car.loc.advance(.05)
        self.assertEqual(self.car.loc.index, 2)
        transition = P.TransitionUpdate(*([0] * 11))
        self.car.on_message(P.MSG_LOCALIZATION_TRANSITION_UPDATE, transition, received_at=self.now + .05)
        self.assertEqual(self.car.loc.index, 2)

    def test_prediction_stops_at_horizon_and_delocalization(self):
        self.fix(1, .2, self.now)
        self.car.loc.advance(1)
        position = self.car.loc.progress()
        self.car.loc.advance(10)
        self.assertAlmostEqual(self.car.loc.progress(), position)
        self.car.on_message(P.MSG_VEHICLE_DELOCALIZED, None, received_at=self.now + 11)
        position = self.car.loc.progress()
        self.car.loc.advance(.2)
        self.assertAlmostEqual(self.car.loc.progress(), position)

    def test_fast_laps_without_any_finish_piece_packets(self):
        route = PositionFilter(self.track)
        origin = route.distance(1, .1)
        self.game.race_start_t = self.now
        # Two full circuits in one frame's queue, all finish-piece reports lost.
        for distance in range(0, int(route.length * 2.5), 100):
            index, frac = route.pose(origin + distance)
            if index != 0:
                self.fix(index, frac, self.now + distance / 2000)
        self.game._track_laps(self.car, self.now + route.length * 2.5 / 2000)
        self.assertGreaterEqual(self.car.laps, 1)
        self.assertAlmostEqual(self.car.best_lap_s, route.length / 2000, delta=.06)

    def test_prediction_alone_cannot_award_lap(self):
        self.fix(0, .4, self.now)
        self.car.lap_start_t = None
        self.car._lap_travel = 0
        self.car.loc.motion_samples.clear()
        self.car.loc.advance(.2)
        self.game._track_laps(self.car, self.now + .2)
        self.assertEqual(self.car.laps, 0)

    def test_prediction_finishes_observed_circuit_without_finish_packet(self):
        self.fix(0, .4, self.now, speed=800)
        self.car.loc.motion_samples.clear()
        self.car._prev_progress, self.car._prev_progress_t = .4, self.now
        self.car.lap_start_t = self.now - 5
        self.car._lap_travel = len(self.track) - .1
        self.game._track_laps(self.car, self.now + .2)
        self.assertEqual(self.car.laps, 1)
        self.assertAlmostEqual(self.car.last_lap_s, 5.07, places=2)
        # A late notification for the already predicted interval cannot rewind
        # the lap clock or score again, nor can silent prediction accrue laps.
        self.fix(0, .6, self.now + .1, speed=800)
        self.game._track_laps(self.car, self.now + .3)
        self.game._track_laps(self.car, self.now + 2)
        self.assertEqual(self.car.laps, 1)

    def test_prediction_cannot_complete_partial_circuit_or_repeat_during_silence(self):
        self.fix(0, .4, self.now, speed=800)
        self.car.loc.motion_samples.clear()
        self.car._prev_progress, self.car._prev_progress_t = .4, self.now
        self.car.lap_start_t = self.now - 5
        self.car._lap_travel = 1
        self.game._track_laps(self.car, self.now + 2)
        self.assertEqual(self.car.laps, 0)
        self.game._track_laps(self.car, self.now + 20)
        self.assertEqual(self.car.laps, 0)

    def test_late_forward_fix_cannot_rewind_display(self):
        self.fix(1, .2, self.now, speed=800)
        self.car.loc.advance(0, self.now + .2)
        before = self.car.loc.progress()
        # Arrived at t+.1, processed after the t+.2 frame was displayed.
        self.fix(1, .25, self.now + .1, speed=800)
        self.car.loc.advance(0, self.now + .21)
        self.assertGreaterEqual(self.car.loc.progress(), before)
        self.assertEqual(self.car.loc.last_update, self.now + .1)
        self.car.loc.advance(0, self.now + .6)
        self.assertGreater(self.car.loc.progress(), before)

    def test_out_of_order_motion_does_not_rewind_or_invalidate_lap(self):
        self.fix(1, .7, self.now, speed=800)
        pose = self.car.loc.progress()
        self.fix(1, .2, self.now - .2, speed=200)
        self.car.on_message(P.MSG_SPEED_UPDATE, P.SpeedUpdate(0, 0, 0), received_at=self.now - .1)
        self.car.on_message(P.MSG_VEHICLE_DELOCALIZED, None, received_at=self.now - .1)
        self.assertEqual(self.car.loc.progress(), pose)
        self.assertEqual(self.car.loc.speed, 800)
        self.assertEqual(self.car._offtrack_reports, 0)

    def test_notification_timestamp_survives_parse_and_queue_delay(self):
        from anki.vehicle import Vehicle
        vehicle = Vehicle(SimpleNamespace(address='test'))
        vehicle.on_timed_message = self.car.enqueue_message
        with patch('anki.vehicle.time.monotonic', return_value=100):
            vehicle._notify(None, bytes.fromhex('10 27 0b 12 00 00 00 00 8d 01 47 00 00 2c 01 90 01'))
        with patch('game.game.time.monotonic', return_value=100.3):
            self.car.drain_messages()
        self.assertEqual(self.car.loc.last_code_t, 100)
        self.assertAlmostEqual(self.car.message_delay_s, .3)

    def test_rank_is_relative_to_finish_line(self):
        second = CarState(SimpleNamespace(model='Second', battery_mv=0, connected=True, on_charger=False), 1, Mock(), 800)
        second.set_track(self.track)
        self.game.cars.append(second)
        for car, frac in ((self.car, .3), (second, .7)):
            car.loc.index, car.loc.frac, car.loc.on_track = 0, frac, True
        self.assertIs(self.game.ranking()[0], self.car)

    def test_code_gap_does_not_erase_continuous_route_progress(self):
        self.fix(0, .3, self.now)
        self.game._track_laps(self.car, self.now)
        self.fix(0, .6, self.now + .1)
        self.game._track_laps(self.car, self.now + .1)
        self.assertIsNotNone(self.car.lap_start_t)
        started = self.car.lap_start_t
        self.fix(1, .2, self.now + 4)
        self.game._track_laps(self.car, self.now + 4)
        self.assertEqual(self.car.lap_start_t, started)

    def test_every_displayed_circuit_counts_with_missing_finish_codes_and_speed_error(self):
        import math
        route = self.car.loc.filter
        origin = route.distance(0, .5)
        self.fix(0, .5, self.now, speed=0)
        self.car.grid_state = 'placed'
        self.car.lights_idle = Mock()
        with patch('game.game.time.monotonic', return_value=self.now):
            self.game.start()
        self.game.lap_target = 99
        for frame in range(1, 1001):
            now = self.now + frame * .02
            index, frac = route.pose(origin + 900 * frame * .02)
            if frame % 8 == 0 and index != 0 and frame % 56 != 0:
                self.fix(index, frac, now, speed=1200 if frame % 32 == 0 else 900)
            self.car.loc.advance(.02, now)
            self.game._track_laps(self.car, now)
            displayed_laps = max(0, math.floor((self.car.loc._display_distance - origin) / route.length))
            self.assertEqual(self.car.laps, displayed_laps, f'frame {frame}: visible circuit was not counted')
        self.assertGreaterEqual(self.car.laps, 4)

    def test_real_codes_on_figure_eight_count_despite_filter_resets(self):
        # Same topology as the reported failing map: repeated IDs, opposite
        # print orientations, and a crossing piece. No mocked location decoder.
        for reported_speed in (400, 1400):
            with self.subTest(reported_speed=reported_speed):
                self.track = Track([Piece(34, START), Piece(20, TURN, 'L'),
                                    Piece(17, TURN, 'L'), Piece(18, TURN, 'L'),
                                    Piece(57, FNF), Piece(17, TURN, 'R', True),
                                    Piece(18, TURN, 'R', True), Piece(23, TURN, 'R', True)])
                self.game.set_track(self.track)
                self.car.laps = 0
                self.car._prev_progress = self.car.lap_start_t = None
                self.car._lap_travel = 0
                now = self.now + reported_speed
                resets = []
                observe = self.car.loc.filter.observe
                def observed(*args, **kwargs):
                    reset = observe(*args, **kwargs)
                    resets.append(reset)
                    return reset
                with patch.object(self.car.loc.filter, 'observe', side_effect=observed):
                    for _ in range(4):
                        for index, piece in enumerate(self.track.pieces):
                            # Just one real barcode per piece, spaced >1s apart.
                            choices = []
                            for code in range(256):
                                decoded = locate(piece.id, code, piece.reversed)
                                if decoded is not None and abs(abs(decoded[0]) - 22) < 5:
                                    choices.append((abs(decoded[1] - .5), code))
                            code = min(choices)[1]
                            now += piece.length_mm / 400
                            self.car.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE,
                                                P.PositionUpdate(code, piece.id, 0, reported_speed,
                                                                 0x40 if piece.reversed else 0), received_at=now)
                            self.game._track_laps(self.car, now)
                    self.assertGreater(sum(resets), 4)
                    self.assertEqual(self.car.laps, 3)

    def test_reused_piece_id_after_missing_codes_does_not_report_false_uturn(self):
        track = Track([Piece(34, START), Piece(20, TURN, 'L'), Piece(17, TURN, 'L'),
                       Piece(18, TURN, 'L'), Piece(57, FNF), Piece(17, TURN, 'R', True),
                       Piece(18, TURN, 'R', True), Piece(23, TURN, 'R', True)])
        loc = CarLocalizer(track)
        # Skip the intervening curve/crossing codes between the two copies of 17.
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, P.PositionUpdate(0, 17, 0, 1400, 0), received_at=100)
        self.assertEqual(loc.index, 2)
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, P.PositionUpdate(11, 17, 0, 1400, 0x40), received_at=101)
        self.assertEqual(loc.index, 5)
        self.assertEqual(loc.direction, 1)

    def test_same_barcode_on_adjacent_identical_pieces_uses_predicted_distance(self):
        track = Track([Piece(34, START), Piece(17, TURN, 'R', True),
                       Piece(17, TURN, 'R', True), Piece(39, STRAIGHT), Piece(20, TURN, 'R')])
        loc = CarLocalizer(track)
        position = P.PositionUpdate(11, 17, 0, 800, 0x40)
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, position, received_at=100)
        self.assertEqual(loc.index, 1)
        elapsed = track.pieces[1].length_at(loc.lane_mm) / 800
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, position, received_at=100 + elapsed)
        self.assertEqual(loc.index, 2)

    def test_queued_crossings_and_rendered_position_share_route_after_frame_stall(self):
        route = self.car.loc.filter
        origin = route.distance(0, .5)
        self.fix(0, .5, self.now, speed=800)
        self.car.grid_state = 'placed'
        self.car.lights_idle = Mock()
        with patch('game.game.time.monotonic', return_value=self.now):
            self.game.start()
        for step in range(1, 81):
            index, frac = route.pose(origin + step * 100)
            self.fix(index, frac, self.now + step * .125, speed=800)
        self.game._track_laps(self.car, self.now + 10)
        self.assertEqual(self.car.laps, 2)
        self.assertAlmostEqual(self.car.best_lap_s, route.length / 800, delta=.15)

    def test_radio_silence_holds_partial_lap_instead_of_erasing_it(self):
        self.fix(0, .3, self.now, speed=400)
        self.game._track_laps(self.car, self.now)
        self.fix(0, .7, self.now + .6, speed=400)
        self.game._track_laps(self.car, self.now + .6)
        started = self.car.lap_start_t
        self.assertIsNotNone(started)
        self.car._last_speed_sent = 400
        with patch('game.game.time.monotonic', return_value=self.now + 30):
            self.game._track_laps(self.car, self.now + 30)
        self.assertEqual(self.car.lap_start_t, started)
        self.assertEqual(self.car.laps, 0)
        self.assertEqual(self.car.lap_note, 'position stale; holding estimate')

    def test_stop_freezes_prediction_without_refreshing_sensor_age(self):
        self.fix(1, .2, self.now)
        self.car.car.set_speed = Mock()
        self.car.hard_stop(self.now + .1)
        self.car.loc.advance(0, self.now + .2)
        stopped = self.car.loc.progress()
        self.car.loc.advance(0, self.now + .8)
        self.assertEqual(self.car.loc.progress(), stopped)
        self.assertEqual(self.car.loc.last_update, self.now)

    def test_stopped_pose_stays_visible_after_telemetry_expires(self):
        self.fix(1, .2, self.now, speed=800)
        self.car.car.set_speed = Mock()
        self.car.hard_stop(self.now + .1)
        self.car.loc.advance(0, self.now + .2)
        stopped = self.car.world()
        with patch('game.game.time.monotonic', return_value=self.now + 60):
            self.car.loc.advance(0, self.now + 60)
            self.assertEqual(self.car.world(), stopped)
            self.assertFalse(self.car.localized())
            self.assertTrue(self.car.map_position_known())
            self.car.car.on_charger = True
            self.assertFalse(self.car.map_position_known())
            self.car.car.on_charger = False
            self.car.loc.on_track = False
            self.assertFalse(self.car.map_position_known())

    def test_unknown_or_stale_moving_car_has_no_retained_stopped_pose(self):
        self.assertFalse(self.car.map_position_known())
        self.fix(1, .2, self.now, speed=800)
        self.car._last_speed_sent = 800
        with patch('game.game.time.monotonic', return_value=self.now + 60):
            self.assertFalse(self.car.map_position_known())

    def test_missed_start_bar_does_not_delay_exit_transition(self):
        self.fix(0, .4, self.now)
        # Both the middle bar and the second half's codes are missing.
        self.car.on_message(P.MSG_LOCALIZATION_TRANSITION_UPDATE, P.TransitionUpdate(*([0] * 11)),
                            received_at=self.now + 560 * .6 / 2000)
        self.assertEqual(self.car.loc.index, 1)
        self.assertAlmostEqual(self.car.loc.frac, 0)

    def test_duplicate_transition_does_not_skip_a_piece(self):
        self.fix(1, .98, self.now)
        for dt in (.01, .015):
            self.car.on_message(P.MSG_LOCALIZATION_TRANSITION_UPDATE, P.TransitionUpdate(*([0] * 11)),
                                received_at=self.now + dt)
        self.assertEqual(self.car.loc.index, 2)

    def test_measurement_corrects_prediction_and_gates_teleport(self):
        filt = PositionFilter(self.track)
        filt.observe(1, .2, 800, 0, self.now)
        predicted = filt.predict(self.now + .1)
        measured = predicted + 30
        index, frac = filt.pose(measured)
        self.assertFalse(filt.observe(index, frac, 800, 0, self.now + .1))
        self.assertLess(predicted, filt.position)
        self.assertLess(filt.position, measured)
        self.assertTrue(filt.observe(4, .5, 800, 0, self.now + .11))

    def test_countdown_stop_preserves_first_timed_lap(self):
        self.fix(0, .5, self.now, speed=0)
        self.car.loc.filter.set_speed(0, self.now)
        self.car.loc.motion_samples.clear()
        self.car.lap_start_t = self.now + 3
        self.car._prev_progress, self.car._prev_progress_t = .5, self.now + 3
        self.game.race_start_t = self.now + 3
        self.fix(0, .6, self.now + 3.1, speed=300)
        self.game._track_laps(self.car, self.now + 3.1)
        self.assertEqual(self.car.lap_start_t, self.now + 3)

    def test_duplicate_crossing_does_not_erase_or_double_count_lap(self):
        t = self.now
        for p in (.3, .6, .4, .6, 1.2, 2.2, 3.2, 4.2, 5.2, 6.2, .2, .6, .4, .6):
            self.game._track_lap_sample(self.car, p, t)
            t += .3
        self.assertEqual(self.car.laps, 1)

    def test_player_recovery_is_bounded_and_cancelled_by_brake(self):
        source = Mock(spec=I.InputSource)
        source.pressed.return_value = False
        source.brake.return_value = 0
        self.car.source = source
        self.game.running = True
        self.car.command_speed = Mock()
        self.car.hard_stop = Mock()
        self.game.retry_recovery(source)
        start = self.car.ai_track_recovery_t
        self.game._human(self.car, .02, start + .1)
        self.car.command_speed.assert_called_with(250, start + .1)
        self.game._human(self.car, .02, start + 1.6)
        self.car.command_speed.assert_called_with(0, start + 1.6)
        self.game.retry_recovery(source)
        source.brake.return_value = 1
        self.game._human(self.car, .02, start + 1.7)
        self.car.hard_stop.assert_called_once()
        self.assertEqual(self.car.ai_track_recovery, 'waiting')

    def test_finished_race_is_recorded_once_and_ranked_by_finish_time(self):
        self.game.race_start_t = self.now - 5
        self.game.lap_target = 1
        self.car.lap_start_t = self.now - 5
        self.car._prev_progress, self.car._prev_progress_t = .3, self.now - .2
        self.car._lap_travel = len(self.track) - .2
        self.game._track_lap_sample(self.car, .7, self.now)
        record = self.game.records.get(self.track, 'race', 'ai', 'race', 1)
        self.assertAlmostEqual(record['seconds'], 4.9)
        self.game._track_lap_sample(self.car, .3, self.now + .1)
        self.game._track_lap_sample(self.car, .7, self.now + .2)
        self.assertEqual(self.car.laps, 1)


class RecordTests(unittest.TestCase):
    def test_personal_records_saved_even_when_overall_record_is_faster(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.json'
            records = Records(path)
            track = circuit()
            pad1 = SimpleNamespace(name='Xbox', short='P1', record_id='xbox:1')
            pad2 = SimpleNamespace(name='Xbox', short='P2', record_id='xbox:2')
            car = SimpleNamespace(name='Dynamo', car=SimpleNamespace(address='A'), source=pad1, driver_label=lambda: 'player')
            records.record(track, 'race', car, 'lap', 5)
            car.source = pad2
            records.record(track, 'race', car, 'lap', 8)
            records.record(track, 'race', car, 'race', 45, 5)
            car.car.address, car.name = 'B', 'X52'
            records.record(track, 'race', car, 'lap', 9)
            loaded = Records(path)
            cars = loaded.leaderboard(track, 'race', 'car', 5)
            pads = loaded.leaderboard(track, 'race', 'controller', 5)
            self.assertEqual([r['lap']['seconds'] for r in cars], [5, 9])
            self.assertEqual([r['lap']['seconds'] for r in pads], [5, 8])
            self.assertEqual(pads[1]['race']['seconds'], 45)
            self.assertIsNone(loaded.leaderboard(track, 'race', 'controller', 10)[1]['race'])
            self.assertEqual(len(loaded.leaderboard(track, 'race', 'pair', 5)), 3)

    def test_legacy_overall_records_survive_new_personal_records(self):
        records = Records()
        track = circuit()
        key = records.key(track, 'race', 'human', 'lap')
        records.entries[key] = {'car': 'Old champion', 'driver': 'P1', 'seconds': 4.5}
        car = SimpleNamespace(name='Test', source=SimpleNamespace(name='Pad', short='P2'), driver_label=lambda: 'P2')
        records.record(track, 'race', car, 'lap', 8)
        self.assertEqual(records.get(track, 'race', 'human', 'lap')['seconds'], 4.5)
        self.assertEqual(len(records.leaderboard(track, 'race', 'controller', 5)), 1)

    def test_roundtrip_and_separate_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.json'
            records = Records(path)
            car = SimpleNamespace(name='Test', source=object(), driver_label=lambda: 'P1')
            track = circuit()
            records.record(track, 'race', car, 'lap', 7.2)
            records.record(track, 'race', car, 'lap', 9.0)
            records.record(track, 'race', car, 'race', 40.0, 5)
            loaded = Records(path)
            self.assertEqual(loaded.get(circuit(), 'race', 'human', 'lap')['seconds'], 7.2)
            self.assertIsNone(loaded.get(track, 'race', 'ai', 'lap'))
            self.assertIsNone(loaded.get(track, 'battle', 'human', 'lap'))
            self.assertIsNone(loaded.get(track, 'race', 'human', 'race', 3))
            track.pieces[2].turn = 'L'
            self.assertIsNone(loaded.get(track, 'race', 'human', 'lap'))

    def test_corrupt_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.json'
            path.write_text('broken')
            records = Records(path)
            car = SimpleNamespace(name='Test', source=None, driver_label=lambda: 'AI')
            records.record(circuit(), 'race', car, 'lap', 5)
            self.assertTrue(records.error)
            self.assertEqual(path.read_text(), 'broken')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.pad = Mock(spec=I.InputSource)
        self.pad.alive.return_value = True
        self.pad.short, self.pad.name = 'P1', 'Pad'
        self.app.pads = [self.pad]
        self.app.keyboard = Mock(spec=I.InputSource)
        self.app.keyboard.alive.return_value = False
        self.app.keyboard.pressed.return_value = False
        car = CarState(SimpleNamespace(model='Test', connected=True, on_charger=False, battery_mv=0), 0, Mock(), 800)
        self.app.cars = [car]
        self.app.game = Game(Mock(), [car], circuit(), True, None)
        self.app.game.begin_grid = Mock()
        self.app.cursors, self.app.claims, self.app.identified = {}, {}, {}
        self.app.click, self.app.state = None, PAIRING
        self.app.lap_choices = (3, 5, 10, 20)
        self.app.update_identify_lights = Mock()

    def press(self, action):
        self.pad.pressed.side_effect = lambda a: a == action

    def test_setup_and_start_using_only_pad(self):
        for action in (I.SELECT, I.TOGGLE_MODE, I.LIMIT_UP):
            self.press(action)
            self.app.pairing_tick()
        self.assertEqual(self.app.game.mode, 'race')
        self.assertEqual(self.app.game.lap_target, 10)
        self.press(I.READY)
        self.app.pairing_tick()
        self.assertEqual(self.app.state, GRID)
        self.assertIs(self.app.cars[0].source, self.pad)
        self.app.game.begin_grid.assert_called_once()

    def test_recovery_button_routes_request_to_player(self):
        self.app.game.retry_recovery = Mock()
        self.app.game.tick = Mock()
        self.press(I.RECOVER)
        self.app.race_tick(.02)
        self.app.game.retry_recovery.assert_called_once_with(self.pad)

    def test_countdown_rumbles_once_per_beat_and_stronger_at_go(self):
        game = self.app.game
        car = self.app.cars[0]
        car.source, car.grid_state = self.pad, 'placed'
        car.lights_flash = Mock()
        with patch('game.game.time.monotonic', return_value=100):
            game.begin_countdown()
        for now in (100.2, 101, 101.2, 102, 102.9, 103, 103.1):
            with patch('game.game.time.monotonic', return_value=now):
                game.countdown_feedback()
        self.assertEqual(self.pad.rumble.call_count, 4)
        self.assertEqual(self.pad.rumble.call_args_list[0].args, (.35, .35, 140))
        self.assertEqual(self.pad.rumble.call_args_list[-1].args, (.8, .9, 400))


if __name__ == '__main__':
    unittest.main()
