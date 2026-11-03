# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Synthetic API checks; no cluster, credentials or network calls are used."""

import builtins
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request


FILES = Path(__file__).resolve().parents[1] / "files"


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class FakeApis:
    def __init__(self, count=1, initialized=False, joined=0):
        self.count = count
        self.member_base = "dns-technitium-cluster"
        self.bootstrap_name = f"{self.member_base}-0.cluster.example.test"
        self.names = [f"{self.member_base}-{i}.cluster.example.test" for i in range(count + 1)]
        self.ips = [f"10.0.0.{i + 10}" for i in range(count + 1)]
        self.ready = [initialized] + [i < joined for i in range(count)]
        self.registered = {i for i, ready in enumerate(self.ready) if ready}
        self.current_primary = 0
        self.calls = []
        self.workloads = []
        self.token_reads = 0
        self.lost_responses = set()
        self.extra_nodes = []

    def workload(self, replicas):
        return {
            "metadata": {"name": self.member_base},
            "spec": {
                "replicas": replicas,
                "template": {"spec": {"containers": [{
                    "name": "technitium-dns-server",
                    "env": [{"name": "DNS_SERVER_DOMAIN", "value": "$(POD_NAME).cluster.example.test"}],
                }]}},
            },
        }

    def cluster_nodes(self):
        return [
            {
                "id": i + 1,
                "name": self.names[i],
                "url": f"https://{self.names[i]}:53443/",
                "ipAddresses": [self.ips[i]],
                "type": "Primary" if i == self.current_primary else "Secondary",
            }
            for i in sorted(self.registered)
        ] + self.extra_nodes

    def cluster_state(self, index):
        result = {"dnsServerDomain": self.names[index], "clusterInitialized": self.ready[index]}
        if self.ready[index]:
            nodes = self.cluster_nodes()
            for node in nodes:
                node["lastSeen"] = f"different-observation-from-member-{index}"
            result.update(clusterDomain="cluster.example.test", clusterNodes=nodes)
        return result

    def resolve(self, service):
        for index in range(self.count + 1):
            if service == f"{self.member_base}-{index}-web.test.svc":
                return self.ips[index]
        raise socket.gaierror(service)

    def urlopen(self, req, context=None, timeout=None):
        method = req.get_method()
        url = urllib.parse.urlparse(req.full_url)
        if url.path == "/apis/apps/v1/namespaces/test/statefulsets":
            assert method == "GET"
            assert urllib.parse.parse_qs(url.query)["labelSelector"] == [
                "app.kubernetes.io/instance=dns"
            ]
            self.calls.append((method, url.path, None))
            return FakeResponse({"items": self.workloads})
        index = self.ips.index(url.hostname)
        path = url.path
        self.calls.append((method, path, index))
        if path == "/api/user/login":
            return FakeResponse({"status": "ok", "token": "synthetic-token"})
        if path == "/api/admin/cluster/state":
            return FakeResponse({"status": "ok", "response": self.cluster_state(index)})
        if path in ("/api/admin/cluster/init", "/api/admin/cluster/initJoin"):
            self.ready[index] = True
            self.registered.add(index)
            if path in self.lost_responses:
                self.lost_responses.remove(path)
                raise TimeoutError("synthetic lost response after successful mutation")
            return FakeResponse({"status": "ok", "response": self.cluster_state(index)})
        if path == "/api/settings/set":
            return FakeResponse({"status": "ok"})
        raise AssertionError(path)


