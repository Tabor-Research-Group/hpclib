"""Regression cases for host KCM tickets and an isolated data-transfer image.

No KDC, SMB server, real password, or real credential cache is used. The fake
image rejects implicit cache/config visibility, just like the reported failure.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TUNNEL = ROOT / "hpclib" / "tunnels" / "data-transfer"
spec = importlib.util.spec_from_file_location("transfer_kerberos_config", TUNNEL / "kerberos_config.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)

from smbclient_fixture import FAKE_TOOL


class TestSmbshellKerberos(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tunnel = self.root / "app" / "data-transfer"
        self.tunnel.mkdir(parents=True)
        for name in ("smbshell.sh", "settings.sh", "kerberos_config.py", "smbclient_transfer.py", "install.sh", "tunnel_config.sh"):
            shutil.copyfile(TUNNEL / name, self.tunnel / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("kinit", "klist", "kvno", "kdestroy", "singularity", "apptainer", "stat"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            path.chmod(0o700)
        (self.bin / "python3").symlink_to(sys.executable)
        self.cache = self.root / "custom cache outside home" / "ticket"
        self.image = self.root / "tools.sif"
        self.image.touch()
        self.conf = self.root / "host-krb5.conf"
        self.fragment = self.root / "realm.conf"
        self.fragment.write_text("[realms]\n AUTH.EXAMPLE.EDU = {\n kdc = kdc.fixture.invalid\n }\n"
                                 "[domain_realm]\n .example.edu = AUTH.EXAMPLE.EDU\n")
        self.conf.write_text(f"include {self.fragment}\n[libdefaults]\n default_realm = AUTH.EXAMPLE.EDU\n"
                             " dns_canonicalize_hostname = fallback\n")
        self.smb = self.root / "smb"
        (self.smb / "research" / "our_group").mkdir(parents=True)
        (self.smb / "research" / "our_group" / "fixture.dat").write_text("DATA")
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.events = self.root / "events.jsonl"
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.root / "empty-home"),
                        HPCTUNNELS_DATA_DIR=str(self.root / "data"), KRB5CCNAME="KCM:",
                        KRB5_CONFIG=str(self.conf), SMB_KRB5CCNAME=str(self.cache),
                        SMB_IMAGE=str(self.image), SMB_HOST="files.example.edu", SMB_USER="me",
                        SMB_ROOT="research/our_group", SMB_REALM="AUTH.EXAMPLE.EDU", SMB_AUTH="kerberos",
                        SMB_CREDENTIALS=str(self.root / "password"), SMB_NONINTERACTIVE="1",
                        SMB_KRB5_CONF="", FAKE_EVENTS=str(self.events), FAKE_SMB=str(self.smb), XDG_RUNTIME_DIR=str(self.memory))

    def run_shell(self, *args, **overrides):
        return subprocess.run(["bash", str(self.tunnel / "smbshell.sh"), *args],
                              env=dict(self.env, **overrides), text=True, capture_output=True, timeout=20)

    def ticket(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text('{"valid": true}')
        self.cache.chmod(0o600)

    def records(self, kind=None):
        rows = [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []
        return [row for row in rows if row["kind"] == kind] if kind else rows

    def test_host_kcm_ticket_is_not_reported_as_a_transfer_ticket(self):
        out = self.run_shell("status", "--json", "--brief")
        self.assertEqual(out.returncode, 0, out.stderr)
        status = json.loads(out.stdout.split("HPCLIB_SMB_STATUS ", 1)[1])
        self.assertIsNone(status["ticket"])
        self.assertEqual(status["host_ticket"]["cache"], "KCM:")
        self.assertEqual(status["ticket_cache"], "FILE")
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.records("image"), [])  # status --brief never starts an image

    def test_login_uses_a_private_file_without_changing_the_host_kcm_cache(self):
        out = self.run_shell("login")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(self.cache.read_text())["valid"])
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        init = self.records("kinit")[0]
        self.assertTrue(init["cache"].startswith("FILE:"))
        self.assertEqual(init["config"], str(self.conf))
        self.assertIn("me@AUTH.EXAMPLE.EDU", init["args"])
        self.assertEqual(list(self.cache.parent.glob(".krb5-login.*")), [])

    def test_failed_login_preserves_the_working_ticket(self):
        self.ticket()
        original = self.cache.read_bytes()
        out = self.run_shell("login", FAKE_KINIT_FAIL="1")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertIn("previous transfer ticket was kept", out.stderr)
        self.assertEqual(list(self.cache.parent.glob(".krb5-login.*")), [])

    def test_listing_and_shell_override_baked_kcm_and_mount_custom_cache(self):
        self.ticket()
        for command in (("ls", "", "--json"), ("shell",)):
            with self.subTest(command=command):
                out = self.run_shell(*command, SMB_KRB5CCNAME="FILE:" + str(self.cache),
                                     RCLONE_CONFIG_SMB_PASS="stale-password")
                self.assertEqual(out.returncode, 0, out.stderr)
        for image in self.records("image"):
            self.assertEqual(image["inside_cache"], "FILE:" + str(self.cache))
            self.assertIsNone(image["kerberos"])
            self.assertFalse(image["has_password"])
            self.assertIn("dns_canonicalize_hostname = false", image["profile"])

    def test_check_and_agent_listing_accept_sssd_final_subsections(self):
        self.ticket()
        self.fragment.write_text(self.fragment.read_text() +
            "[plugins]\n localauth = {\n module = sssd:/usr/lib64/sssd/modules/sssd_krb5_localauth_plugin.so\n }*\n")
        for command in (("check",), ("ls", "", "--json")):
            out = self.run_shell(*command)
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("localauth = {", self.records("image")[0]["profile"])

    def test_profile_failure_exposes_actual_cause_on_the_console_error_line(self):
        self.ticket()
        self.conf.write_text(f"include {self.root / 'missing-site-policy'}\n")
        out = self.run_shell("check")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("missing-site-policy", out.stderr.strip().splitlines()[-1])
        self.assertEqual(self.records("image"), [])

    def test_ambiguous_final_crypto_policy_fails_without_replacing_profile(self):
        generated = self.root / "generated.conf"
        generated.write_text("previous policy\n")
        for text in (
            "[libdefaults]*\n permitted_enctypes = aes256-cts\n[libdefaults]\n permitted_enctypes = rc4-hmac\n",
            "[realms]\n AUTH.EXAMPLE.EDU = {\n kdc = first\n }*\n AUTH.EXAMPLE.EDU = {\n kdc = second\n }\n",
            "[libdefaults]\n permitted_enctypes* = aes256-cts\n permitted_enctypes = rc4-hmac\n",
            "[libdefaults]\n permitted_enctypes = aes256-cts\n permitted_enctypes* = rc4-hmac\n",
        ):
            self.conf.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(profile.ProfileError, "repeated final"):
                profile.prepare([self.conf], generated, str(self.cache))
            self.assertEqual(generated.read_text(), "previous policy\n")

    def test_unique_final_policy_and_literal_asterisk_values_are_preserved(self):
        self.conf.write_text("[libdefaults]*\n permitted_enctypes* = aes256-cts aes128-cts\n"
                             "[plugins]\n localauth* = {\n module = sssd:/lib/sssd.so\n }\n"
                             "[appdefaults]\n host_based_services = *\n")
        generated = self.root / "generated.conf"
        profile.prepare([self.conf], generated, str(self.cache))
        self.assertIn("permitted_enctypes = aes256-cts aes128-cts", generated.read_text())
        self.assertIn("host_based_services = *", generated.read_text())

    def test_host_only_kcm_never_satisfies_kerberos_mode_or_falls_back_to_password(self):
        Path(self.env["SMB_CREDENTIALS"]).write_text("pass=encoded-password\n")
        out = self.run_shell("ls", "", "--json")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no transfer ticket", out.stderr)
        self.assertEqual(self.records("image"), [])

    def test_apptainer_receives_the_same_explicit_overrides(self):
        self.ticket()
        (self.bin / "singularity").unlink()
        out = self.run_shell("ls", "", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.records("image")[0]["inside_cache"], "FILE:" + str(self.cache))

    def run_gui(self, stop=False, **overrides):
        import pty, select, time
        master, slave = pty.openpty()
        env = dict(self.env, SMB_NONINTERACTIVE="", FAKE_EXPECT_PASSWORD="synthetic-password", **overrides)
        proc = subprocess.Popen(["bash", str(self.tunnel / "smbshell.sh"), "gui", "--port", "27555"],
                                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        os.close(slave)
        error = b""
        try:
            deadline = time.monotonic() + 8
            while b"GUI session and transfer sign-in): " not in error:
                if time.monotonic() > deadline:
                    self.fail("GUI did not ask for a password: " + error.decode())
                ready, _, _ = select.select([proc.stderr], [], [], 0.2)
                if ready:
                    chunk = os.read(proc.stderr.fileno(), 4096)
                    if not chunk:
                        break
                    error += chunk
            os.write(master, b"synthetic-password\n")
            if stop:
                deadline = time.monotonic() + 8
                while not self.records("gui"):
                    if time.monotonic() > deadline:
                        self.fail("GUI did not start")
                    time.sleep(0.05)
                proc.terminate()
            out, err = proc.communicate(timeout=15)
            return subprocess.CompletedProcess(proc.args, proc.returncode, out.decode(), (error + err).decode())
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            os.close(master)

    def test_gui_asks_once_creates_ticket_and_cleans_session_password(self):
        out = self.run_gui(RCLONE_SMB_USE_KERBEROS="true", RCLONE_CONFIG_SMB_SERVER_PASS="stale-password")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.cache.is_file())
        self.assertIn("HPCLIB_TRANSFER_GUI", out.stdout)
        self.assertIn('"transfer_backend": "smbclient"', out.stdout)
        self.assertNotIn("synthetic-password", out.stdout + out.stderr)
        self.assertEqual(len(self.records("kinit")), 1)
        record = self.records("gui")[0]
        self.assertFalse(record["password_in_argv"])
        self.assertFalse(Path(record["config_path"]).exists())
        self.assertFalse(Path(self.env["SMB_CREDENTIALS"]).exists())
        self.assertEqual(list(self.memory.iterdir()), [])

    def test_stopping_gui_removes_session_config_but_keeps_ticket(self):
        out = self.run_gui(stop=True, FAKE_GUI_WAIT="1")
        self.assertNotEqual(out.returncode, 0)  # explicitly terminated
        self.assertTrue(self.cache.is_file())
        self.assertFalse(Path(self.records("gui")[0]["config_path"]).exists())
        self.assertEqual(list(self.memory.iterdir()), [])

    def test_custom_gui_receives_session_password_configuration(self):
        custom = self.root / "custom.gui.sh"
        custom.write_text('grep -q "use_kerberos = false" "$RCLONE_CONFIG" || exit 1\n'
                          'echo "HPCLIB_TRANSFER_GUI port=$HPCLIB_GUI_PORT"\n')
        out = self.run_gui(SMB_GUI=str(custom))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HPCLIB_TRANSFER_GUI", out.stdout)
        self.assertTrue(self.cache.exists())
        self.assertEqual(list(self.memory.iterdir()), [])

    def test_gui_failed_kinit_preserves_ticket_and_never_starts_rclone(self):
        self.ticket()
        original = self.cache.read_bytes()
        out = self.run_gui(FAKE_KINIT_FAIL="1")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(self.records("gui"), [])
        self.assertEqual(list(self.memory.iterdir()), [])

    def test_gui_never_falls_back_to_a_disk_session_directory(self):
        (self.bin / "stat").write_text("#!/bin/bash\necho diskfs\n")
        out = self.run_shell("gui", "--port", "27555", SMB_NONINTERACTIVE="")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("memory filesystem", out.stderr)
        self.assertEqual(self.records("kinit"), [])

    def test_install_checks_samba_independently_of_the_optional_gui(self):
        for changes, expected, code in (({}, 'optional GUI: rclone fixture', 0),
                                       ({'FAKE_NO_RCLONE': '1'}, 'optional GUI: unavailable', 0),
                                       ({'FAKE_NO_SMB': '1'}, 'has no smbclient', 1)):
            with self.subTest(changes=changes):
                out = subprocess.run(['bash', str(self.tunnel / 'install.sh'), '--check'],
                                     env=dict(self.env, **changes), text=True, capture_output=True)
                self.assertEqual(out.returncode, code, out.stderr)
                self.assertIn(expected, out.stdout)

    def test_missing_gui_binary_fails_before_password_or_kinit(self):
        out = self.run_shell('gui', '--port', '27555', FAKE_NO_RCLONE='1')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('optional GUI needs rclone', out.stderr)
        self.assertEqual(self.records('kinit'), [])

    def test_submit_manifest_requires_ticket_and_passes_native_job_options(self):
        self.ticket()
        sbatch = self.bin / 'sbatch'
        sbatch.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
                          'json.dump(sys.argv[1:], open(os.environ["FAKE_BATCH"], "w"))\nprint("9001")\n')
        sbatch.chmod(0o700)
        batch = self.root / 'batch.json'
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps([['get', 'research/our_group', str(self.root/'copy')],
                                        ['put', str(self.root/'result'), 'research/out']]))
        out = self.run_shell('submit', '--time=1:00:00', '--manifest', str(manifest), FAKE_BATCH=str(batch))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '9001')
        options = json.loads(batch.read_text())
        self.assertIn('--time=1:00:00', options)
        self.assertIn('--array=0-1', options)
        lines = Path(options[-1]).read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])[0], 'get')

    def test_old_saved_password_cannot_submit_a_job_without_ticket(self):
        Path(self.env['SMB_CREDENTIALS']).write_text('pass=old-password\n')
        out = self.run_shell('submit', 'get', 'research/our_group', str(self.root/'copy'))
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('needs a transfer ticket', out.stderr)

    def test_connection_check_makes_one_attempt_at_the_configured_folder(self):
        self.ticket()
        out = self.run_shell("check")
        self.assertEqual(out.returncode, 0, out.stderr)
        images = self.records("image")
        self.assertEqual(len(images), 1)
        self.assertIn("//files.example.edu/research", images[0]["command"])
        self.assertIn("Connection verified", out.stdout)

    def test_blank_realm_uses_transfer_ticket_realm_in_samba(self):
        self.ticket()
        out = self.run_shell("check", SMB_REALM="")
        self.assertEqual(out.returncode, 0, out.stderr)
        command = self.records("image")[0]["command"]
        self.assertIn("--realm=AUTH.EXAMPLE.EDU", command)

    def test_blank_settings_discover_short_identity_and_keep_json_listing_clean(self):
        self.ticket()
        # Both DNS and SMB are synthetic, including the subprocess transport.
        dns = self.root / "fake-dns"
        dns.mkdir()
        (dns / "sitecustomize.py").write_text("import socket\n"
            "socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 445))]\n")
        for command in (("ls", "", "--json"), ("check",)):
            with self.subTest(command=command):
                out = self.run_shell(*command, SMB_REALM="", SMB_SPN="",
                                     FAKE_SMB_FQDN_UNKNOWN="1", PYTHONPATH=str(dns))
                self.assertEqual(out.returncode, 0, out.stderr)
                if command[0] == "ls": self.assertEqual(json.loads(out.stdout)[0]["Name"], "fixture.dat")
                else: self.assertIn("HPCLIB_SMB_CHECK OK", out.stdout)
                first, second = self.records("image")[-2:]
                self.assertIn("//files.example.edu/research", first["command"])
                self.assertIn("//files/research", second["command"])
                self.assertIn("--ip-address=127.0.0.1", second["command"])
                self.assertIn("--realm=AUTH.EXAMPLE.EDU", second["command"])

    def test_connection_check_keeps_underlying_error_in_its_last_line(self):
        self.ticket()
        out = self.run_shell("check", FAKE_SMB_ERROR="session setup failed: NT_STATUS_INVALID_PARAMETER")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("NT_STATUS_INVALID_PARAMETER", out.stderr.strip().splitlines()[-1])
        self.assertIn("cifs/files.example.edu@AUTH.EXAMPLE.EDU", out.stderr)
        self.assertEqual(len(self.records("image")), 1)

    def test_local_credential_error_stops_before_a_copy(self):
        self.ticket()
        out = self.run_shell("get", "folder", str(self.root / "destination"),
                             FAKE_SMB_ERROR="unsupported KRB5CCNAME: KCM:")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("unsupported KRB5CCNAME", out.stderr)
        self.assertNotIn("server refused", out.stderr)
        self.assertEqual(len(self.records("image")), 1)
        self.assertFalse((self.root / "destination").exists())

    def test_find_spn_does_not_call_a_local_cache_error_an_unknown_spn(self):
        self.ticket()
        out = self.run_shell("find-spn", SMB_HOST="localhost", FAKE_KVNO_ERROR="kvno: No KCM server found")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("No KCM server found", out.stderr)
        self.assertNotIn("not known:", out.stdout)

    def test_old_password_setting_never_satisfies_a_cli_signin(self):
        Path(self.env["SMB_CREDENTIALS"]).write_text("pass=encoded-password\n")
        out = self.run_shell("ls", "", "--json", SMB_AUTH="password", RCLONE_CONFIG_SMB_USE_KERBEROS="true")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no transfer ticket", out.stderr)
        self.assertEqual(self.records("image"), [])
        state = self.run_shell("status", "--json", "--brief")
        status = json.loads(state.stdout.split("HPCLIB_SMB_STATUS ")[1])
        self.assertFalse(status["credentials"])
        self.assertTrue(status["saved_credentials"])

    def test_saving_a_password_is_retired(self):
        out = self.run_shell("save-credentials")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no longer saved", out.stderr)
        self.assertFalse(Path(self.env["SMB_CREDENTIALS"]).exists())


class TestKerberosProfile(unittest.TestCase):
    def test_includes_merge_realms_and_preserve_explicit_dns_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragments = root / "fragments"
            fragments.mkdir()
            (fragments / "10-site.conf").write_text("[libdefaults]\n dns_lookup_kdc = false\n"
                                                   "[realms]\n A = {\n kdc = first.invalid\n }\n")
            (fragments / "20-site").write_text("[realms]\n A = {\n kdc = second.invalid\n }\n"
                                              " B = {\n kdc = third.invalid\n }\n")
            (fragments / ".ignored.conf").write_text("invalid")
            (fragments / "ignored.conf~").write_text("invalid")
            source = root / "krb5.conf"
            source.write_text(f"includedir {fragments}\n[libdefaults]\n dns_lookup_kdc = true\n"
                               " dns_canonicalize_hostname = fallback # MIT value\n"
                               "[domain_realm]\n .example = A\n")
            output = root / "container.conf"
            profile.prepare([source], output, "/private/ticket", "A")
            text = output.read_text()
            self.assertEqual(text.count("[realms]"), 1)
            self.assertEqual(text.count("A = {"), 1)
            for value in ("first.invalid", "second.invalid", "third.invalid", ".example = A"):
                self.assertIn(value, text)
            self.assertIn("dns_lookup_kdc = false", text)
            self.assertNotIn("dns_lookup_kdc = true", text)
            self.assertIn("default_ccache_name = FILE:/private/ticket", text)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_missing_include_and_cycles_leave_the_previous_profile_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root / "source.conf", root / "out.conf"
            output.write_text("previous profile")
            for content in (f"include {root / 'missing.conf'}\n", f"include {source}\n"):
                source.write_text(content)
                with self.assertRaises((OSError, profile.ProfileError)):
                    profile.prepare([source], output, "/private/ticket")
                self.assertEqual(output.read_text(), "previous profile")


if __name__ == "__main__":
    unittest.main()
