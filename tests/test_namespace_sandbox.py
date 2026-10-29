"""Native namespace backend regressions. Real kernel checks run separately on Linux."""
import io
import concurrent.futures
import ctypes.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hpclib/servers"))
import rest_bwrap
import rest_sandbox
import sandbox_tools


class TestNamespaceSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ("bwrap", "ch-run"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
        self.work = self.root / "project with spaces"
        self.work.mkdir()
        self.policy = self.root / "policy.bpf"
        self.policy.write_bytes(b"test")
        self.patch = mock.patch.object(rest_bwrap, "seccomp_filter", return_value=str(self.policy))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def sandbox(self, method="bubblewrap", **options):
        config = {"method": method, "runtime": str(self.root / ("bwrap" if method == "bubblewrap" else "ch-run")),
                  "boundary_runtime": str(self.root / "bwrap"), "gpus": "never", **options}
        sandbox = rest_sandbox.Sandbox(config, data_dir=str(self.root / "data"), which=lambda name: None)
        sandbox.srun_supports = lambda *args: True
        return sandbox

    def test_boundary_and_mask_order_with_safe_shell_quoting(self):
        hidden = self.work / "secret"
        hidden.mkdir()
        trusted = self.work / "trusted"
        trusted.mkdir()
        sandbox = self.sandbox().protect(hidden=[str(hidden)], read_only=[str(trusted)])
        lines, plan = sandbox.launch("#!/bin/bash", "printf '%s' 'quoted $HOME `uname`'\nexit 7", [str(self.work)])
        script = "\n".join(lines)
        self.assertEqual(plan["network"], "none")
        self.assertIn("--unshare-net", script)
        self.assertIn("--disable-userns", script)
        self.assertIn("--seccomp 9", script)
        self.assertIn("--remount-ro /", script)
        self.assertIn("trap hpc_sandbox_cleanup EXIT", script)
        self.assertIn("SSH_AUTH_SOCK", script)
        self.assertGreater(script.index(str(trusted)), script.index(str(self.work)))
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode, 0)

    def test_managed_install_precedes_path_with_absolute_override(self):
        managed = self.root / "managed"
        managed.mkdir()
        binary = managed / "bwrap"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        sandbox = self.sandbox()
        with mock.patch.object(rest_sandbox, "MANAGED_BIN", str(managed)):
            self.assertEqual(sandbox._find(["bwrap"]), str(binary))
            self.assertEqual(sandbox._find([str(self.root / "bwrap")]), str(self.root / "bwrap"))

    def test_extra_path_discovers_both_runtimes_and_reaches_child_environment(self):
        previous = rest_sandbox.extra_path()
        with mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(rest_sandbox, "MANAGED_BIN", str(self.root / "not-installed")):
            try:
                rest_sandbox.set_extra_path([str(self.root)])
                for method, binary in (("bubblewrap", "bwrap"), ("charliecloud", "ch-run")):
                    sandbox = rest_sandbox.Sandbox({"method": method}, data_dir=str(self.root / "data"))
                    self.assertEqual(sandbox._locate(), (method, str(self.root / binary)))
                environment = rest_sandbox.child_env({"PATH": "", "HPC_REST_TOKEN": "synthetic"})
                self.assertEqual(environment["PATH"], str(self.root))
                self.assertNotIn("HPC_REST_TOKEN", environment)
            finally:
                rest_sandbox.set_extra_path(previous)

    def test_charliecloud_requires_boundary_and_uses_locked_sources(self):
        sandbox = self.sandbox("charliecloud")
        lines, plan = sandbox.launch("#!/bin/bash", "true", [str(self.work)])
        script = "\n".join(lines)
        self.assertEqual(plan["boundary_runtime"], str(self.root / "bwrap"))
        self.assertIn("/.hpclib/ch-run --no-passwd", script)
        self.assertIn("--unshare-net", script)
        self.assertNotIn("--disable-userns", script)
        (self.root / "bwrap").unlink()
        with self.assertRaisesRegex(rest_sandbox.SandboxError, "requires Bubblewrap"):
            sandbox.resolve()

    def test_policy_failure_never_falls_back(self):
        with mock.patch.object(rest_bwrap, "seccomp_filter", side_effect=rest_bwrap.BoundaryError("missing libseccomp")):
            with self.assertRaisesRegex(rest_sandbox.SandboxError, "missing libseccomp"):
                self.sandbox(allow_unsandboxed=True).launch("#!/bin/bash", "true", [str(self.work)])

    def test_runtime_in_writable_scope_is_refused(self):
        with self.assertRaisesRegex(rest_sandbox.SandboxError, "agent-writable"):
            self.sandbox().launch("#!/bin/bash", "true", [str(self.root)])

    def test_custom_charliecloud_outer_account_and_image_format(self):
        image = self.root / "image"
        image.mkdir()
        lines, _ = self.sandbox("charliecloud", image=str(image)).launch("#!/bin/sh", "true", [str(self.work)])
        self.assertIn('hpc_bwrap+=("${hpc_sandbox_account[@]}")', lines)
        invalid = self.root / "image.sif"
        invalid.write_bytes(b"not raw SquashFS")
        with self.assertRaisesRegex(rest_sandbox.SandboxError, "raw SquashFS"):
            self.sandbox(image=str(invalid)).resolve()

    def test_explicit_network_for_service_and_sync(self):
        sandbox = self.sandbox()
        for network in (True, "host"):
            with self.subTest(network=network):
                lines, plan = sandbox.launch("#!/bin/bash", "true", [str(self.work)], network=network)
                self.assertNotIn("--unshare-net", "\n".join(lines))
                self.assertFalse(plan["network_isolated"])

    def test_no_unsafe_flags_or_implicit_gpu_devices(self):
        with self.assertRaisesRegex(ValueError, "flags"):
            self.sandbox(flags=["--bind", "/", "/"])
        with self.assertRaisesRegex(rest_sandbox.SandboxError, "explicit devices"):
            self.sandbox(gpus="auto").launch("#!/bin/bash", "true", [str(self.work)], gpus=1)
        with self.assertRaisesRegex(ValueError, "individual paths"):
            self.sandbox(devices=["/dev/../etc/passwd"])

    def test_environment_images_use_private_read_only_fuse_mount(self):
        image = self.work / "venv.sqfs"
        image.write_bytes(b"test SquashFS")
        target = self.work / ".venv"
        target.mkdir()
        lines, plan = self.sandbox().launch("#!/bin/bash", "true", [str(self.work)], images=[(str(image), str(target))])
        script = "\n".join(lines)
        self.assertIn('"$hpc_sandbox_fuse" -o ro', script)
        self.assertIn('--ro-bind "$hpc_sandbox_tmp/img/0"', script)
        self.assertIn("fusermount3 -u -z", script)
        self.assertEqual(plan["images"], [[str(image), str(target)]])


