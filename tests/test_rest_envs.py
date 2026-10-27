"""Tests for Python environments (hpclib/servers/rest_envs.py) through the
REST server: detecting uv and pixi projects, syncing them, and jobs that run
in them. uv and pixi are faked; SLURM is faked as in test_rest_jobs.

Run with:  python -m unittest discover -s tests -p test_rest_envs.py
"""
import json
import os
import shutil
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
      # a real (empty) virtual environment: python in it is the one running the tests
      [ -x .venv/bin/python ] || "$FAKE_PYTHON" -m venv --without-pip --symlinks .venv || exit 3
      [ "${2:-}" = --locked ] || echo "lock" > uv.lock
      echo "Installed 3 packages"
      exit 0
    fi
    if [ "$1" = export ]; then
      if [ -f "$FAKE_TOOLS/pins-$(basename "$PWD")" ]; then cat "$FAKE_TOOLS/pins-$(basename "$PWD")"
      else printf '%s\\n' 'numpy==2.5.4 \\' '    --hash=sha256:x' '    # via analysis' 'six==1.17.0'; fi
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
      list)
        echo '[{"name": "python", "version": "3.12.1", "build": "h1_cpython", "kind": "conda"},
               {"name": "numpy", "version": "2.5.4", "build": "py312_0", "kind": "conda"},
               {"name": "tqdm", "version": "4.67.1", "build": null, "kind": "pypi"}]'
        exit 0 ;;
      install)
        shift; manifest=; env=default
        while [ "$#" -gt 0 ]; do
          case "$1" in --manifest-path) manifest="$2"; shift ;; -e) env="$2"; shift ;; --skip) shift ;; esac; shift
        done
        root=$(dirname "$manifest")
        p="$root/.pixi/envs/$env"
        mkdir -p "$p/conda-meta" "$p/bin" "$p/etc/conda/activate.d" "$p/lib"
        printf '#!/bin/bash\\necho xtb 6.7\\n' > "$p/bin/xtb"; chmod +x "$p/bin/xtb"
        echo 'export XTBPATH="$CONDA_PREFIX/share/xtb"' > "$p/etc/conda/activate.d/xtb.sh"
        echo "PIXI_CACHE_DIR=$PIXI_CACHE_DIR"
        echo "installed $env"
        exit 0 ;;
    esac
    echo "fake pixi: unexpected $*" >&2; exit 9
""")

# a SquashFS image is a tar file here: packed and unpacked faithfully, mounted by nothing
FAKE_MKSQUASHFS = textwrap.dedent("""\
    #!/bin/bash
    echo "mksquashfs $*" >> "$FAKE_TOOLS/calls"
    [ -n "$(ls -A "$1")" ] || { echo "nothing in $1" >&2; exit 1; }
    tar -C "$1" -cf "$2" . && echo "Parallel mksquashfs: Using 4 processors"
""")
FAKE_UNSQUASHFS = textwrap.dedent("""\
    #!/bin/bash
    echo "unsquashfs $*" >> "$FAKE_TOOLS/calls"
    [ "$1 $2" = "-f -d" ] || exit 2
    tar -C "$3" -xf "$4"
