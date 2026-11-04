# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Route the managed primary Service from verified Technitium cluster state."""

import concurrent.futures
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
import uuid

from managed_api import ApiError, Deadline, HttpError, InvalidResponse, KubernetesClient, TechnitiumClient
from managed_leader import Leader

UNASSIGNED = "unassigned"
ROLE_KEY = "technitium.io/cluster-role"
TERM_KEY = "technitium.io/observer-term"


def choose_primary(states, peer_ips, member_base, domain):
    """A majority must agree, including the selected primary itself."""
    count = len(peer_ips)
    expected = {f"{member_base}-{ordinal}.{domain}": ordinal for ordinal in range(count)}
    votes = []
    for ordinal, state in enumerate(states):
        if state is None or not state.initialized:
            continue
        if state.name != f"{member_base}-{ordinal}.{domain}" or state.domain != domain:
            return None
        if set(state.members) - set(expected):
            return None
        try:
            primary = state.primary
        except InvalidResponse:
            return None
        own = state.members.get(state.name)
        if own is None or peer_ips[ordinal] not in own.addresses:
            return None
        votes.append((ordinal, state, primary))
    if len(votes) < count // 2 + 1:
        return None
    _, canonical, primary = votes[0]
    if any(state.nodes != canonical.nodes for _, state, _ in votes):
        return None
    primary_ordinal = expected[primary.name]
    if peer_ips[primary_ordinal] not in primary.addresses:
        return None
    if not any(ordinal == primary_ordinal for ordinal, _, _ in votes):
        return None
    return f"{member_base}-{primary_ordinal}"


def read_member_pods(kube, release_name, member_base, count):
    pods = {}
    for ordinal in range(count):
        name = f"{member_base}-{ordinal}"
        try:
            pod = kube.pod(name)
        except HttpError as exc:
            if exc.status == 404:
                continue
            raise
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {})
        owners = metadata.get("ownerReferences", [])
        if (metadata.get("name") != name
                or labels.get("app.kubernetes.io/instance") != release_name
                or labels.get("app.kubernetes.io/component") != "cluster-member"
                or not any(owner.get("kind") == "StatefulSet"
                           and owner.get("name") == member_base
                           and owner.get("controller") is True for owner in owners)):
            raise ApiError("member Pod ownership differs from chart")
        pods[name] = pod
    return pods


def fence_pods(kube, pods, leader):
    """Invalidate prior holders' Pod snapshots before changing any role labels."""
    fenced = {}
    for name, pod in pods.items():
        metadata = pod["metadata"]
        annotations = dict(metadata.get("annotations", {}))
        if annotations.get(TERM_KEY) != leader.term:
            # Read before renewing, then CAS: an old holder cannot stamp a newly fenced Pod.
            leader.renew()
            annotations[TERM_KEY] = leader.term
            pod = kube.pod(name, patch=[
                {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                {"op": "add", "path": "/metadata/annotations", "value": annotations},
            ])
        fenced[name] = pod
    return fenced


def patch_role(kube, pod, role, leader):
    # A holder may renew here, but must not acquire a new term using old observations.
    leader.renew()
    metadata = pod["metadata"]
    current = metadata["labels"].get(ROLE_KEY)
    path = "/metadata/labels/" + ROLE_KEY.replace("~", "~0").replace("/", "~1")
    operation = {"op": "remove", "path": path} if role is None else {
        "op": "add" if current is None else "replace", "path": path, "value": role,
    }
    kube.pod(metadata["name"], patch=[
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/metadata/annotations/technitium.io~1observer-term", "value": leader.term},
        operation,
    ])


def reconcile_pods(kube, pods, desired, leader):
    leader.renew()
    target = pods.get(desired)
    if target is None or target["metadata"].get("deletionTimestamp"):
        desired = UNASSIGNED
    changed = False
    for name, pod in pods.items():
        if name != desired and ROLE_KEY in pod["metadata"]["labels"]:
            patch_role(kube, pod, None, leader)
            changed = True
    if desired != UNASSIGNED and target["metadata"]["labels"].get(ROLE_KEY) != "primary":
        patch_role(kube, target, "primary", leader)
        changed = True
    return changed, desired


def observe(namespace, member_base, domain, count, port, username, credential, token_auth, clients,
            verify_tls=True, ca_file=None):
    peer_ips = []
    for ordinal in range(count):
        try:
            peer_ips.append(socket.gethostbyname(f"{member_base}-{ordinal}-web.{namespace}.svc"))
        except socket.gaierror:
            peer_ips.append(None)
    for ordinal, ip in enumerate(peer_ips):
        if ip and ip not in clients:
            clients[ip] = TechnitiumClient(ip, port, username,
                                          password="" if token_auth else credential,
                                          token=credential if token_auth else "",
                                          verify_tls=verify_tls, ca_file=ca_file,
                                          server_name=f"{member_base}-{ordinal}.{domain}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(clients[ip].state, Deadline(15)) if ip else None for ip in peer_ips]
        states = []
        for future in futures:
            try:
                states.append(future.result() if future else None)
            except ApiError as exc:
                print(f"managed primary observation: member {len(states)} unavailable ({exc})",
                      file=sys.stderr, flush=True)
                states.append(None)
    return choose_primary(states, peer_ips, member_base, domain) or UNASSIGNED


def main():
    namespace, release_name = os.environ["NAMESPACE"], os.environ["RELEASE_NAME"]
    member_base, domain = os.environ["MEMBER_BASE"], os.environ["CLUSTER_DOMAIN"]
    count, port = int(os.environ["SECONDARIES"]) + 1, int(os.environ["HTTPS_PORT"])
    username = os.environ["OBSERVER_USER"]
    verify_tls, ca_file = os.environ.get("VERIFY_TLS", "true") == "true", os.environ.get("CA_FILE")
    token_auth = bool(os.environ.get("TOKEN_FILE"))
    credential_file = Path(os.environ["TOKEN_FILE"] if token_auth else os.environ["PASSWORD_FILE"])
    interval = min(int(os.environ["POLL_SECONDS"]), 10)
    poll_seconds = int(os.environ["POLL_SECONDS"])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    kube = KubernetesClient(namespace)
    leader = Leader(kube, os.environ["LEASE_NAME"], f"{os.environ['POD_UID']}/{uuid.uuid4()}", stop)
    clients, last_credential, previous = {}, None, None
    # Renew even when the configured observation interval exceeds the lease duration.
    next_observation = 0
    try:
        while not stop.is_set():
            try:
                was_leader = leader.held
                if not leader.acquire():
                    next_observation = 0
                elif was_leader and time.monotonic() < next_observation:
                    pass
                else:
                    pods = read_member_pods(kube, release_name, member_base, count)
                    pods = fence_pods(kube, pods, leader)
                    credential = credential_file.read_text()
                    if credential != last_credential:
                        clients.clear()
                        last_credential = credential
                    desired = observe(namespace, member_base, domain, count, port, username,
                                      credential, token_auth, clients, verify_tls, ca_file)
                    changed, desired = reconcile_pods(kube, pods, desired, leader)
                    if changed or desired != previous:
                        print(f"managed primary route: {desired}", flush=True)
                    previous = desired
                    next_observation = time.monotonic() + poll_seconds
            except (ApiError, OSError) as exc:
                leader.invalidate()
                print(f"managed primary route: reconcile failed ({exc})", file=sys.stderr, flush=True)
                next_observation = 0
            # Unexpected exceptions propagate with a traceback and restart the container.
            stop.wait(interval)
    finally:
        try:
            leader.release()
        except ApiError as exc:
            print(f"managed primary route: Lease release failed ({exc})", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