class ManagedScriptsTests(unittest.TestCase):
    def run_script(self, filename, fake, extra=None):
        env = {
            "NAMESPACE": "test",
            "STATEFULSET_NAME": fake.member_base,
            "RELEASE_NAME": "dns",
            "SECONDARIES": str(fake.count),
            "KUBERNETES_SERVICE_HOST": "10.96.0.1",
            "KUBERNETES_SERVICE_PORT": "443",
            "CLUSTER_DOMAIN": "cluster.example.test",
            "BOOTSTRAP_MEMBER_DOMAIN": fake.bootstrap_name,
            "BOOTSTRAP_MEMBER_SERVICE": f"{fake.member_base}-0-web",
            "MEMBER_BASE": fake.member_base,
            "ADMIN_USER": "admin",
            "ADMIN_PASSWORD": "synthetic-only",
            "PRIMARY_TOTP": "",
            "POD_NETWORKS": "10.244.0.0/16",
            "HTTP_PORT": "5380",
            "HTTPS_PORT": "53443",
            "ALLOW_INITIALIZE": "false",
        }
        env.update(extra or {})
        original_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("/serviceaccount/token"):
                fake.token_reads += 1
                return io.StringIO("synthetic-service-token")
            return original_open(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch("builtins.open", side_effect=fake_open))
            stack.enter_context(mock.patch.object(urllib.request, "urlopen", side_effect=fake.urlopen))
            stack.enter_context(mock.patch.object(socket, "gethostbyname", side_effect=fake.resolve))
            stack.enter_context(mock.patch.object(ssl, "create_default_context", return_value=None))
            stack.enter_context(mock.patch.object(time, "sleep"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            runpy.run_path(str(FILES / filename), run_name="__main__")

    def rejected(self, fake, message, script="managed-cluster.py", extra=None):
        errors = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(errors):
            self.run_script(script, fake, extra)
        self.assertIn(message, errors.getvalue())
        self.assertFalse(any(method != "GET" and path != "/api/user/login" for method, path, _ in fake.calls))

    def mutations(self, fake):
        return [path for _, path, _ in fake.calls if path.startswith("/api/admin/cluster/init")]

    def test_guard_allows_fresh_install_or_standalone_transition(self):
        fake = FakeApis(count=0)
        self.run_script("managed-guard.py", fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "GET")

    def test_guard_allows_expansion_using_current_workload(self):
        fake = FakeApis(count=2)
        fake.workloads = [fake.workload(1)]
        self.run_script("managed-guard.py", fake)
        self.assertEqual(len(fake.calls), 1)

    def test_guard_rejects_scale_down(self):
        fake = FakeApis(count=1)
        fake.workloads = [fake.workload(3)]
        self.rejected(fake, "scale-down", "managed-guard.py")

    def test_guard_rejects_naming_change(self):
        fake = FakeApis(count=1)
        fake.workloads = [fake.workload(2)]
        fake.workloads[0]["metadata"]["name"] = "old-member-name"
        self.rejected(fake, "naming cannot change", "managed-guard.py")

    def test_guard_rejects_domain_change(self):
        fake = FakeApis(count=1)
        fake.workloads = [fake.workload(2)]
        fake.workloads[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "$(POD_NAME).old.example.test"
        self.rejected(fake, "domain cannot change", "managed-guard.py")

    def test_guard_rejects_ambiguous_workloads(self):
        fake = FakeApis(count=1)
        fake.workloads = [fake.workload(2), fake.workload(2)]
        self.rejected(fake, "multiple managed StatefulSets", "managed-guard.py")

    def test_guard_stops_on_unavailable_kubernetes_api(self):
        fake = FakeApis()
        fake.urlopen = mock.Mock(side_effect=urllib.error.URLError("synthetic outage"))
        self.rejected(fake, "could not inspect", "managed-guard.py")

    def test_initialization_requires_explicit_authorization(self):
        self.rejected(FakeApis(count=0), "initialize it explicitly")

    def test_authorized_initialization_then_two_joins(self):
        fake = FakeApis(count=2)
        self.run_script("managed-cluster.py", fake, {"ALLOW_INITIALIZE": "true"})
        self.assertEqual(fake.ready, [True, True, True])
        self.assertEqual(self.mutations(fake), ["/api/admin/cluster/init"] + ["/api/admin/cluster/initJoin"] * 2)
        self.assertEqual(fake.token_reads, 0)

    def test_existing_membership_needs_no_chart_record(self):
        fake = FakeApis(count=2, initialized=True, joined=2)
        self.run_script("managed-cluster.py", fake)
        self.run_script("managed-cluster.py", fake)
        self.assertEqual(self.mutations(fake), [])
        self.assertEqual(fake.token_reads, 0)

    def test_expansion_joins_only_unregistered_members(self):
        fake = FakeApis(count=2, initialized=True, joined=1)
        self.run_script("managed-cluster.py", fake)
        self.assertEqual(self.mutations(fake), ["/api/admin/cluster/initJoin"])
        self.assertIn(("POST", "/api/admin/cluster/initJoin", 2), fake.calls)

    def test_lost_init_response_uses_persisted_state(self):
        fake = FakeApis(count=0)
        fake.lost_responses.add("/api/admin/cluster/init")
        self.run_script("managed-cluster.py", fake, {"ALLOW_INITIALIZE": "true"})
        self.run_script("managed-cluster.py", fake)
        self.assertEqual(self.mutations(fake), ["/api/admin/cluster/init"])

    def test_lost_join_response_uses_persisted_membership(self):
        fake = FakeApis(count=1, initialized=True)
        fake.lost_responses.add("/api/admin/cluster/initJoin")
        self.run_script("managed-cluster.py", fake)
        self.run_script("managed-cluster.py", fake)
        self.assertEqual(self.mutations(fake), ["/api/admin/cluster/initJoin"])

    def test_partial_join_requires_recovery_on_retry(self):
        fake = FakeApis(count=1, initialized=True)
        original = fake.urlopen
        def partial_join(req, **kwargs):
            if urllib.parse.urlparse(req.full_url).path == "/api/admin/cluster/initJoin":
                fake.registered.add(1)
                raise TimeoutError("synthetic registration without local completion")
            return original(req, **kwargs)
        fake.urlopen = partial_join
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_script("managed-cluster.py", fake)
        self.assertEqual(fake.ready, [True, False])
        fake.calls.clear()
        fake.urlopen = original
        self.rejected(fake, "lost local cluster state")

    def test_lost_all_member_state_does_not_initialize(self):
        self.rejected(FakeApis(count=2), "initialize it explicitly")

    def test_lost_primary_state_with_surviving_peers_is_rejected(self):
        fake = FakeApis(count=2, initialized=True, joined=2)
        fake.ready[0] = False
        self.rejected(fake, "another member is clustered", extra={"ALLOW_INITIALIZE": "true"})

    def test_registered_secondary_with_lost_local_state_is_rejected(self):
        fake = FakeApis(count=1, initialized=True, joined=1)
        fake.ready[1] = False
        self.rejected(fake, "lost local cluster state")

    def test_initialized_but_unregistered_secondary_is_rejected(self):
        fake = FakeApis(count=1, initialized=True, joined=1)
        fake.registered.remove(1)
        self.rejected(fake, "conflicts with the current primary")

    def test_foreign_cluster_is_rejected_before_mutation(self):
        fake = FakeApis(count=1, initialized=True, joined=1)
        original = fake.cluster_state
        def foreign(index):
            result = original(index)
            if index == 1:
                result["clusterDomain"] = "foreign.example.test"
            return result
        fake.cluster_state = foreign
        self.rejected(fake, "another cluster domain")

    def test_same_domain_different_member_identity_is_rejected(self):
        fake = FakeApis(count=1, initialized=True, joined=1)
        original = fake.cluster_state
        def conflicting(index):
            result = original(index)
            if index == 1:
                result["clusterNodes"][1]["id"] = 999
            return result
        fake.cluster_state = conflicting
        self.rejected(fake, "conflicts with the current primary")

    def test_members_outside_requested_topology_are_rejected(self):
        fake = FakeApis(count=0, initialized=True)
        fake.extra_nodes = [{"name": "unexpected.cluster.example.test"}]
        self.rejected(fake, "outside the requested StatefulSet")

    def test_manual_promotion_uses_observed_primary(self):
        fake = FakeApis(count=1, initialized=True, joined=1)
        fake.current_primary = 1
        self.run_script("managed-cluster.py", fake)
        self.assertIn(("POST", "/api/settings/set", 1), fake.calls)
        self.assertNotIn(("POST", "/api/settings/set", 0), fake.calls)

    def test_promotion_requires_former_primary_to_rejoin(self):
        fake = FakeApis(count=2, initialized=True, joined=2)
        fake.current_primary = 1
        fake.ready[0] = False
        fake.registered.remove(0)
        original = fake.cluster_state
        def ejected(index):
            result = original(index)
            if index == 0:
                result["dnsServerDomain"] = fake.member_base + "-0"
            return result
        fake.cluster_state = ejected
        self.rejected(fake, "rejoin ordinal zero as a secondary")

    def test_unreachable_peer_does_not_authorize_initialization(self):
        fake = FakeApis(count=1)
        original = fake.urlopen
        def unavailable(req, **kwargs):
            if urllib.parse.urlparse(req.full_url).hostname == fake.ips[1]:
                raise urllib.error.URLError("synthetic peer outage")
            return original(req, **kwargs)
        fake.urlopen = unavailable
        self.rejected(fake, "could not log in", extra={"ALLOW_INITIALIZE": "true"})


if __name__ == "__main__":
    unittest.main()
