"""Tests for Python environments (hpclib/servers/rest_envs.py) through the
REST server: detecting uv and pixi projects, syncing them, and jobs that run
in them. uv and pixi are faked; SLURM is faked as in test_rest_jobs.

Run with:  python -m unittest discover -s tests -p test_rest_envs.py
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import JobServerTestCase  # noqa: E402
import rest_envs  # noqa: E402
import rest_jobs  # noqa: E402
from rest_client import RESTClient  # noqa: E402
from rest_server import TokenAuth  # noqa: E402

FAKE_UV = textwrap.dedent("""\
    #!/bin/bash
    echo "uv $*" >> "$FAKE_TOOLS/calls"
    case "$1 $2" in
      "--version "*) echo "uv 0.9.9"; exit 0 ;;
      "cache dir") echo "$FAKE_TOOLS/uv-cache"; exit 0 ;;
      "python dir") echo "$FAKE_TOOLS/uv-python"; exit 0 ;;
    esac
    if [ "$1" = sync ]; then
      [ -f pyproject.toml ] || { echo "no pyproject.toml" >&2; exit 2; }
      [ -n "${FAKE_FAIL:-}" ] && { echo "resolution failed" >&2; exit 1; }
      echo "UV_CACHE_DIR=$UV_CACHE_DIR UV_LINK_MODE=$UV_LINK_MODE"
      mkdir -p .venv/bin
      ln -sf "$FAKE_PYTHON" .venv/bin/python
      [ "${2:-}" = --locked ] || echo "lock" > uv.lock
      echo "Installed 3 packages"
      exit 0
    fi
    echo "fake uv: unexpected $*" >&2; exit 9
""")

FAKE_PIXI = textwrap.dedent("""\
    #!/bin/bash
    echo "pixi $*" >> "$FAKE_TOOLS/calls"
    case "$1" in
      --version) echo "pixi 0.55.0"; exit 0 ;;
      info) echo "{\\"cache_dir\\": \\"$FAKE_TOOLS/rattler\\"}"; exit 0 ;;
      install)
        shift; manifest=; env=default
        while [ "$#" -gt 0 ]; do
          case "$1" in --manifest-path) manifest="$2"; shift ;; -e) env="$2"; shift ;; esac; shift
        done
        root=$(dirname "$manifest")
        p="$root/.pixi/envs/$env"
        mkdir -p "$p/conda-meta" "$p/bin" "$p/etc/conda/activate.d"
        printf '#!/bin/bash\\necho xtb 6.7\\n' > "$p/bin/xtb"; chmod +x "$p/bin/xtb"
        echo 'export XTBPATH="$CONDA_PREFIX/share/xtb"' > "$p/etc/conda/activate.d/xtb.sh"
        echo "PIXI_CACHE_DIR=$PIXI_CACHE_DIR"
        echo "installed $env"
        exit 0 ;;
    esac
    echo "fake pixi: unexpected $*" >&2; exit 9
