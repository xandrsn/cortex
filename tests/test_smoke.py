"""Comprobaciones rápidas de Cortex (sin webcam ni micro): python -m unittest discover -s tests -t ."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class CortexSmoke(unittest.TestCase):
    def test_assistant_imports_and_defaults(self):
        from importlib.util import find_spec
        from pepito import config
        self.assertEqual(config.ROOT, ROOT)
        self.assertIn("window_gestures", config.DEFAULTS)
        self.assertFalse(config.DEFAULTS["gestures_enabled"])
        self.assertIsNotNone(find_spec("pepito.app"))   # no se importa: importarlo arranca el asistente

    def test_gesture_package_imports(self):
        from gesture import hand_tracker  # noqa: F401  (gesture.main abre la cámara al importarse: no se importa)
        self.assertTrue((ROOT / "src" / "gesture" / "hand_landmarker.task").exists())

    def test_no_secret_files_in_tree(self):
        for name in (".env", "whatsapp-mcp.json"):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertFalse((ROOT / "secrets").exists())


if __name__ == "__main__":
    unittest.main()
