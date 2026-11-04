# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Synthetic primary-route decisions and Kubernetes patch checks."""

import copy
import http.client
import sys
import threading
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


path = Path(__file__).resolve().parents[1] / "files" / "managed-primary-controller.py"
sys.path.insert(0, str(path.parent))
from managed_api import ClusterState, HttpError, InvalidResponse, TechnitiumClient
from managed_leader import Leader, LostLeadership

spec = importlib.util.spec_from_file_location("managed_primary_controller", path)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)

BASE = "dns-technitium-cluster"
DOMAIN = "cluster.example.test"
IPS = ["10.0.0.10", "10.0.0.11", "10.0.0.12"]


def name(ordinal):
    return f"{BASE}-{ordinal}.{DOMAIN}"


def nodes(primary, members=(0, 1, 2)):
    return [
        {
            "id": ordinal + 1,
            "name": name(ordinal),
            "url": f"https://{name(ordinal)}:53443/",
            "ipAddresses": [IPS[ordinal]],
            "type": "Primary" if ordinal == primary else "Secondary",
        }
        for ordinal in members
    ]


def state(ordinal, primary=0, members=(0, 1, 2)):
    return {
        "dnsServerDomain": name(ordinal),
        "clusterInitialized": True,
        "clusterDomain": DOMAIN,
        "clusterNodes": nodes(primary, members),
    }


