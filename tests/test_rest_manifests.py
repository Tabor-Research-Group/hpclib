"""hpclib's edits to a project's manifest for a layer on a core (hpclib/servers/rest_manifests.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hpclib" / "servers"))
import rest_manifests as m  # noqa: E402

PYPROJECT = '''[project]
name = "a"  # x
dependencies = ["pandas"]

[tool.uv]
constraint-dependencies = ["mything<2"]  # mine
dev-dependencies = []

[tool.ruff]
line-length = 100
'''
PIXI = '[workspace]\nname = "o"\n\n[dependencies]\npandas = "*"\n\n[constraints]\nfoo = "<2"\n\n[feature.x.dependencies]\nbar = "*"\n'


class TestManifests(unittest.TestCase):
    def test_uv_block_is_replaced_and_removed(self):
        a = m.set_uv_constraints(PYPROJECT, ["numpy==2.5.4", "six==1.17.0"], "../core")
        self.assertIn('"mything<2",\n    # hpclib core: ../core', a)
        self.assertIn('    "six==1.17.0",\n    # hpclib core end\n]  # mine\ndev-dependencies = []', a)
        b = m.set_uv_constraints(a, ["numpy==2.6.0"], "../core")
        self.assertEqual(b.count("# hpclib core end"), 1)
        self.assertNotIn("six", b)
        c = m.set_uv_constraints(b, [], "")
        self.assertNotIn("hpclib", c)
        self.assertIn('"mything<2"', c)
        self.assertIn("[tool.ruff]\nline-length = 100", c)
        if m.tomllib:
            self.assertEqual(m.tomllib.loads(a)["tool"]["uv"]["constraint-dependencies"],
                             ["mything<2", "numpy==2.5.4", "six==1.17.0"])
            self.assertEqual(m.tomllib.loads(c)["tool"]["uv"]["constraint-dependencies"], ["mything<2"])

    def test_uv_table_or_key_added(self):
        out = m.set_uv_constraints('[project]\nname = "x"\n', ["n==1"], "c")
        self.assertTrue(out.endswith('[tool.uv]\nconstraint-dependencies = [\n    # hpclib core: c (hpclib replaces '
                                     'these lines at each sync)\n    "n==1",\n    # hpclib core end\n]\n'))
        out = m.set_uv_constraints('[tool.uv]\npackage = false\n', ["n==1"], "c")
        self.assertTrue(out.startswith("[tool.uv]\nconstraint-dependencies = [\n"))
        self.assertIn("package = false", out)
        self.assertEqual(m.set_uv_constraints('[project]\nname = "x"\n', [], "c"), '[project]\nname = "x"\n')

    @unittest.skipUnless(m.tomllib, "needs tomllib (Python 3.11+)")
    def test_refuses_what_it_cant_edit(self):
        with self.assertRaisesRegex(m.ManifestError, "outside a \\[tool.uv\\] table"):
            m.set_uv_constraints('[tool]\nuv = { constraint-dependencies = ["x"] }\n', ["n==1"], "c")
        with self.assertRaisesRegex(m.ManifestError, "already constrains numpy"):
            m.set_pixi_constraints('[constraints]\nnumpy = "<3"\n', [("numpy", "2.5", "b")], "c")

    def test_pixi_block(self):
        q = m.set_pixi_constraints(PIXI, [("numpy", "2.5.4", "py312_0"), ("python", "3.12.16", "h5_cpython")], "../core")
        self.assertIn('foo = "<2"\n# hpclib core: ../core', q)
        self.assertIn('numpy = { version = "==2.5.4", build = "py312_0" }', q)
        self.assertIn("# hpclib core end\n\n[feature.x.dependencies]", q)
        self.assertEqual(m.set_pixi_constraints(q, [], "c"), PIXI)
        added = m.set_pixi_constraints('[workspace]\nname = "o"\n', [("numpy", "2", "b")], "c")
        self.assertIn("\n\n[constraints]\n# hpclib core: c", added)
        py = m.set_pixi_constraints('[project]\nname = "o"\n\n[tool.pixi.workspace]\nchannels = []\n',
                                    [("numpy", "2", "b")], "c", pyproject=True)
        self.assertIn("[tool.pixi.constraints]\n# hpclib core", py)

    def test_core_declaration(self):
        out = m.set_core(PYPROJECT, "../core")
        self.assertEqual(m.read_core(out), {"core": "../core"})
        out = m.set_core(out, "../other", "gpu")
        self.assertEqual(m.read_core(out), {"core": "../other", "core-environment": "gpu"})
        self.assertEqual(m.set_core(out, None), PYPROJECT)
        kept = m.set_core(PYPROJECT + '\n[tool.hpclib]\nnote = "mine"\ncore = "x"\n', None)
        self.assertIn('[tool.hpclib]\nnote = "mine"', kept)
        with self.assertRaises(m.ManifestError):
            m.set_core(PYPROJECT, 'a"b')


if __name__ == "__main__":
    unittest.main()
