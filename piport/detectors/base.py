# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Interface every detector backend implements."""
import threading

from . import postprocess


class Detector:
    """One shared model behind a lock -- accelerators serialize inference
    anyway, so it's as fast as one instance per camera.

    Subclasses set NAME and DEFAULT_MODEL, implement available() and
    _infer(), and set self.input_size in __init__.
    """

    NAME = ""
    DEFAULT_MODEL = ""

    def __init__(self, model_path=None):
        self.model_path = model_path or self.DEFAULT_MODEL
        self.input_size = 640  # square side in px; ffmpeg scales frames to this
        self._lock = threading.Lock()
        self._labels = postprocess.load_labels()

    @classmethod
    def available(cls, model_path=None):
        """(ok, reason): whether this backend's runtime, hardware and default
        model (or model_path) are present. Cheap -- loads nothing."""
        raise NotImplementedError

    def _infer(self, frame_rgb):
        """Run the model; return (boxes, classes, scores) in input_size pixel
        space, or Nones when nothing passes the threshold."""
        raise NotImplementedError

    def detect(self, frame_rgb):
        """frame_rgb: HxWx3 uint8 RGB, already resized to input_size (plain
        stretch, no letterboxing). Returns {"objectType", "score", "box":
        (x1,y1,x2,y2)} in input_size pixel space, score-descending, mapped to
        AI Port's person/vehicle/animal vocabulary."""
        with self._lock:
            boxes, classes, scores = self._infer(frame_rgb)
        return postprocess.to_results(boxes, classes, scores, self._labels)
