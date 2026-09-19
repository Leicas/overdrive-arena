"""A scalar Kalman position estimate constrained to the scanned circuit.

State is unwrapped centerline millimetres. Measured wheel speed is the control
input; lane radius converts it to centerline speed. BLE fixes correct prediction.
Prediction expires instead of inventing laps during a radio outage.
"""
import math


class PositionFilter:
    HORIZON = 1.0

    def __init__(self, track):
        self.track = track
        self.offsets = [0.0]
        for piece in track.pieces:
            self.offsets.append(self.offsets[-1] + piece.length_mm)
        self.length = self.offsets[-1]
        self.position = None
        self.variance = 10000.0
        self.time = 0.0
        self.last_fix = 0.0
        self.speed = 0.0
        self.lane = 0.0

    def distance(self, index, frac):
        return self.offsets[index] + frac * self.track.pieces[index].length_mm

    def pose(self, distance):
        distance %= self.length
        for index, piece in enumerate(self.track.pieces):
            if distance < self.offsets[index + 1]:
                return index, (distance - self.offsets[index]) / piece.length_mm
        return 0, 0.0

    def predict(self, now):
        if self.position is None:
            return None
        remaining = max(0.0, min(now, self.last_fix + self.HORIZON) - self.time)
        position = self.position
        # Small integration steps handle changing lane radius across piece edges.
        while remaining > 1e-9:
            dt = min(remaining, 0.02)
            index, _ = self.pose(position)
            piece = self.track.pieces[index]
            position += self.speed * dt * piece.length_mm / piece.length_at(self.lane)
            remaining -= dt
        return position

    def observe(self, index, frac, speed, lane, now, reset=False, noise=45.0):
        measured = self.distance(index, frac)
        predicted = self.predict(now)
        # A confirmed/commanded stop may outlast the countdown without breaking
        # continuity. Moving cars still lose confidence after the short horizon.
        discontinuity = reset or predicted is None or (abs(self.speed) > 0 and now - self.last_fix > self.HORIZON)
        if predicted is not None:
            measured += round((predicted - measured) / self.length) * self.length
            variance = self.variance + (80.0 + abs(self.speed) * 0.25) ** 2 * max(0, now - self.time)
            residual = measured - predicted
            # A large relocation cannot establish continuous race travel.
            discontinuity |= abs(residual) > max(300.0, 4 * math.sqrt(variance + noise ** 2))
        if discontinuity:
            self.position, self.variance = measured, noise ** 2
        else:
            gain = variance / (variance + noise ** 2)
            self.position = predicted + gain * residual
            self.variance = (1 - gain) * variance
        self.time, self.speed, self.lane = now, speed, lane
        self.last_fix = now
        return discontinuity

    def set_speed(self, speed, now):
        if self.position is not None:
            self.position = self.predict(now)
            self.time = max(self.time, min(now, self.last_fix + self.HORIZON))
        self.speed = speed
