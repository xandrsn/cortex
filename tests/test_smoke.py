"""Quick Cortex checks (no webcam or mic): python -m pytest -q"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class CortexSmoke(unittest.TestCase):
    def test_hub_imports_and_defaults(self):
        from importlib.util import find_spec
        from hub import config
        self.assertEqual(config.ROOT, ROOT)
        self.assertTrue((config.WEB_DIR / "index.html").exists())
        self.assertNotIn("camera_index", config.DEFAULTS)   # the camera belongs to Sense
        self.assertIsNotNone(find_spec("hub.app"))   # not imported: importing it starts the assistant

    def test_sense_imports_and_defaults(self):
        from sense import config
        from sense.window import hand_tracker  # noqa: F401  (window.main opens pygame on use: not imported)
        self.assertEqual(config.ROOT, ROOT)
        self.assertTrue((config.WEB_DIR / "index.html").exists())
        self.assertTrue((ROOT / "src" / "sense" / "window" / "hand_landmarker.task").exists())
        self.assertEqual(config.DEFAULTS["ui_port"], 8766)

    def test_no_secret_files_in_tree(self):
        for name in (".env", "whatsapp-mcp.json"):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertFalse((ROOT / "secrets").exists())


if __name__ == "__main__":
    unittest.main()
