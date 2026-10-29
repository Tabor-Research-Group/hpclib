"""Tests for the config's `extra_path`: directories added to PATH for the server and what it starts, found for you
where SLURM is only on a login shell's PATH (rest_sandbox.set_extra_path, rest_server.detect_slurm_path and friends).

Run with:  python -m unittest tests.test_extra_path -v   (or from tests/: python -m unittest test_extra_path)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rest_jobs import JobServerTestCase  # noqa: E402
import rest_sandbox  # noqa: E402
import rest_server  # noqa: E402


class LoginShellCase(unittest.TestCase):
    """A cluster whose SLURM is on a login shell's PATH (a profile line) but not on a server's: a fake HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.slurm = self.tmp / "cm" / "slurm" / "bin"
        self.slurm.mkdir(parents=True)
        for command in ("sbatch", "srun", "sinfo"):
            (self.slurm / command).write_text("#!/bin/sh\nexit 0\n")
            (self.slurm / command).chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / ".bash_profile").write_text(f'export PATH="$PATH:{self.slurm}"\n')
        plain = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not shutil.which("sbatch", path=p))
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": plain})
        patched.start()
        self.addCleanup(patched.stop)
        self.addCleanup(rest_sandbox.set_extra_path, ())
        rest_sandbox.set_extra_path(())
        rest_server._slurm_hint = (0.0, [])