class PrimaryControllerTests(unittest.TestCase):
    def choose(self, states, ips=IPS):
        try:
            parsed = [ClusterState.parse(item) if item is not None else None for item in states]
        except InvalidResponse:
            return None
        return controller.choose_primary(parsed, ips, BASE, DOMAIN)

    def test_routes_to_verified_primary(self):
        self.assertEqual(self.choose([state(i) for i in range(3)]), f"{BASE}-0")

    def test_follows_graceful_promotion_with_quorum(self):
        old = {"dnsServerDomain": "dns-technitium-cluster-0", "clusterInitialized": False}
        self.assertEqual(
            self.choose([old, state(1, 1, (1, 2)), state(2, 1, (1, 2))]),
            f"{BASE}-1",
        )

    def test_two_members_need_both_votes_after_promotion(self):
        old = {"dnsServerDomain": "old", "clusterInitialized": False}
        self.assertIsNone(self.choose([old, state(1, 1, (1,))], IPS[:2]))

    def test_rejects_disagreeing_primary(self):
        self.assertIsNone(self.choose([state(0), state(1, 1), state(2)]))

    def test_rejects_primary_without_self_observation(self):
        self.assertIsNone(self.choose([None, state(1), state(2)]))

    def test_rejects_wrong_stable_address(self):
        states = [state(i) for i in range(3)]
        states[0]["clusterNodes"][0]["ipAddresses"] = ["10.0.0.99"]
        self.assertIsNone(self.choose(states))

    def test_rejects_foreign_member(self):
        states = [state(i) for i in range(3)]
        for item in states:
            item["clusterNodes"].append({
                "id": 9, "name": "foreign.cluster.example.test", "url": "https://foreign/",
                "ipAddresses": ["10.0.0.99"], "type": "Secondary",
            })
        self.assertIsNone(self.choose(states))

    def test_rejects_insufficient_votes(self):
        self.assertIsNone(self.choose([state(0), None, None]))

    def test_rejects_foreign_cluster_domain(self):
        states = [state(i) for i in range(3)]
        states[2]["clusterDomain"] = "other.example.test"
        self.assertIsNone(self.choose(states))

    def pod(self, ordinal, primary=False):
        labels = {"app.kubernetes.io/instance": "dns",
                  "app.kubernetes.io/component": "cluster-member"}
        if primary:
            labels[controller.ROLE_KEY] = "primary"
        return {"metadata": {
            "name": f"{BASE}-{ordinal}", "uid": f"uid-{ordinal}",
            "resourceVersion": str(20 + ordinal), "labels": labels,
            "annotations": {controller.TERM_KEY: "test"},
            "ownerReferences": [{"kind": "StatefulSet", "name": BASE, "controller": True}],
        }}

    def reconcile(self, kube, pods, desired, leader=None):
        return controller.reconcile_pods(
            kube, {pod["metadata"]["name"]: pod for pod in pods}, desired, leader or mock.Mock(term="test"))

    def test_removes_old_label_before_assigning_new_primary(self):
        kube = mock.Mock()
        changed, desired = self.reconcile(kube, [self.pod(0, True), self.pod(1)], f"{BASE}-1")
        self.assertTrue(changed)
        removed, assigned = kube.pod.call_args_list
        self.assertEqual(removed.args, (f"{BASE}-0",))
        self.assertEqual(removed.kwargs["patch"][-1]["op"], "remove")
        self.assertEqual(assigned.args, (f"{BASE}-1",))
        self.assertEqual(assigned.kwargs["patch"], [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "21"},
            {"op": "test", "path": "/metadata/annotations/technitium.io~1observer-term", "value": "test"},
            {"op": "add", "path": "/metadata/labels/technitium.io~1cluster-role", "value": "primary"},
        ])

    def test_does_not_assign_when_old_label_cannot_be_removed(self):
        kube = mock.Mock()
        kube.pod.side_effect = OSError("unavailable")
        with self.assertRaises(OSError):
            self.reconcile(kube, [self.pod(0, True), self.pod(1)], f"{BASE}-1")
        kube.pod.assert_called_once()
        self.assertEqual(kube.pod.call_args.args[0], f"{BASE}-0")

    def test_refuses_unowned_pod(self):
        pod = self.pod(0)
        pod["metadata"]["ownerReferences"][0]["name"] = "foreign"
        kube = mock.Mock()
        kube.pod.return_value = pod
        with self.assertRaises(controller.ApiError):
            controller.read_member_pods(kube, "dns", BASE, 1)
        kube.pod.assert_called_once()

    def test_absent_member_pod_is_not_an_api_failure(self):
        kube = mock.Mock()
        kube.pod.side_effect = [self.pod(0), HttpError(404)]
        self.assertEqual(list(controller.read_member_pods(kube, "dns", BASE, 2)), [f"{BASE}-0"])

    def test_clears_stale_labels_when_observation_is_uncertain(self):
        kube = mock.Mock()
        changed, desired = self.reconcile(kube, [self.pod(0, True), self.pod(1, True)], controller.UNASSIGNED)
        self.assertTrue(changed)
        self.assertEqual(desired, controller.UNASSIGNED)
        self.assertEqual(kube.pod.call_count, 2)
        self.assertTrue(all(call.kwargs["patch"][-1]["op"] == "remove" for call in kube.pod.call_args_list))

    def test_does_not_label_a_terminating_primary(self):
        pod = self.pod(0, True)
        pod["metadata"]["deletionTimestamp"] = "2026-11-04T00:00:00Z"
        kube = mock.Mock()
        changed, desired = self.reconcile(kube, [pod], f"{BASE}-0")
        self.assertTrue(changed)
        self.assertEqual(desired, controller.UNASSIGNED)
        self.assertEqual(kube.pod.call_args.kwargs["patch"][-1]["op"], "remove")

    def test_repeated_observation_does_not_write_pods(self):
        kube = mock.Mock()
        changed, _ = self.reconcile(kube, [self.pod(0, True), self.pod(1)], f"{BASE}-0")
        self.assertFalse(changed)
        kube.pod.assert_not_called()

    def test_truncated_response_counts_as_unavailable_and_clears_stale_route(self):
        import managed_api
        clients = {ip: TechnitiumClient(ip, 53443, "admin", token="synthetic") for ip in IPS}
        with mock.patch.object(controller.socket, "gethostbyname", side_effect=IPS), \
             mock.patch.object(managed_api, "open_request", side_effect=http.client.IncompleteRead(b"{", 20)):
            desired = controller.observe("test", BASE, DOMAIN, 3, 53443, "admin", "synthetic", True, clients)
        self.assertEqual(desired, controller.UNASSIGNED)
        kube = mock.Mock()
        self.reconcile(kube, [self.pod(0, True)], desired)
        self.assertEqual(kube.pod.call_args.kwargs["patch"][-1]["op"], "remove")


