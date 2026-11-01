import copy

import numpy as np

from ptzcam.config import DEFAULTS
from ptzcam.detector import Detection

CLS = {"person": 0, "bicycle": 1, "vehicle": 2, "motorcycle": 3, "animal": 16}


def det(x, y, w=0.06, h=0.15, score=0.8, category="person"):
    return Detection(np.array([x - w / 2, y - h / 2, x + w / 2, y + h / 2]), score, CLS[category], category)


class FakeConfig:
    def __init__(self, **patch):
        self.data = copy.deepcopy(DEFAULTS)
        for sec, vals in patch.items():
            self.data[sec].update(vals)


class FakeBus:
    def __init__(self):
        self.messages = []

    def publish(self, name, data):
        self.messages.append((name, data))

    def names(self):
        return [n for n, _ in self.messages]


class FakeRecorder:
    def __init__(self):
        self.jobs = []

    def request_clip(self, event_id, t0, t1, out_path, callback):
        self.jobs.append((event_id, t0, t1, out_path, callback))
