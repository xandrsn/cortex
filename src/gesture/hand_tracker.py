"""
hand_tracker.py
Wraps the MediaPipe Tasks HandLandmarker (0.10+) to detect and expose
hand landmark positions.  Returns normalized (0-1) coordinates with the
x-axis mirrored so that moving your right hand right moves the cursor right.

The model file (~4 MB) is downloaded automatically on first run.
"""

import os
import time
import urllib.request

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

# MediaPipe landmark indices used by this project
THUMB_TIP  = 4
INDEX_TIP  = 8
MIDDLE_TIP = 12   # available for future gestures

_MODEL_URL  = (
    "https://storage.googleapis.com/mediapipe-models/"
    "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
)
_MODEL_PATH = os.path.join(os.path.dirname(__file__), "hand_landmarker.task")


def _ensure_model():
    if not os.path.exists(_MODEL_PATH):
        print(f"Downloading hand-landmark model (~4 MB) …")
        try:
            urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
            print("  Model saved to", _MODEL_PATH)
        except Exception as exc:
            raise RuntimeError(
                f"Could not download the MediaPipe model.\n"
                f"URL: {_MODEL_URL}\n"
                f"Error: {exc}\n"
                "Please download the file manually and place it in the project folder."
            ) from exc


class HandTracker:
    def __init__(
        self,
        max_hands: int = 1,
        detection_confidence: float = 0.7,
        tracking_confidence: float = 0.5,
    ):
        _ensure_model()

        base_options = mp_python.BaseOptions(model_asset_path=_MODEL_PATH)
        options = mp_vision.HandLandmarkerOptions(
            base_options=base_options,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=max_hands,
            min_hand_detection_confidence=detection_confidence,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=tracking_confidence,
        )
        self._landmarker = mp_vision.HandLandmarker.create_from_options(options)
        self._t0 = time.monotonic()

    def process(self, bgr_frame):
        """
        Run HandLandmarker on a BGR frame.
        Returns the list of NormalizedLandmark for the first hand, or None.
        """
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int((time.monotonic() - self._t0) * 1000)
        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        if result.hand_landmarks:
            return result.hand_landmarks[0]   # list[NormalizedLandmark]
        return None

    def get_landmark_positions(self, landmarks) -> dict | None:
        """
        Convert raw landmark list to a dict keyed by landmark index.
        Values are (x, y) floats in [0, 1] with x mirrored for natural interaction.
        Returns None when landmarks is None.
        """
        if landmarks is None:
            return None
        return {
            idx: (1.0 - lm.x, lm.y)
            for idx, lm in enumerate(landmarks)
        }

    def close(self):
        self._landmarker.close()