""")

UV_PYPROJECT = '[project]\nname = "analysis"\nversion = "0.1"\nrequires-python = ">=3.9"\ndependencies = []\n'
PIXI_TOML = '[workspace]\nname = "xtb-runs"\nchannels = ["conda-forge"]\nplatforms = ["linux-64"]\n' \
            '[dependencies]\nxtb = "*"\n'


class EnvTestCase(JobServerTestCase):

    def setUp(self):
        super().setUp()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for name, text in (("uv", FAKE_UV), ("pixi", FAKE_PIXI)):
            (self.bin_dir / name).write_text(text)
            (self.bin_dir / name).chmod(0o755)
        env = mock.patch.dict(os.environ, {"FAKE_TOOLS": str(self.tools), "FAKE_PYTHON": sys.executable})
        env.start()
        self.addCleanup(env.stop)
        self.envs = rest_envs.EnvironmentManager({}, data_dir=str(self.data / "rest"))
        self.jobs.environments = self.envs
        self.env_token = TokenAuth.add_token(str(self.tokens_file), "envy", ["read", "submit", "envs"],
                                             [str(self.llm_root)])
        self.agent = RESTClient(self.url, token=self.env_token)
        self.uv_project = self.llm_root / "analysis"
        self.uv_project.mkdir()
        (self.uv_project / "pyproject.toml").write_text(UV_PYPROJECT)
        (self.uv_project / "analyze.py").write_text("import sys\nprint('ran with', sys.prefix)\n")
        self.pixi_project = self.llm_root / "xtb-runs"
        self.pixi_project.mkdir()
        (self.pixi_project / "pixi.toml").write_text(PIXI_TOML)

    def calls(self):
        path = self.tools / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def sync(self, project, **kwargs):
        started = self.agent.sync_environment(str(project), **kwargs)
        return self.agent.sync_status(started["id"], wait=30)


class TestDetect(EnvTestCase):

    def test_kinds(self):
        self.assertEqual(rest_envs.EnvironmentManager.detect(str(self.uv_project))["manager"], "uv")
        self.assertEqual(rest_envs.EnvironmentManager.detect(str(self.pixi_project))["manager"], "pixi")
        both = self.llm_root / "both"
        both.mkdir()
        (both / "pyproject.toml").write_text(UV_PYPROJECT + "\n[tool.pixi.workspace]\nchannels = []\n")
        info = rest_envs.EnvironmentManager.detect(str(both))
        self.assertEqual((info["manager"], info["manifest"]), ("pixi", str(both / "pyproject.toml")))
        empty = self.llm_root / "empty"
        empty.mkdir()
        self.assertIsNone(rest_envs.EnvironmentManager.detect(str(empty))["manager"])
        with self.assertRaises(rest_envs.EnvironmentError_):
            rest_envs.EnvironmentManager.detect(str(self.pixi_project), "pixi", "bad name")

    def test_tools_and_info_route(self):
        out = self.llm.environment()
        self.assertEqual(out["managers"]["uv"], {"available": True, "version": "0.9.9"})
        self.assertEqual(out["managers"]["pixi"]["version"], "0.55.0")
        info = self.llm.environment(str(self.uv_project))
        self.assertEqual((info["manager"], info["ready"], info["locked"]), ("uv", False, False))
        self.assertIn("environments", self.llm.cluster())
        self.expect_error(403, self.llm.environment, str(self.outside))

    def test_tools_off_or_missing(self):
        self.jobs.environments = rest_envs.EnvironmentManager({"uv": None, "pixi": "/nowhere/pixi"})
        managers = self.llm.environment()["managers"]
        self.assertEqual(managers, {"uv": {"available": False}, "pixi": {"available": False}})
        payload = self.expect_error(503, self.agent.sync_environment, str(self.uv_project))
        self.assertIn("not installed", payload["error"])

    def test_bad_config(self):
        for bad in ({"conda": "auto"}, {"modules": ["x; rm"]}, {"timeout": 0}):
            with self.assertRaises(ValueError):
                rest_envs.EnvironmentManager(bad)


class TestSync(EnvTestCase):

    def test_needs_envs_scope(self):
        self.expect_error(403, self.llm.sync_environment, str(self.uv_project))
        self.expect_error(403, self.agent.sync_environment, str(self.outside))

    def test_uv(self):
        out = self.sync(self.uv_project)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertTrue(out["environment_info"]["ready"])
        self.assertIn("Installed 3 packages", out["log_tail"])
        self.assertIn(f"UV_CACHE_DIR={self.tools / 'uv-cache'} UV_LINK_MODE=copy", out["log_tail"])
        self.assertIn("uv sync", self.calls())                  # no lockfile yet: resolve
        out = self.sync(self.uv_project)
        self.assertIn("uv sync --locked", self.calls())         # with one: install exactly that
        self.sync(self.uv_project, update=True)
        self.assertEqual(self.calls()[-1], "uv sync")
        self.assertTrue((self.tools / "uv-cache").is_dir())

    def test_pixi(self):
        out = self.sync(self.pixi_project)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertEqual(out["environment_info"]["environment"], "default")
        self.assertTrue((self.pixi_project / ".pixi" / "envs" / "default" / "conda-meta").is_dir())
        self.assertTrue(any(c.startswith(f"pixi install --manifest-path {self.pixi_project}/pixi.toml -e default")
                            for c in self.calls()))
        (self.pixi_project / "pixi.lock").write_text("lock")
        self.sync(self.pixi_project, environment="gpu")
        self.assertTrue(self.calls()[-1].endswith("-e gpu --locked"))

    def test_failure_and_visibility(self):
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "1"}):
            out = self.sync(self.uv_project)
        self.assertEqual((out["state"], out["exit_code"]), ("failed", 1))
        self.assertIn("resolution failed", out["log_tail"])
        self.assertFalse(out["environment_info"]["ready"])
        self.expect_error(404, self.llm.sync_status, out["id"])     # another token's sync
        self.assertEqual(self.owner.sync_status(out["id"])["id"], out["id"])
        self.expect_error(404, self.agent.sync_status, "nope")

    def test_nothing_to_sync(self):
        empty = self.llm_root / "empty"
        empty.mkdir()
        self.expect_error(422, self.agent.sync_environment, str(empty))
        self.expect_error(400, self.agent.request, "POST", "/envs/sync", body={"project": "x", "pip": True})

    def test_config_variables_reach_the_sync(self):
        self.envs.sync_env = {"UV_INDEX_URL": "https://pypi.example/simple", "UV_LINK_MODE": "hardlink"}
        info = self.envs.detect(str(self.uv_project))
        script, _ = self.envs._script(info, self.envs.tools()["uv"], update=False)
        self.assertIn("export UV_INDEX_URL=https://pypi.example/simple", script)
        # set after hpclib's defaults, so they win
        self.assertGreater(script.index("export UV_LINK_MODE=hardlink"), script.index("export UV_LINK_MODE=copy"))
        out = self.sync(self.uv_project)
        self.assertIn("UV_LINK_MODE=hardlink", " ".join(out["log_tail"]))
        self.assertEqual(self.llm.environment()["sync_environment"], ["UV_INDEX_URL", "UV_LINK_MODE"])

    def test_runs_in_the_sandbox(self):
        runtime = lambda name: sys.executable if name == "singularity" else None  # noqa: E731
        self.jobs.sandbox = rest_jobs.rest_sandbox.Sandbox({"method": "auto"}, which=runtime,
                                                           data_dir=str(self.data / "rest"))
        info = self.envs.detect(str(self.uv_project))
        self.envs.sandbox = self.jobs.sandbox
        script, command = self.envs._script(info, self.envs.tools()["uv"], update=False)
        self.assertIn("--no-home", script)
        self.assertIn(f"--bind {self.uv_project}:{self.uv_project} ", script)       # the project: writable
        self.assertIn(f"--bind {self.tools / 'uv-cache'}:{self.tools / 'uv-cache'} ", script)
        self.assertIn(f"--bind {self.bin_dir / 'uv'}:{self.bin_dir / 'uv'}:ro", script)  # the tool: read-only
        self.assertNotIn(f"--bind {self.llm_root}:", script)                       # not the other directories


class TestJobs(EnvTestCase):

    def submit(self, project, dry_run=True, **params):
        return self.agent.submit_job("python_project", dict(
            {"project": str(project), "script": str(project / "analyze.py")}, **params), dry_run=dry_run)

    def test_needs_a_synced_environment(self):
        payload = self.expect_error(422, self.submit, self.uv_project)
        self.assertIn("sync it first", payload["error"])
        self.assertFalse(payload["environment"]["ready"])

    def test_uv_job(self):
        self.sync(self.uv_project)
        plan = self.submit(self.uv_project, args="--fast out.json")
        self.assertEqual(plan["environment"]["manager"], "uv")
        self.assertIn(f"export VIRTUAL_ENV={self.uv_project / '.venv'}", plan["script"])
        # the job script, run here (unsandboxed): python is the project's
        res = subprocess.run(["bash", "-c", plan["script"]], capture_output=True, text=True, cwd=self.uv_project,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"python: {self.uv_project / '.venv' / 'bin' / 'python'}", res.stdout)
        self.assertIn("ran with", res.stdout)
        self.expect_error(422, self.submit, self.uv_project, args="; rm -rf ~")

    def test_pixi_activation(self):
        self.sync(self.pixi_project)
        info = self.envs.detect(str(self.pixi_project))
        lines = self.envs.prelude(info) + ['echo "$XTBPATH"; xtb']
        res = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        env = self.pixi_project / ".pixi" / "envs" / "default"
        self.assertEqual(res.stdout.split(), [f"{env}/share/xtb", "xtb", "6.7"])

    def test_broken_environment_fails_clearly(self):
        info = self.envs.detect(str(self.pixi_project), "pixi")
        res = subprocess.run(["bash", "-c", "\n".join(self.envs.prelude(info) + ["echo ran"])],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 4)
        self.assertIn("sync it again", res.stderr)
        self.assertNotIn("ran", res.stdout)

    def test_sandboxed_job_activates_inside(self):
        self.sync(self.uv_project)
        (self.tools / "uv-python").mkdir(exist_ok=True)      # made by the sync
        runtime = lambda name: sys.executable if name == "singularity" else None  # noqa: E731
        self.jobs.sandbox = rest_jobs.rest_sandbox.Sandbox({"method": "auto"}, which=runtime,
                                                           data_dir=str(self.data / "rest"))
        plan = self.submit(self.uv_project)
        script = plan["script"]
        self.assertIn("hpc_sandbox_prelude=$(cat <<'HPC_SANDBOX_PRELUDE_", script)
        self.assertIn('/bin/bash -c "$hpc_sandbox_prelude" hpc-env', script)
        self.assertIn(str(self.tools / "uv-python"), plan["sandbox"]["read_only"])   # uv's Pythons
        # activation happens inside the container, not in the job script before it
        before_launch = script.split("hpc_sandbox_prelude=")[0]
        self.assertNotIn("VIRTUAL_ENV", before_launch)
        res = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_template_validation(self):
        spec = {"description": "x", "parameters": {"p": {"type": "path"}}}
        for bad in ({"project": "${q}"}, {"manager": "conda", "project": "${p}"}, {"project": ""},
                    {"project": "${p}", "extra": 1}):
            with self.assertRaises(ValueError):
                rest_jobs.JobTemplate("t", dict(spec, environment=bad), "#!/bin/bash\n")
        ok = rest_jobs.JobTemplate("t", dict(spec, environment={"project": "${p}"}), "#!/bin/bash\n")
        self.assertEqual(ok.spec["environment"], {"project": "${p}", "manager": "auto", "name": "default",
                                                  "required": True})


if __name__ == "__main__":
    import unittest
    unittest.main()
