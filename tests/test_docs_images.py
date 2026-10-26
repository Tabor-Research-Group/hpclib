"""The docs' screenshots: every picture a page shows exists, and every one in docs/images is used."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"


class TestDocsImages(unittest.TestCase):
    def test_pictures_and_pages_agree(self):
        used = set()
        for page in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
            for ref in re.findall(r'!\[[^\]]*\]\(([^)]+\.png)\)|<img src="([^"]+\.png)"', page.read_text()):
                target = (page.parent / (ref[0] or ref[1])).resolve()
                self.assertTrue(target.is_file(), f"{page.relative_to(ROOT)} shows {ref[0] or ref[1]}, which is missing")
                used.add(target)
        unused = sorted(p.name for p in IMAGES.glob("*.png") if p.resolve() not in used)
        self.assertEqual(unused, [], "pictures in docs/images no page shows")

    def test_each_picture_is_one_the_script_takes(self):
        script = (IMAGES / "screenshots.py").read_text()
        for p in IMAGES.glob("*.png"):
            self.assertIn(f'("{p.stem}",', script, f"{p.name} isn't in screenshots.py's DOCS")


if __name__ == "__main__":
    unittest.main()