class TestExtraPath(LoginShellCase):

    def test_set_extra_path_reaches_this_process_and_its_children(self):
        before = os.environ["PATH"]
        rest_sandbox.set_extra_path([str(self.slurm), "/opt/other/bin"])
        self.assertEqual(os.environ["PATH"], os.pathsep.join([before, str(self.slurm), "/opt/other/bin"]))
        self.assertEqual(shutil.which("sbatch"), str(self.slurm / "sbatch"))
        self.assertEqual(rest_sandbox.extra_path(), [str(self.slurm), "/opt/other/bin"])
        # commands the server starts get a base environment without the server's own PATH: the directories too
        env = rest_sandbox.child_env({"PATH": "/usr/bin", "HPC_REST_TOKEN_X": "secret"})
        self.assertEqual(env["PATH"], os.pathsep.join(["/usr/bin", str(self.slurm), "/opt/other/bin"]))
        self.assertNotIn("HPC_REST_TOKEN_X", env)
        # changing the list takes the old ones back, and adds nothing twice
        rest_sandbox.set_extra_path([str(self.slurm)])
        self.assertEqual(os.environ["PATH"], os.pathsep.join([before, str(self.slurm)]))
        rest_sandbox.set_extra_path(())
        self.assertEqual(os.environ["PATH"], before)
        self.assertNotIn(str(self.slurm), rest_sandbox.child_env({"PATH": "/usr/bin"})["PATH"])

    def test_a_child_finds_programs_in_the_directories(self):
        self.assertIsNone(shutil.which("sbatch"))
        rest_sandbox.set_extra_path([str(self.slurm)])
        res = subprocess.run(["sbatch"], capture_output=True, env=rest_sandbox.child_env())   # PATH searched in env
        self.assertEqual(res.returncode, 0)

    def test_check_extra_path(self):
        self.assertEqual(rest_server.check_extra_path(None), [])
        self.assertEqual(rest_server.check_extra_path(["/a/bin", " /b/bin/ ", "/a/bin", "/c/../d"]),
                         ["/a/bin", "/b/bin", "/d"])
        self.assertEqual(rest_server.check_extra_path(["~/bin"]), [os.path.join(os.path.expanduser("~"), "bin")])
        for bad in ("/a/bin", ["relative/bin"], ["/a:/b"], [""], [5], ["/x"] * 33 + ["/y"], [["/a"]]):
            with self.assertRaises(ValueError, msg=bad):
                rest_server.check_extra_path(bad)

    def test_detect_finds_what_a_login_shell_has(self):
        self.assertEqual(rest_server.detect_slurm_path(), [str(self.slurm)])
        # nothing to add once this process can find them itself
        rest_sandbox.set_extra_path([str(self.slurm)])
        self.assertEqual(rest_server.detect_slurm_path(), [])

    def test_detect_with_no_slurm_anywhere(self):
        (self.home / ".bash_profile").write_text("")
        with mock.patch.object(rest_server, "SLURM_DIRS", ()):
            self.assertEqual(rest_server.detect_slurm_path(), [])

    def test_detect_finds_what_only_an_interactive_shell_has(self):
        # a module loaded from ~/.bashrc, which a plain login shell skips (as on Vision)
        (self.home / ".bash_profile").write_text("")
        (self.home / ".bashrc").write_text(f'export PATH="$PATH:{self.slurm}"\n')
        with mock.patch.object(rest_server, "SLURM_DIRS", ()):
            self.assertEqual(rest_server.detect_slurm_path(), [str(self.slurm)])

    def test_detect_looks_in_the_usual_install_directories(self):
        (self.home / ".bash_profile").write_text("")
        pattern = str(self.tmp / "cm" / "*" / "bin")
        with mock.patch.object(rest_server, "SLURM_DIRS", ("/no/such/dir/bin", pattern)):
            self.assertEqual(rest_server.detect_slurm_path(), [str(self.slurm)])
        # the real list names Bright Cluster Manager's directory first
        self.assertEqual(rest_server.SLURM_DIRS[0], "/cm/local/apps/slurm/current/bin")

    def test_detect_answers_can_be_reused_briefly(self):
        self.assertEqual(rest_server.detect_slurm_path(cache=300), [str(self.slurm)])
        (self.home / ".bash_profile").write_text("")
        self.assertEqual(rest_server.detect_slurm_path(cache=300), [str(self.slurm)])     # cached
        self.assertEqual(rest_server.detect_slurm_path(), [])                              # asked again

    def config_file(self, content):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(content))
        config = dict(content, path=str(path))
        return path, config

    def test_startup_saves_it_in_config_json(self):
        path, config = self.config_file({"limits": {"max_cpus": 8}})
        found = rest_server.ensure_slurm_on_path(config)
        dirs, saved, backup = found
        self.assertEqual((dirs, saved), ([str(self.slurm)], str(path)))
        saved_config = json.loads(path.read_text())
        self.assertEqual(saved_config["extra_path"], [str(self.slurm)])
        self.assertEqual(saved_config["limits"], {"max_cpus": 8})                           # the rest is kept
        self.assertEqual(json.loads(Path(backup).read_text()), {"limits": {"max_cpus": 8}})  # and the old file
        self.assertEqual(shutil.which("sbatch"), str(self.slurm / "sbatch"))
        self.assertEqual(config["extra_path"], [str(self.slurm)])
        self.assertIsNone(rest_server.ensure_slurm_on_path(config))                          # nothing more to do

    def test_startup_leaves_other_clusters_alone(self):
        _, config = self.config_file({"scheduler": {"type": "local"}})
        self.assertIsNone(rest_server.ensure_slurm_on_path(config))                          # no SLURM wanted
        (self.home / ".bash_profile").write_text("")
        path, config = self.config_file({})
        self.assertIsNone(rest_server.ensure_slurm_on_path(config))                          # none to be found
        self.assertEqual(json.loads(path.read_text()), {})

    def test_startup_without_a_config_file_still_works_for_this_run(self):
        _, config = self.config_file({})
        config["path"] = str(self.tmp / "absent.json")
        dirs, saved, backup = rest_server.ensure_slurm_on_path(config)
        self.assertEqual((dirs, saved, backup), ([str(self.slurm)], None, None))
        self.assertFalse((self.tmp / "absent.json").exists())                                # nothing created
        self.assertEqual(shutil.which("sbatch"), str(self.slurm / "sbatch"))

    def test_init_config_writes_it(self):
        data = self.tmp / "data"
        (data / "rest").mkdir(parents=True)
        server = Path(rest_server.__file__).resolve()
        env = dict(os.environ, HPCTUNNELS_DATA_DIR=str(data), HOME=str(self.home),
                   PATH=os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]))
        res = subprocess.run([sys.executable, str(server), "--init-config", "--no-sandbox"], capture_output=True,
                             text=True, env=env, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        config = json.loads((data / "rest" / "config.json").read_text())
        self.assertEqual(config["extra_path"], [str(self.slurm)])
        self.assertNotIn("scheduler", config)                                                # SLURM, not local jobs
        self.assertIn("extra PATH", res.stdout)
        # an existing file without the key gets it too, and one with the key is left as it is
        (data / "rest" / "config.json").write_text(json.dumps({"sandbox": {"method": "none"}}))
        res = subprocess.run([sys.executable, str(server), "--init-config", "--no-sandbox"], capture_output=True,
                             text=True, env=env, timeout=120)
        self.assertEqual(json.loads((data / "rest" / "config.json").read_text())["extra_path"], [str(self.slurm)])
        self.assertIn("added SLURM's directory", res.stdout)
        res = subprocess.run([sys.executable, str(server), "--init-config", "--no-sandbox"], capture_output=True,
                             text=True, env=env, timeout=120)
        self.assertIn("kept the existing", res.stdout)


class TestServerModules(LoginShellCase):
    """`server_modules`: a cluster whose SLURM comes from a module (Bright's slurm/25.05) that only a shell can load."""

    def setUp(self):
        super().setUp()
        self.addCleanup(rest_sandbox.load_server_modules, [])
        (self.home / ".bash_profile").write_text(
            f'''module() {{
  case "$1 $2" in
    "load slurm/25.05")
      [ "$FAKE_LOADED" = slurm/25.05 ] && return 0     # already loaded: Lmod does nothing
      export PATH="{self.slurm}:$PATH" SLURM_CONF=/cm/slurm.conf LD_LIBRARY_PATH="/cm/lib:$LD_LIBRARY_PATH" \\
        LOADEDMODULES=slurm/25.05 ;;
    "unload slurm/25.05") return 0 ;;                  # required by DefaultModules: a plain unload keeps it
    "--force purge") unset FAKE_LOADED SLURM_CONF
      PATH=${{PATH//{self.slurm}:/}}; LD_LIBRARY_PATH=${{LD_LIBRARY_PATH//\\/cm\\/lib:/}} ;;
    "-t avail") echo "/cm/local/modulefiles:"; echo "slurm/25.05"; echo "slurm/24.11(default)"; echo "slurm/slurm/25.05" ;;
    *) echo "Lmod has detected the following error:  The following module(s) are unknown: $2" >&2; return 1 ;;
  esac
}}
echo "a message of the day"
''')
        mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/base/lib"}).start()
        self.addCleanup(mock.patch.stopall)

    def test_what_a_module_changes_is_found(self):
        changes = rest_sandbox.resolve_server_modules(["slurm/25.05"])
        self.assertEqual(changes["SLURM_CONF"], ("set", "/cm/slurm.conf"))
        self.assertEqual(changes["PATH"], ("list", [str(self.slurm)], []))
        self.assertEqual(changes["LD_LIBRARY_PATH"], ("list", ["/cm/lib"], []))
        self.assertNotIn("LOADEDMODULES", changes)           # the module system's own bookkeeping
        self.assertEqual(rest_sandbox.resolve_server_modules([]), {})

    def test_status_says_what_the_modules_did(self):
        self.assertEqual(rest_sandbox.module_status(), "no server_modules are set")
        rest_sandbox.load_server_modules(["slurm/25.05"])
        self.assertEqual(rest_sandbox.module_status(),
                         "server_modules slurm/25.05 loaded, changing LD_LIBRARY_PATH, PATH, SLURM_CONF")
        with self.assertRaises(ValueError):
            rest_sandbox.load_server_modules(["slurm/99"])
        self.assertRegex(rest_sandbox.module_status(), "^server_modules did not load: could not load slurm/99")
        rest_sandbox.load_server_modules([])

    def test_a_module_the_environment_says_is_loaded_is_loaded_again(self):
        # a server started over SSH can inherit LOADEDMODULES-like state without the variables that go with it
        with mock.patch.dict(os.environ, {"FAKE_LOADED": "slurm/25.05"}):
            changes = rest_sandbox.resolve_server_modules(["slurm/25.05"])
        self.assertEqual(changes["SLURM_CONF"], ("set", "/cm/slurm.conf"))
        self.assertIn("PATH", changes)

    def test_a_module_the_login_profile_already_loaded_still_counts(self):
        # Vision: ~/.bash_profile (via DefaultModules) loads slurm, so the shell has it before and after
        profile = self.home / ".bash_profile"
        profile.write_text(profile.read_text() + "module load slurm/25.05\n")
        changes = rest_sandbox.resolve_server_modules(["slurm/25.05"])
        self.assertEqual(changes["SLURM_CONF"], ("set", "/cm/slurm.conf"))
        self.assertEqual(changes["PATH"], ("list", [str(self.slurm)], []))

    def test_a_module_that_does_not_load_says_why(self):
        with self.assertRaisesRegex(ValueError, "could not load slurm/99: .*unknown"):
            rest_sandbox.resolve_server_modules(["slurm/99"])

    def test_loaded_for_the_server_and_what_it_starts(self):
        before = os.environ["PATH"]
        self.assertIsNone(shutil.which("sbatch"))
        rest_sandbox.set_extra_path(["/extra/bin"])
        self.assertEqual(rest_sandbox.load_server_modules(["slurm/25.05"]), ["slurm/25.05"])
        self.assertEqual(rest_sandbox.server_modules(), ["slurm/25.05"])
        self.assertEqual(shutil.which("sbatch"), str(self.slurm / "sbatch"))
        self.assertEqual(os.environ["SLURM_CONF"], "/cm/slurm.conf")
        self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/cm/lib:/base/lib")
        self.assertTrue(os.environ["PATH"].endswith("/extra/bin"))
        # commands get the same, on the environment saved from before the Python launcher's modules
        env = rest_sandbox.child_env({"PATH": "/usr/bin"})
        self.assertEqual(env["PATH"], os.pathsep.join([str(self.slurm), "/usr/bin", "/extra/bin"]))
        self.assertEqual(env["SLURM_CONF"], "/cm/slurm.conf")
        res = subprocess.run(["sbatch"], capture_output=True, env=rest_sandbox.child_env())
        self.assertEqual(res.returncode, 0)
        # an empty list takes it all back
        rest_sandbox.load_server_modules([])
        self.assertEqual(os.environ["PATH"], os.pathsep.join([before, "/extra/bin"]))
        self.assertNotIn("SLURM_CONF", os.environ)
        self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/base/lib")
        self.assertNotIn("SLURM_CONF", rest_sandbox.child_env({"PATH": "/usr/bin"}))

    def test_check_module_names(self):
        self.assertEqual(rest_sandbox.check_module_names(None), [])
        self.assertEqual(rest_sandbox.check_module_names([" slurm/25.05 ", "slurm/25.05", "GCC/13.2.0"]),
                         ["slurm/25.05", "GCC/13.2.0"])
        for bad in ("slurm", ["slurm; rm -rf /"], ["--help"], [""], [5], ["m"] * 17, [["slurm"]]):
            with self.assertRaises(ValueError, msg=bad):
                rest_sandbox.check_module_names(bad)

    def test_the_clusters_slurm_modules_newest_first(self):
        self.assertEqual(rest_server.detect_slurm_module(), ["slurm/25.05", "slurm/24.11"])

    def test_startup_loads_slurm_when_it_is_the_only_way(self):
        path, config = self.config_file({"tokens_file": "t"})
        config["scheduler"] = {"type": "slurm"}
        with mock.patch.object(rest_server, "SLURM_DIRS", ()), mock.patch.object(rest_server, "detect_slurm_path",
                                                                                 return_value=[]):
            found = rest_server.ensure_slurm_module(config)
        self.assertEqual(found[0], ["slurm/25.05"])
        self.assertEqual(json.loads(path.read_text())["server_modules"], ["slurm/25.05"])
        self.assertEqual(config["server_modules"], ["slurm/25.05"])
        self.assertTrue(shutil.which("sbatch"))
        self.assertIsNone(rest_server.ensure_slurm_module(config))   # nothing more to do now

    def test_startup_leaves_a_module_that_does_not_help(self):
        (self.home / ".bash_profile").write_text(self.home.joinpath(".bash_profile").read_text().replace(
            'export PATH="%s:$PATH" ' % self.slurm, "export "))
        path, config = self.config_file({"tokens_file": "t"})
        config["scheduler"] = {"type": "slurm"}
        self.assertIsNone(rest_server.ensure_slurm_module(config))
        self.assertNotIn("server_modules", json.loads(path.read_text()))
        self.assertEqual(rest_sandbox.server_modules(), [])

    config_file = TestExtraPath.config_file


class TestExtraPathSettings(JobServerTestCase):
    """PUT/GET /admin/config, as the console's Server settings use them."""

    def setUp(self):
        super().setUp()
        self.addCleanup(rest_sandbox.set_extra_path, ())
        self.path = self.data / "rest" / "config.json"
        self.path.write_text(json.dumps({"tokens_file": str(self.tokens_file)}))
        self.path.chmod(0o600)
        self.server.config_path = str(self.path)

    def test_saved_and_applied_at_once(self):
        tools = self.tmp / "cluster-tools"
        tools.mkdir()
        out = self.owner.update_config({"extra_path": [str(tools)]})
        self.assertEqual(out["config"]["extra_path"], [str(tools)])
        self.assertEqual(json.loads(self.path.read_text())["extra_path"], [str(tools)])
        self.assertIn("extra_path", self.owner.admin_config()["editable"])
        self.assertEqual(self.owner.admin_config()["effective"]["extra_path"], [str(tools)])
        self.assertTrue(os.environ["PATH"].endswith(str(tools)))
        # null puts it back
        out = self.owner.update_config({"extra_path": None})
        self.assertNotIn("extra_path", json.loads(self.path.read_text()))
        self.assertEqual(self.owner.admin_config()["effective"]["extra_path"], [])
        self.assertFalse(os.environ["PATH"].endswith(str(tools)))

    def test_bad_values_are_refused(self):
        before = self.path.read_text()
        for value in ("/a/bin", ["relative"], ["/a:/b"], [3]):
            self.expect_error(422, self.owner.update_config, {"extra_path": value})
        self.assertEqual(self.path.read_text(), before)

    def test_the_console_is_told_when_slurm_cant_be_found(self):
        effective = self.owner.admin_config()["effective"]
        self.assertEqual(effective["slurm_missing"], [])                  # the fake sbatch is on PATH here
        self.assertTrue(effective["slurm_found"])
        with mock.patch.object(rest_server.shutil, "which", return_value=None), \
                mock.patch.object(rest_server, "detect_slurm_path", return_value=["/cm/slurm/bin"]) as detect:
            effective = self.owner.admin_config()["effective"]
        self.assertEqual(effective["slurm_missing"], ["/cm/slurm/bin"])
        self.assertFalse(effective["slurm_found"])
        self.assertEqual(detect.call_args.kwargs, {"cache": 300})        # a login shell isn't started per page view

    def test_server_modules_saved_applied_and_checked(self):
        self.addCleanup(rest_sandbox.load_server_modules, [])
        changes = {"SLURM_CONF": ("set", "/cm/slurm.conf")}
        with mock.patch.object(rest_sandbox, "resolve_server_modules", return_value=changes) as resolve:
            out = self.owner.update_config({"server_modules": ["slurm/25.05"]})
        resolve.assert_called_once_with(["slurm/25.05"])
        self.assertEqual(json.loads(self.path.read_text())["server_modules"], ["slurm/25.05"])
        self.assertEqual(self.owner.admin_config()["effective"]["server_modules"], ["slurm/25.05"])
        self.assertIn("server_modules", self.owner.admin_config()["editable"])
        self.assertEqual(os.environ["SLURM_CONF"], "/cm/slurm.conf")
        # a module that doesn't load is refused, and the file is left alone
        before = self.path.read_text()
        with mock.patch.object(rest_sandbox, "resolve_server_modules", side_effect=ValueError("could not load x/1: no")):
            self.expect_error(422, self.owner.update_config, {"server_modules": ["x/1"]})
        self.expect_error(422, self.owner.update_config, {"server_modules": "slurm"})
        self.assertEqual(self.path.read_text(), before)
        # null puts it back
        self.owner.update_config({"server_modules": None})
        self.assertNotIn("server_modules", json.loads(self.path.read_text()))
        self.assertNotIn("SLURM_CONF", os.environ)

    def test_the_console_is_offered_the_slurm_module(self):
        with mock.patch.object(rest_server, "detect_slurm_module", return_value=["slurm/25.05"]) as detect, \
                mock.patch.object(rest_server.shutil, "which", return_value=None):
            effective = self.owner.admin_config()["effective"]
        self.assertEqual(effective["slurm_modules"], ["slurm/25.05"])
        self.assertEqual(detect.call_args.kwargs, {"cache": 300})


class TestSlurmProblem(unittest.TestCase):
    """cluster_info says why SLURM's commands didn't answer, not only that its lists are empty."""

    def info(self, path):
        import rest_jobs
        with mock.patch.dict(os.environ, {"PATH": path}):
            return rest_jobs.ClusterInfo(rest_jobs.SlurmRunner())._collect()

    def test_a_missing_command(self):
        info = self.info("/nonexistent")
        self.assertIsNone(info["slurm_version"])
        self.assertIn("`sinfo` not found on PATH", info["slurm_problem"])
        self.assertIn("PATH: /nonexistent", info["slurm_problem"])

    def test_a_failing_command(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "sinfo").write_text("#!/bin/sh\necho 'sinfo: error: no slurm.conf' >&2\nexit 1\n")
        (tmp / "sinfo").chmod(0o755)
        info = self.info(str(tmp))
        self.assertIn("`sinfo` exited 1: sinfo: error: no slurm.conf", info["slurm_problem"])

    def test_nothing_to_report_when_it_works(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "sinfo").write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo 'slurm 25.05.0'\nexit 0\n")
        (tmp / "sinfo").chmod(0o755)
        info = self.info(str(tmp))
        self.assertEqual(info["slurm_version"], "slurm 25.05.0")
        self.assertNotIn("slurm_problem", info)
