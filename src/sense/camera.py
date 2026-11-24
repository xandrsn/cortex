"""One webcam shared by several readers: vision (gestures) and window control read at the same time.

Windows does not allow opening the same webcam twice; SharedCamera opens it once, reads it in a thread and each
reader gets an object with read()/release() like cv2.VideoCapture.
"""

import threading

import cv2


class _Reader:
    def __init__(self, cam: "SharedCamera"):
        self._cam = cam
        self._seq = 0

    def isOpened(self) -> bool:   # noqa: N802 (same API as cv2.VideoCapture)
        return self._cam.opened

    def read(self, timeout: float = 1.0):
        """The next frame this reader has not seen yet: (True, frame), or (False, None) if none arrives."""
        frame, self._seq = self._cam.wait(self._seq, timeout)
        return (frame is not None), frame

    def set(self, *_):   # whoever opens the camera decides the resolution
        return False

    def release(self) -> None:
        self._cam.leave(self)


class SharedCamera:
    def __init__(self, index: int, width: int = 640, height: int = 480, opener=None):
        self.index, self.width, self.height = index, width, height
        self._opener = opener or self._open
        self._cap = None
        self._readers: set = set()
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._thread: threading.Thread | None = None

    @property
    def opened(self) -> bool:
        return self._cap is not None

    def _open(self, index):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def reader(self) -> _Reader | None:
        """A new reader; opens the camera if it is the first. None if it cannot be opened."""
        with self._lock:
            if self._cap is None:
                self._cap = self._opener(self.index)
                if self._cap is None:
                    return None
                self._thread = threading.Thread(target=self._run, args=(self._cap,), name="camera", daemon=True)
                self._thread.start()
            r = _Reader(self)
            self._readers.add(r)
            return r

    def leave(self, reader) -> None:
        """Remove a reader; the camera is released with the last one (so another app can use it)."""
        # shortcut: if vision reopens a hung camera while window control keeps reading, the
        # device is not really reopened; closing both readers (disable and re-enable gestures) fixes it.
        with self._lock:
            self._readers.discard(reader)
            if self._readers or self._cap is None:
                return
            cap, self._cap = self._cap, None
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=3)
        cap.release()

    def wait(self, after: int, timeout: float):
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after or self._cap is None, timeout)
            return (self._frame, self._seq) if self._seq > after else (None, after)

    def _run(self, cap):
        while self._cap is cap:
            ok, frame = cap.read()
            if not ok or frame is None:
                with self._cond:
                    self._cond.wait(0.05)
                continue
            with self._cond:
                self._frame, self._seq = frame, self._seq + 1
                self._cond.notify_all()