class TestSandboxInstaller(unittest.TestCase):
    def test_source_patch_requires_reviewed_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "bubblewrap.c").write_text("a different upstream release")
            with self.assertRaisesRegex(RuntimeError, "reviewed"):
                sandbox_tools.patch_readonly_destinations(source)
    def test_download_checksum_failure_does_not_extract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(sandbox_tools.urllib.request, "urlopen", return_value=io.BytesIO(b"modified download")):
                with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
                    sandbox_tools.unpack("bubblewrap", root, root)
            self.assertFalse((root / "bubblewrap").exists())

    def test_blocked_namespaces_do_not_build_or_publish(self):
        info = {"namespaces": {"ok": False, "detail": "site restriction"}}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(sandbox_tools.platform, "system", return_value="Linux"), \
                mock.patch.object(sandbox_tools, "report", return_value=info), mock.patch.object(sandbox_tools, "_bubblewrap") as build:
            with self.assertRaisesRegex(RuntimeError, "Namespace creation is blocked"):
                sandbox_tools.install("charliecloud", Path(temporary))
            build.assert_not_called()
            self.assertFalse((Path(temporary) / ".install-lock").exists())

    def test_unsafe_tool_and_concurrent_install(self):
        with self.assertRaises(ValueError):
            sandbox_tools.install("arbitrary shell")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(sandbox_tools.platform, "system", return_value="Linux"):
            (Path(temporary) / ".install-lock").mkdir()
            with self.assertRaisesRegex(RuntimeError, "already running"):
                sandbox_tools.install("bubblewrap", Path(temporary))


@unittest.skipUnless(sys.platform == "linux" and ctypes.util.find_library("seccomp"), "requires Linux libseccomp")
class TestNativePolicy(unittest.TestCase):
    def test_concurrent_publication_and_tampered_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                paths = list(pool.map(lambda _: rest_bwrap.seccomp_filter(temporary), range(16)))
            self.assertEqual(len(set(paths)), 1)
            policy = Path(paths[0])
            self.assertGreater(policy.stat().st_size, 0)
            self.assertEqual(list(Path(temporary).iterdir()), [policy])
            policy.write_bytes(b"modified")
            with self.assertRaisesRegex(rest_bwrap.BoundaryError, "modified"):
                rest_bwrap.seccomp_filter(temporary)


if __name__ == "__main__":
    unittest.main()