class FakeKube:
    def __init__(self):
        self.current = {"metadata": {"uid": "lease", "resourceVersion": "1"}}
        self.pods = {f"{BASE}-{i}": PrimaryControllerTests().pod(i) for i in range(2)}

    def lease(self, name, patch=None):
        if patch:
            assert patch[0]["value"] == self.current["metadata"]["uid"]
            if patch[1]["value"] != self.current["metadata"]["resourceVersion"]:
                raise HttpError(409)
            self.current["spec"] = copy.deepcopy(patch[2]["value"])
            self.current["metadata"]["resourceVersion"] = str(int(self.current["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(self.current)

    def pod(self, name, patch=None):
        current = self.pods[name]["metadata"]
        if patch:
            if patch[0]["value"] != current["uid"] or patch[1]["value"] != current["resourceVersion"]:
                raise HttpError(409)
            if len(patch) == 4 and patch[2]["value"] != current.get("annotations", {}).get(controller.TERM_KEY):
                raise HttpError(409)
            op = patch[-1]
            if op["path"] == "/metadata/annotations":
                current["annotations"] = copy.deepcopy(op["value"])
            elif op["op"] == "remove":
                current["labels"].pop(controller.ROLE_KEY)
            else:
                current["labels"][controller.ROLE_KEY] = op["value"]
            current["resourceVersion"] = str(int(current["resourceVersion"]) + 1)
        return copy.deepcopy(self.pods[name])


class LeadershipTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        patch = mock.patch("managed_leader.time.monotonic", side_effect=lambda: self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.kube = FakeKube()
        self.stop = threading.Event()
        self.a = Leader(self.kube, "observer", "a", self.stop)
        self.b = Leader(self.kube, "observer", "b", threading.Event())

    def test_overlapping_observer_waits_for_leadership(self):
        self.assertTrue(self.a.acquire())
        self.assertFalse(self.b.acquire())
        with self.assertRaises(LostLeadership):
            controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.b)
        self.assertTrue(all(controller.ROLE_KEY not in p["metadata"]["labels"] for p in self.kube.pods.values()))

    def test_expired_observer_cannot_apply_old_snapshot_after_takeover(self):
        self.a.acquire()
        old = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.a)
        self.assertFalse(self.b.acquire())
        self.clock = 31
        self.assertTrue(self.b.acquire())
        fresh = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.b)
        controller.reconcile_pods(self.kube, fresh, f"{BASE}-1", self.b)
        with self.assertRaises(LostLeadership):
            controller.reconcile_pods(self.kube, old, f"{BASE}-0", self.a)
        selected = [n for n, p in self.kube.pods.items() if p["metadata"]["labels"].get(controller.ROLE_KEY) == "primary"]
        self.assertEqual(selected, [f"{BASE}-1"])

    def test_api_rejects_old_write_delayed_after_its_leadership_check(self):
        self.a.acquire()
        old = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.a)
        self.b.acquire()
        with mock.patch.object(self.kube, "pod") as request:
            controller.patch_role(self.kube, old[f"{BASE}-0"], "primary", self.a)
        delayed = request.call_args.kwargs["patch"]
        self.clock = 31
        # b observes a's last renewal before starting its expiry timer.
        self.assertFalse(self.b.acquire())
        self.clock = 62
        self.assertTrue(self.b.acquire())
        fresh = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.b)
        controller.reconcile_pods(self.kube, fresh, f"{BASE}-1", self.b)
        with self.assertRaises(HttpError):
            self.kube.pod(f"{BASE}-0", patch=delayed)
        self.assertNotIn(controller.ROLE_KEY, self.kube.pods[f"{BASE}-0"]["metadata"]["labels"])

    def test_renewal_blocks_takeover(self):
        self.a.acquire()
        self.b.acquire()
        self.clock = 15
        self.a.renew()
        self.assertFalse(self.b.acquire())
        self.clock = 31
        self.assertFalse(self.b.acquire())

    def test_uncertain_patch_cannot_arrive_after_same_holder_retries(self):
        self.a.acquire()
        old = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.a)
        with mock.patch.object(self.kube, "pod") as request:
            controller.patch_role(self.kube, old[f"{BASE}-0"], "primary", self.a)
        delayed = request.call_args.kwargs["patch"]
        # The client timed out, but the server may still process the old request.
        self.a.invalidate()
        self.assertTrue(self.a.acquire())
        fresh = controller.fence_pods(self.kube, copy.deepcopy(self.kube.pods), self.a)
        controller.reconcile_pods(self.kube, fresh, f"{BASE}-1", self.a)
        with self.assertRaises(HttpError):
            self.kube.pod(f"{BASE}-0", patch=delayed)
        self.assertNotIn(controller.ROLE_KEY, self.kube.pods[f"{BASE}-0"]["metadata"]["labels"])

    def test_termination_blocks_writes_and_release_allows_replacement(self):
        self.a.acquire()
        self.stop.set()
        with self.assertRaises(LostLeadership):
            controller.reconcile_pods(self.kube, copy.deepcopy(self.kube.pods), f"{BASE}-0", self.a)
        # After leadership is lost, expiry handles takeover; a normal shutdown releases immediately.
        self.stop.clear()
        self.a.acquire()
        self.a.release()
        self.assertTrue(self.b.acquire())

    def test_lease_recreation_invalidates_observation(self):
        self.a.acquire()
        self.kube.current["metadata"]["uid"] = "replacement"
        with self.assertRaises(LostLeadership):
            self.a.renew()


if __name__ == "__main__":
    unittest.main()
