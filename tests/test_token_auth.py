"""Local synthetic tests: no cluster, SSH, real grants or credentials."""
import ast
import concurrent.futures
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hpclib/servers"))
from token_auth import (TokenAuth, TokenRegistry, ServiceToken, CapabilityTickets, ResourceSigner)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "tokens.json")
        self.now = 1000
        self.registry = TokenRegistry(self.path, clock=lambda: self.now)
        self.root = str(Path(self.tmp.name).resolve() / "work")
        self.grant = self.registry.create_grant("research", ["read", "submit"], [self.root])
        self.session = self.registry.issue(self.grant["id"], ttl=60, lease=120, device_id="laptop")

    def identify(self, issued=None):
        return self.registry.identify((issued or self.session)["token"])

    def test_identity_is_stable_and_secret_is_hashed(self):
        who = self.identify()
        self.assertEqual((who.name, who.grant_id, who.device_id), ("research", self.grant["id"], "laptop"))
        self.assertNotIn(self.session["token"], Path(self.path).read_text())
        self.assertNotIn("sha256", json.dumps(self.registry.list()))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_expiry_and_relogin_issue(self):
        self.now += 60
        self.assertIsNone(self.identify())
        with self.assertRaises(PermissionError):
            self.registry.rotate(self.session["token"])
        fresh = self.registry.issue(self.grant["id"])
        self.assertEqual(self.identify(fresh).name, "research")

    def test_rotation_retains_principal_and_session_id(self):
        self.now += 50
        fresh = self.registry.rotate(self.session["token"], ttl=60)
        self.assertIsNone(self.identify())
        self.assertEqual(self.identify(fresh).session_id, self.session["session"]["id"])
        self.now += 59
        self.assertIsNotNone(self.identify(fresh))
        last = self.registry.rotate(fresh["token"], ttl=60)
        self.assertEqual(last["session"]["expires"], 1120)

    def test_revoke_grant_and_no_name_reuse(self):
        self.registry.revoke(self.grant["id"])
        self.assertIsNone(self.identify())
        with self.assertRaises(ValueError):
            self.registry.issue(self.grant["id"])
        with self.assertRaises(ValueError):
            self.registry.create_grant("research", ["read"], [self.root])

    def test_delegate_and_revoke_parent(self):
        child = self.registry.delegate(self.session["token"], scopes=["read"], allow=[self.root + "/sub"])
        self.assertEqual(self.identify(child).scopes, ("read",))
        self.assertEqual(child["session"]["expires"], 1060)
        self.assertEqual(self.identify(child).device_id, "laptop")
        self.registry.revoke_self(self.session["token"])
        self.assertIsNone(self.identify(child))
        with self.assertRaises(PermissionError):
            self.registry.rotate(child["token"])

    def test_sibling_survives_logout_and_grant_revocation_kills_all(self):
        sibling = self.registry.issue(self.grant["id"])
        self.registry.revoke_self(self.session["token"])
        self.assertIsNotNone(self.identify(sibling))
        self.registry.revoke(self.grant["id"])
        self.assertIsNone(self.identify(sibling))

    def test_scope_and_path_widening_refused(self):
        for args in ({"scopes": ["files:write"]}, {"allow": [self.root + "-other"]},
                     {"allow": [self.root + "/../outside"]}, {"scopes": ["*"]}, {"scopes": ["slurm"]}):
            with self.subTest(args=args), self.assertRaises((PermissionError, ValueError)):
                self.registry.delegate(self.session["token"], **args)

    def test_symlink_widening_refused(self):
        Path(self.root).mkdir()
        Path(self.root, "escape").symlink_to(Path(self.tmp.name) / "elsewhere")
        with self.assertRaises(PermissionError):
            self.registry.delegate(self.session["token"], allow=[self.root + "/escape"])

    def test_changed_symlink_does_not_expand_existing_grant(self):
        Path(self.root).mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.root)
        grant = self.registry.create_grant("linked", ["read"], [str(link)])
        issued = self.registry.issue(grant["id"])
        link.unlink()
        link.symlink_to(Path(self.tmp.name) / "elsewhere")
        self.assertEqual(self.identify(issued).allow, [self.root])

    def test_granted_root_replaced_by_symlink_fails_closed(self):
        Path(self.root).mkdir()
        Path(self.root).rmdir()
        Path(self.root).symlink_to(Path(self.tmp.name) / "outside")
        self.assertIsNone(self.identify())

    def test_grant_update_narrows_all_sessions_without_reissuing(self):
        self.registry.update_grant(self.grant["id"], ["read"], [self.root + "/sub"])
        self.assertEqual(self.identify().scopes, ("read",))
        self.assertEqual(self.identify().allow, [self.root + "/sub"])
        # A subsequent grant expansion cannot expand the originally issued cap.
        self.registry.update_grant(self.grant["id"], ["read", "files:write"], [str(Path(self.root).parent)])
        self.assertEqual(self.identify().scopes, ("read",))
        self.assertEqual(self.identify().allow, [self.root])

    def test_wrong_audience(self):
        self.assertIsNone(TokenRegistry(self.path, audience="hpclib:workspace", clock=lambda: self.now).identify(self.session["token"]))

    def test_parent_cycle_fails_closed(self):
        with self.registry.edit() as records:
            records[-1]["parent_id"] = records[-1]["id"]
        self.assertIsNone(self.identify())

    def test_bounded_delegation(self):
        parent = self.session
        for _ in range(self.registry.MAX_DEPTH - 1):
            parent = self.registry.delegate(parent["token"])
            self.assertIsNotNone(self.identify(parent))
        with self.assertRaises(ValueError):
            self.registry.delegate(parent["token"])

    def test_malformed_expiry_fails_closed(self):
        for value in (float("inf"), float("nan"), "tomorrow", True):
            with self.registry.edit() as records:
                records[-1]["expires"] = value
            self.assertIsNone(self.identify())

    def test_session_issuance_is_bounded(self):
        self.registry.MAX_ACTIVE_PER_GRANT = 1
        with self.assertRaises(ValueError):
            self.registry.delegate(self.session["token"])
        self.registry.revoke_self(self.session["token"])
        self.assertTrue(self.registry.issue(self.grant["id"]))
        self.registry.MAX_RECORDS = 3
        with self.assertRaises(ValueError):
            self.registry.issue(self.grant["id"])

    def test_invalid_durations(self):
        for value in (float("inf"), float("nan"), True, -1, 0, "60", 90000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry.issue(self.grant["id"], ttl=value)

    def test_legacy_verifiers_reject_new_records(self):
        for record in self.registry.records():
            self.assertFalse(record.get("scopes"), "old verifiers ignore kind/expiry, so sessions must omit top-level scopes")
        self.assertFalse(any(TokenAuth.valid_entry(r) for r in self.registry.records()))
        legacy = TokenAuth.add_token(self.path, "legacy", ["read"], [self.root])
        auth = TokenAuth("owner-secret", tokens_file=self.path)
        self.assertEqual(auth.identify("Bearer " + legacy).name, "legacy")
        self.assertEqual(len(self.registry.list()), 2)
        TokenAuth.revoke_token(self.path, "legacy")
        self.assertIsNone(auth.identify("Bearer " + legacy))
        self.assertEqual(len(self.registry.list()), 2)

    def test_rest_accepts_live_sessions_but_not_expired_sessions(self):
        live_registry = TokenRegistry(self.path)
        issued = live_registry.issue(self.grant["id"])
        auth = TokenAuth("owner-secret", tokens_file=self.path)
        self.assertEqual(auth.identify("Bearer " + issued["token"]).name, "research")
        live_registry.revoke(issued["session"]["id"])
        self.assertIsNone(auth.identify("Bearer " + issued["token"]))
        self.assertEqual(auth.identify("Bearer owner-secret").name, "owner")
        expired = TokenRegistry(self.path, clock=lambda: time.time() - 10000).issue(self.grant["id"], ttl=10)
        self.assertIsNone(auth.identify("Bearer " + expired["token"]))

    def test_legacy_scope_upgrade_cannot_convert_session_to_legacy(self):
        record = self.registry.records()[-1]
        with self.assertRaises(ValueError):
            TokenAuth.add_scopes_by_hash(self.path, record["sha256"], ["files:write"])
        self.assertNotIn("scopes", self.registry.records()[-1])

    def test_legacy_revoke_grant_leaves_tombstone(self):
        TokenAuth.revoke_token(self.path, "research")
        self.assertIsNone(self.identify())
        with self.assertRaises(ValueError):
            self.registry.create_grant("research", ["read"], [self.root])

    def test_concurrent_legacy_and_session_issuance(self):
        def issue(i):
            if i % 2:
                return TokenAuth.add_token(self.path, "legacy-{}".format(i), ["read"], [self.root])
            return self.registry.issue(self.grant["id"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(issue, range(24)))
        self.assertEqual(len(self.registry.records()), 26)

    def test_registry_rejects_symlink_and_public_files(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(PermissionError):
            self.registry.records()
        os.chmod(self.path, 0o600)
        link = Path(self.tmp.name) / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaises(PermissionError):
            TokenRegistry(link).records()


class CapabilitiesTests(unittest.TestCase):
    def test_service_audience_separation(self):
        auth = ServiceToken("secret", "workspace")
        self.assertTrue(auth.check("secret", "workspace"))
        self.assertFalse(auth.check("secret", "rest"))
        self.assertFalse(auth.check("wrong", "workspace"))

    def test_single_use_purpose_resource_and_expiry(self):
        now = [1000]
        tickets = CapabilityTickets("workspace", clock=lambda: now[0], limit=2)
        ticket = tickets.issue("terminal", "shell", 10)
        self.assertTrue(tickets.redeem(ticket, "terminal", "shell"))
        self.assertFalse(tickets.redeem(ticket, "terminal", "shell"))
        for purpose, resource in (("file", "shell"), ("terminal", "other")):
            ticket = tickets.issue("terminal", "shell", 10)
            self.assertFalse(tickets.redeem(ticket, purpose, resource))
        ticket = tickets.issue("terminal", "shell", 10)
        now[0] += 10
        self.assertFalse(tickets.redeem(ticket, "terminal", "shell"))

    def test_capacity_and_cleanup(self):
        now = [1]
        tickets = CapabilityTickets("workspace", clock=lambda: now[0], limit=1)
        tickets.issue("terminal", "shell", 1)
        with self.assertRaises(ValueError):
            tickets.issue("terminal", "shell", 1)
        now[0] = 3
        self.assertTrue(tickets.issue("terminal", "shell", 1))

    def test_signed_link_cannot_be_replayed_for_other_purpose_or_service(self):
        signer = ResourceSigner("secret", "workspace")
        signature = signer.sign("raw", 1010, [2, "/work/sub"])
        self.assertTrue(signer.check(signature, "raw", 1010, [2, "/work/sub"], now=1000))
        self.assertFalse(signer.check(signature, "terminal", 1010, [2, "/work/sub"], now=1000))
        self.assertFalse(signer.check(signature, "raw", 1010, [2, "/work/sub"], now=1010))
        self.assertFalse(ResourceSigner("secret", "rest").check(signature, "raw", 1010, [2, "/work/sub"], now=1000))

    def test_helpers_keep_workspace_python36_grammar(self):
        ast.parse((Path(__file__).resolve().parents[1] / "hpclib/servers/token_auth.py").read_text(), feature_version=(3, 6))


class PackagingTests(unittest.TestCase):
    def test_core_helpers_refuse_external_paths_and_unknown_sources(self):
        import console_packages
        for request in ({"../escape": "token_auth"}, {"elsewhere.py": "token_auth"},
                        {"tunnels/workspace/auth.py": "../../secret"},
                        {"tunnels/workspace/auth.py": {"bad": "type"}}):
            with self.subTest(request=request), self.assertRaises(console_packages.PackageError):
                console_packages.core_helpers({"tunnels": {"workspace": "tunnels/workspace"}, "core_helpers": request})

    def test_public_api_uses_the_same_implementation(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hpclib.auth import TokenRegistry as PublicRegistry
        self.assertEqual(PublicRegistry.__module__, "hpclib.servers.token_auth")
        self.assertEqual(PublicRegistry.MAX_DEPTH, TokenRegistry.MAX_DEPTH)
