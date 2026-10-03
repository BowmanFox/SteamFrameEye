"""Stateful smoothing for direct VRChat OSC; standard library only.

Filter angles in radians, then convert to the existing FT/v2 +/-45 degree scale.
Raw SFEY packets are never processed here: VRCFaceTracking filters those on the PC.
"""
import math


class OneEuro:
    def __init__(self, min_cutoff, beta, derivative_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.derivative_cutoff = derivative_cutoff
        self.value = None
        self.velocity = 0.0

    @staticmethod
    def alpha(cutoff, dt):
        return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff * dt))

    def filter(self, value, dt):
        if self.value is None:
            self.value = value
            return value
        velocity = (value - self.value) / dt
        self.velocity += self.alpha(self.derivative_cutoff, dt) * (velocity - self.velocity)
        cutoff = self.min_cutoff + self.beta * abs(self.velocity)
        self.value += self.alpha(cutoff, dt) * (value - self.value)
        return self.value


class Channel:
    def __init__(self, min_cutoff, beta):
        self.adaptive = OneEuro(min_cutoff, beta)
        self.history = None

    def clear_history(self):
        self.history = None

    def filter(self, value, dt):
        if self.history is None:
            self.history = (value, value)
        previous, older = self.history
        self.history = (value, previous)
        median = sorted((value, previous, older))[1]
        return self.adaptive.filter(median, dt)


class DirectOscSmoother:
    """Reject gaze/lid outliers and smooth motion while retaining blinks and winks.

    Uses sample timestamps, resets after a gap/clock reset, and holds gaze when
    neither eye is open and reliably tracked. Eyelid levels remain uncalibrated.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.gaze = [Channel(0.8, 5.0) for _ in range(6)]
        # Faster lid response keeps short blinks visible.
        self.lids = [Channel(8.0, 2.0) for _ in range(2)]
        self.last_time = None
        self.last_gaze = (0.0,) * 6
        self.source = 0

    @staticmethod
    def angles(vector):
        x, y, z = vector
        return math.atan2(x, -z), math.atan2(y, -z)

    def process(self, record):
        t = record["sample_time"]
        dt = 1.0 / 90.0 if self.last_time is None else t - self.last_time
        if dt <= 0 or dt > 0.25:
            self.reset()
            dt = 1.0 / 90.0
        self.last_time = t
        dt = max(0.004, min(0.05, dt))

        use = [record["openness"][i] >= 0.55 and
               all(math.isfinite(v) and 0 <= v < 0.03 for v in record["cov"][i])
               for i in range(2)]
        source = int(use[0]) | (int(use[1]) << 1)
        if source != self.source:
            for channel in self.gaze:
                channel.clear_history()
            self.source = source
        if source:
            left, right = (self.angles(v) for v in record["gaze"])
            if all(use):
                combined = self.angles(record["fixation"])
            else:
                left = right = combined = left if use[0] else right
            angles = left + right + combined
            self.last_gaze = tuple(f.filter(v, dt) for f, v in zip(self.gaze, angles))

        lids = []
        for channel, value in zip(self.lids, record["openness"]):
            filtered = channel.filter(max(0.0, min(1.0, value)), dt)
            lids.append(0.0 if filtered < 0.03 else 1.0 if filtered > 0.97 else filtered)
        scale = 4.0 / math.pi
        return tuple(max(-1.0, min(1.0, v * scale)) for v in self.last_gaze) + tuple(lids)
