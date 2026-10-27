"""console_styles.py: Interface Settings as a form over user.css (read, write, the marked block, the one-time
moves)."""
import json
import tempfile
import unittest
from pathlib import Path

import test_agent_console  # noqa: F401  (puts hpclib/servers on the path)
import console_styles as cs  # noqa: E402

DEFAULTS = (Path(__file__).resolve().parents[1] / "agent-console" / "default-styles.css").read_text()


class TestConsoleStyles(unittest.TestCase):
    def test_variables_and_parts(self):
        known = cs.variables(DEFAULTS)
        self.assertEqual(known["--bg"], {"default": "#ffffff", "dark": "#18181a"})
        self.assertIsNone(known["--radius-control"]["dark"])

    def test_a_copy_of_the_defaults_is_edited_in_place(self):
        out = cs.write(DEFAULTS, DEFAULTS, {"common": {"--content-max-width": "1800px"}, "light": {"--bg": "#ff4435"},
                                            "dark": {"--bg": "#000000"}})
        self.assertNotIn(cs.BEGIN, out)                                   # every variable had a line already
        self.assertEqual(out.count("--bg: #000000;"), 2)                  # both dark blocks
        self.assertEqual(cs.read(out, DEFAULTS), {"common": {"--content-max-width": "1800px"},
                                                  "light": {"--bg": "#ff4435"}, "dark": {"--bg": "#000000"}})
        # back to the defaults: the changed lines go; the copy's other lines, holding defaults, stay
        back = cs.write(out, DEFAULTS, {"common": {}, "light": {}, "dark": {}})
        self.assertNotIn("--bg: #ff4435", back)
        self.assertIn("--fg: #1d1d1f;", back)
        self.assertEqual(cs.read(back, DEFAULTS), {"common": {}, "light": {}, "dark": {}})

    def test_the_block_is_rewritten_each_save(self):
        first = cs.write("main { padding: 0; }\n", DEFAULTS, {"common": {}, "light": {"--bg": "#111111"}, "dark": {}})
        self.assertTrue(first.startswith("main { padding: 0; }\n" + cs.BEGIN))
        second = cs.write(first, DEFAULTS, {"common": {}, "light": {"--bg": "#222222"}, "dark": {}})
        self.assertEqual(second.count(cs.BEGIN), 1)
        self.assertIn("--bg: #222222;", second)
        self.assertNotIn("#111111", second)
        self.assertEqual(cs.write(second, DEFAULTS, {"common": {}, "light": {}, "dark": {}}), "main { padding: 0; }\n")

    def test_checked(self):
        for bad in ({"light": {"--x": "1"}}, {"dark": {"--radius-control": "2px"}}, {"common": {"--bg": "red"}},
                    {"light": {"--bg": "red;}"}}, {"light": {"--bg": "/* x */"}}, {"other": {}}):
            with self.assertRaises(cs.StyleError, msg=bad):
                cs.check(bad, DEFAULTS)
        self.assertEqual(cs.check({"light": {"--bg": " "}}, DEFAULTS)["light"], {})

    def test_an_old_default_moves_to_the_new_one_once(self):
        d = Path(tempfile.mkdtemp())
        (d / "user.css").write_text(":root {\n  --content-max-width: 1200px;\n}\n")
        self.assertEqual(cs.upgrade_defaults(str(d), DEFAULTS), ["content-width-1600"])
        self.assertIn("--content-max-width: 1600px;", (d / "user.css").read_text())
        (d / "user.css").write_text(":root {\n  --content-max-width: 1200px;\n}\n")    # chosen again: kept
        self.assertEqual(cs.upgrade_defaults(str(d), DEFAULTS), [])
        self.assertIn("1200px", (d / "user.css").read_text())
        self.assertEqual(json.loads((d / "user-css-upgrades.json").read_text()), ["content-width-1600"])


if __name__ == "__main__":
    unittest.main()