""")

UV_PYPROJECT = '[project]\nname = "analysis"\nversion = "0.1"\nrequires-python = ">=3.9"\ndependencies = []\n'
PIXI_TOML = '[workspace]\nname = "xtb-runs"\nchannels = ["conda-forge"]\nplatforms = ["linux-64"]\n' \
            '[dependencies]\nxtb = "*"\n'


class EnvTestCase(JobServerTestCase):

    def setUp(self):
        super().setUp()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for name, text in (("uv", FAKE_UV), ("pixi", FAKE_PIXI), ("mksquashfs", FAKE_MKSQUASHFS),
                           ("unsquashfs", FAKE_UNSQUASHFS)):
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

    def calls(self, prefix=""):
        path = self.tools / "calls"
        return [c for c in (path.read_text().splitlines() if path.exists() else []) if c.startswith(prefix)]

    def submit(self, project, dry_run=True, **params):
        return self.agent.submit_job("python_project", dict(
            {"project": str(project), "script": str(project / "analyze.py")}, **params), dry_run=dry_run)

    def syncs(self):
        """The calls that sync (not the ones that describe the result)."""
        return [c for c in self.calls() if c.startswith(("uv sync", "pixi install"))]

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
        self.assertEqual(self.syncs()[-1], "uv sync")
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
        self.assertTrue(self.syncs()[-1].endswith("-e gpu --locked"))

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

    def test_uv_squash(self):
        out = self.sync(self.uv_project, squash=True)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertIn("uv sync --compile-bytecode", self.syncs())    # the image is read-only: compile now
        self.assertEqual(self.calls("mksquashfs")[-1], "mksquashfs .venv .venv.sqfs.tmp -noappend -no-xattrs -no-progress")
        venv, image = self.uv_project / ".venv", self.uv_project / ".venv.sqfs"
        self.assertTrue(image.is_file())
        self.assertFalse((self.uv_project / ".venv.sqfs.tmp").exists())
        self.assertEqual(os.listdir(venv), [])                       # the mount point, emptied
        self.assertNotIn("Parallel mksquashfs", out["log_tail"])     # its summary isn't the log's
        info = out["environment_info"]
        self.assertEqual((info["ready"], info["squashed"], info["image"]), (True, True, str(image)))
        self.assertTrue(out["squash"])
        self.assertEqual(self.envs.job_images(info), [(str(image), str(venv))])
        # the record: the interpreter and the packages (what a layer on it needs)
        record = json.loads((self.uv_project / ".venv.hpclib.json").read_text())
        self.assertEqual([(p["name"], p["version"]) for p in record["packages"]], [("numpy", "2.5.4"), ("six", "1.17.0")])
        self.assertEqual(record["python"]["site_packages"].split("/lib/")[0], str(venv))
        self.assertTrue(record["squashed"])
        self.assertFalse(list(self.uv_project.glob("*.tmp")))
        self.assertFalse((self.uv_project / ".venv.hpclib-syncing").exists())
        # synced again, squash left out: it stays squashed (built again, packed again)
        out = self.sync(self.uv_project)
        self.assertEqual(self.syncs()[-1], "uv sync --locked --compile-bytecode")
        self.assertTrue(out["environment_info"]["squashed"])
        # squash=false unpacks it, and the image goes
        out = self.sync(self.uv_project, squash=False)
        self.assertEqual(self.syncs()[-1], "uv sync --locked")
        self.assertIn("removed .venv.sqfs: the environment is unpacked in .venv again", out["log_tail"])
        self.assertFalse(image.exists())
        self.assertEqual((out["environment_info"]["ready"], out["environment_info"]["squashed"]), (True, False))
        self.assertEqual(self.envs.job_images(out["environment_info"]), [])

    def test_squash_and_unsquash_on_their_own(self):
        self.expect_error(422, self.agent.squash_environment, str(self.uv_project))      # nothing synced yet
        self.sync(self.uv_project)
        python_cfg = (self.uv_project / ".venv" / "pyvenv.cfg").read_text()
        started = self.agent.squash_environment(str(self.uv_project))
        self.assertEqual(started["kind"], "squash")
        out = self.agent.sync_status(started["id"], wait=30)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertEqual(self.syncs(), ["uv sync"])                                      # no sync this time
        self.assertTrue(out["environment_info"]["squashed"])
        self.assertEqual(os.listdir(self.uv_project / ".venv"), [])
        self.expect_error(409, self.agent.squash_environment, str(self.uv_project))      # already
        started = self.agent.unsquash_environment(str(self.uv_project))
        out = self.agent.sync_status(started["id"], wait=30)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertEqual(self.calls("unsquashfs")[-1], "unsquashfs -f -d .venv .venv.sqfs")
        self.assertEqual((self.uv_project / ".venv" / "pyvenv.cfg").read_text(), python_cfg)
        self.assertFalse((self.uv_project / ".venv.sqfs").exists())
        self.assertEqual((out["environment_info"]["squashed"], out["environment_info"]["ready"]), (False, True))
        self.expect_error(409, self.agent.unsquash_environment, str(self.uv_project))    # not squashed
        # pixi too: .pixi-NAME.sqfs beside .pixi/, which pixi tidies
        self.sync(self.pixi_project)
        out = self.agent.sync_status(self.agent.squash_environment(str(self.pixi_project))["id"], wait=30)
        self.assertEqual(out["state"], "succeeded", out)
        self.assertTrue((self.pixi_project / ".pixi-default.sqfs").is_file())
        self.assertEqual(os.listdir(self.pixi_project / ".pixi" / "envs" / "default"), [])
        info = out["environment_info"]
        self.assertEqual((info["squashed"], info["ready"], info["packages"]), (True, True, 3))

    def test_squash_failures(self):
        self.expect_error(400, self.agent.request, "POST", "/envs/sync",
                          body={"project": str(self.uv_project), "squash": "yes"})
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "1"}):
            out = self.sync(self.uv_project, squash=True)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(self.calls("mksquashfs"), [])                   # nothing packed after a failed sync
        self.assertIn("last sync failed", out["environment_info"]["problem"])
        # no mksquashfs where the server runs: refused at once, before a long sync
        which = shutil.which
        with mock.patch.object(rest_envs.shutil, "which",
                               side_effect=lambda n, path=None: None if "squashfs" in n else which(n, path=path)):
            payload = self.expect_error(503, self.agent.sync_environment, str(self.uv_project), squash=True)
            self.assertIn("needs mksquashfs", payload["error"])
        self.assertFalse((self.uv_project / ".venv.sqfs").exists())

    def test_config_variables_reach_the_sync(self):
        self.envs.sync_env = {"UV_INDEX_URL": "https://pypi.example/simple", "UV_LINK_MODE": "hardlink"}
        info = self.envs.detect(str(self.uv_project))
        script, _ = self.envs._script(info, self.envs.tools()["uv"], update=False, squash=False)
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
        script, command = self.envs._script(info, self.envs.tools()["uv"], update=False, squash=False)
        self.assertIn("--no-home", script)
        self.assertIn(f"--bind {self.uv_project}:{self.uv_project} ", script)       # the project: writable
        self.assertIn(f"--bind {self.tools / 'uv-cache'}:{self.tools / 'uv-cache'} ", script)
        self.assertIn(f"--bind {self.bin_dir / 'uv'}:{self.bin_dir / 'uv'}:ro", script)  # the tool: read-only
        self.assertNotIn(f"--bind {self.llm_root}:", script)                       # not the other directories


class TestJobs(EnvTestCase):

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

    def test_squashed_environment_is_mounted(self):
        self.sync(self.uv_project, squash=True)
        (self.tools / "uv-python").mkdir(exist_ok=True)
        venv, image = self.uv_project / ".venv", self.uv_project / ".venv.sqfs"
        # unsandboxed, nothing can mount it
        payload = self.expect_error(422, self.submit, self.uv_project)
        self.assertIn("squashed into an image", payload["error"])
        runtime = lambda name: sys.executable if name == "singularity" else None  # noqa: E731
        self.jobs.sandbox = rest_jobs.rest_sandbox.Sandbox({"method": "auto"}, which=runtime,
                                                           data_dir=str(self.data / "rest"))
        plan = self.submit(self.uv_project)
        self.assertEqual(plan["environment"]["image"], str(image))
        self.assertEqual(plan["sandbox"]["images"], [[str(image), str(venv)]])
        script = plan["script"]
        mount = f"--bind {image}:{venv}:image-src=/"
        self.assertIn(mount, script)
        self.assertGreater(script.index(mount), script.index(f"--bind {self.llm_root}:{self.llm_root} "))
        self.assertIn(f"export VIRTUAL_ENV={venv}", script)          # activated as before, on the mounted image
        res = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_changing_an_environment_jobs_use_is_refused(self):
        self.sync(self.uv_project)
        job = self.submit(self.uv_project, dry_run=False)
        self.assertEqual(self.jobs.registry.get(job["job_id"])["environments"], [str(self.uv_project / ".venv")])
        payload = self.expect_error(409, self.agent.squash_environment, str(self.uv_project))
        self.assertEqual(payload["jobs"], [job["job_id"]])
        self.expect_error(409, self.agent.sync_environment, str(self.uv_project), squash=True)
        self.sync(self.uv_project)                       # a plain sync isn't refused (as before)
        out = self.agent.sync_status(self.agent.squash_environment(str(self.uv_project), force=True)["id"], wait=30)
        self.assertEqual(out["state"], "succeeded", out)

    def test_busy_environment_takes_no_jobs(self):
        self.sync(self.uv_project)
        marker = self.uv_project / ".venv.hpclib-syncing"
        marker.write_text(json.dumps({"id": "x1", "kind": "squash", "pid": os.getpid(), "started": time.time()}))
        payload = self.expect_error(422, self.submit, self.uv_project)
        self.assertIn("a squash (x1) is working on it", payload["error"])
        self.expect_error(409, self.agent.sync_environment, str(self.uv_project))   # another server's operation
        marker.write_text(json.dumps({"id": "x1", "kind": "squash", "pid": 2 ** 22 + 7, "started": time.time()}))
        self.assertTrue(self.envs.detect(str(self.uv_project))["ready"])           # its server is gone: stale

    def test_template_validation(self):
        spec = {"description": "x", "parameters": {"p": {"type": "path"}}}
        for bad in ({"project": "${q}"}, {"manager": "conda", "project": "${p}"}, {"project": ""},
                    {"project": "${p}", "extra": 1}):
            with self.assertRaises(ValueError):
                rest_jobs.JobTemplate("t", dict(spec, environment=bad), "#!/bin/bash\n")
        ok = rest_jobs.JobTemplate("t", dict(spec, environment={"project": "${p}"}), "#!/bin/bash\n")
        self.assertEqual(ok.spec["environment"], {"project": "${p}", "manager": "auto", "name": "default",
                                                  "required": True})


class TestCore(EnvTestCase):
    """Environments built on a core: its packages used, not installed again; the manifest carries its versions."""

    def setUp(self):
        super().setUp()
        self.core = self.llm_root / "core"
        self.core.mkdir()
        (self.core / "pyproject.toml").write_text(UV_PYPROJECT.replace("analysis", "core"))
        self.layer = self.uv_project
        (self.layer / "pyproject.toml").write_text(
            '[project]\nname = "analysis"\nversion = "0.1"\ndependencies = ["pandas"]\n\n'
            '[tool.uv]\nconstraint-dependencies = ["scipy<2"]  # mine\n')

    def test_uv_layer(self):
        self.expect_error(422, self.agent.sync_environment, str(self.layer), core=str(self.core))   # core not synced
        out = self.sync(self.core, squash=True)
        self.assertEqual(out["state"], "succeeded", out)
        core_record = json.loads((self.core / ".venv.hpclib.json").read_text())
        out = self.sync(self.layer, core=str(self.core))
        self.assertEqual(out["state"], "succeeded", out)
        self.assertTrue(out["manifest_changed"])
        uv_call = self.syncs()[-1]
        self.assertIn(f"--python {core_record['python']['base_executable']}", uv_call)
        self.assertIn("--no-install-package numpy --no-install-package six", uv_call)
        self.assertNotIn("--locked", uv_call)                             # the manifest changed: lock again
        text = (self.layer / "pyproject.toml").read_text()
        self.assertIn('"scipy<2",', text)                                 # the user's constraint stays
        self.assertIn('"numpy==2.5.4",', text)
        self.assertIn("# hpclib core: ../core (hpclib replaces these lines at each sync)", text)
        self.assertIn('[tool.hpclib]\ncore = "../core"', text)
        site = out["environment_info"]["python"]["site_packages"]
        self.assertEqual((Path(site) / "_hpclib_core.pth").read_text().strip(), core_record["python"]["site_packages"])
        info = out["environment_info"]
        self.assertTrue(info["ready"], info)
        self.assertEqual((info["core"]["project"], info["core"]["squashed"]), (str(self.core), True))
        self.assertEqual(self.envs.job_images(info), [(str(self.core / ".venv.sqfs"), str(self.core / ".venv"))])
        self.assertEqual(self.envs.job_envs(info), [str(self.layer / ".venv"), str(self.core / ".venv")])
        prelude = "\n".join(self.envs.prelude(info))
        self.assertIn(f'export PATH="$VIRTUAL_ENV/bin:{self.core}/.venv/bin:$PATH"', prelude)
        # synced again: the manifest says which core (and nothing changes in it); --locked again
        out = self.sync(self.layer)
        self.assertFalse(out["manifest_changed"])
        self.assertIn("--locked", self.syncs()[-1])
        self.assertIn("--no-install-package numpy", self.syncs()[-1])
        # the core is synced again (new versions): the layer waits for its own sync
        (self.tools / "pins-core").write_text("numpy==2.6.0\nsix==1.17.0\n")
        self.sync(self.core)
        info = self.envs.detect(str(self.layer))
        self.assertFalse(info["ready"])
        self.assertIn("synced again since", info["problem"])
        payload = self.expect_error(422, self.submit, self.layer)
        self.assertIn("synced again since", payload["error"])
        out = self.sync(self.layer)
        self.assertTrue(out["environment_info"]["ready"])
        self.assertIn('"numpy==2.6.0",', (self.layer / "pyproject.toml").read_text())
        # core="" (MCP) / null: on its own again, and the manifest as the user had it
        out = self.sync(self.layer, core=None)
        text = (self.layer / "pyproject.toml").read_text()
        self.assertNotIn("hpclib", text)
        self.assertIn('"scipy<2",', text)
        self.assertNotIn("--no-install-package", self.syncs()[-1])
        self.assertNotIn("core", out["environment_info"])

    def test_refused_cores(self):
        self.sync(self.core)
        self.expect_error(422, self.agent.sync_environment, str(self.layer), core=str(self.layer))   # itself
        self.sync(self.pixi_project)
        self.expect_error(422, self.agent.sync_environment, str(self.layer), core=str(self.pixi_project))
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.expect_error(403, self.agent.sync_environment, str(self.layer), core=str(outside))
        self.sync(self.layer, core=str(self.core))
        other = self.llm_root / "other"
        other.mkdir()
        (other / "pyproject.toml").write_text(UV_PYPROJECT.replace("analysis", "other"))
        payload = self.expect_error(422, self.agent.sync_environment, str(other), core=str(self.layer))
        self.assertIn("itself built on a core", payload["error"])
        # a constraint written another way: refused, the file untouched
        (other / "pyproject.toml").write_text(UV_PYPROJECT + '[tool]\nuv = { constraint-dependencies = ["x"] }\n')
        if rest_envs.rest_manifests.tomllib is not None:
            before = (other / "pyproject.toml").read_text()
            payload = self.expect_error(422, self.agent.sync_environment, str(other), core=str(self.core))
            self.assertIn("outside a [tool.uv] table", payload["error"])
            self.assertEqual((other / "pyproject.toml").read_text(), before)

    def test_pixi_layer(self):
        core = self.llm_root / "pixi-core"
        core.mkdir()
        (core / "pixi.toml").write_text(PIXI_TOML.replace("xtb-runs", "pixi-core"))
        self.sync(core, squash=True)
        out = self.sync(self.pixi_project, core=str(core))
        self.assertEqual(out["state"], "succeeded", out)
        call = self.syncs()[-1]
        self.assertIn("--skip numpy --skip python --skip tqdm", call)
        text = (self.pixi_project / "pixi.toml").read_text()
        self.assertIn('numpy = { version = "==2.5.4", build = "py312_0" }', text)
        self.assertIn('python = { version = "==3.12.1", build = "h1_cpython" }', text)
        self.assertNotIn("tqdm", text.split("[constraints]")[1].split("[tool.hpclib]")[0])   # conda packages only
        self.assertIn('[tool.hpclib]\ncore = "../pixi-core"\ncore-environment = "default"', text)
        info = out["environment_info"]
        self.assertTrue(info["ready"], info)
        layer, core_env = self.pixi_project / ".pixi" / "envs" / "default", core / ".pixi" / "envs" / "default"
        self.assertEqual(self.envs.job_images(info), [(str(core / ".pixi-default.sqfs"), str(core_env))])
        # in a job: the core's interpreter and libraries, the layer's packages through the user site; both
        # environments' activation scripts. Here, the core unpacked for the run.
        out = self.agent.sync_status(self.agent.unsquash_environment(str(core))["id"], wait=30)
        self.assertEqual(out["state"], "succeeded", out)
        info = self.envs.detect(str(self.pixi_project))
        self.assertTrue(info["ready"], info)                  # unsquashing doesn't change the core it was built on
        (core_env / "bin" / "python").symlink_to(sys.executable)
        site = layer / "lib" / "python" / "site-packages"
        res = subprocess.run(["bash", "-c", "\n".join(self.envs.prelude(info) + [
            'echo "$CONDA_PREFIX|$PYTHONUSERBASE|$XTBPATH"', 'echo "${LD_LIBRARY_PATH%%:*}"', "command -v python"])],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], f"{core_env}|{layer}|{layer}/share/xtb")
        self.assertEqual(lines[1], f"{layer}/lib")
        self.assertEqual(lines[2], f"{core_env}/bin/python")


if __name__ == "__main__":
    import unittest
    unittest.main()
