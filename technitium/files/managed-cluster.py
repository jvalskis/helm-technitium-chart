# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Verify persisted membership and join new StatefulSet members."""

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def fail(message):
    print(f"managed cluster: {message}", file=sys.stderr)
    sys.exit(1)


domain = os.environ["CLUSTER_DOMAIN"]
bootstrap_domain = os.environ["BOOTSTRAP_MEMBER_DOMAIN"]
bootstrap_service = os.environ["BOOTSTRAP_MEMBER_SERVICE"]
member_base = os.environ["MEMBER_BASE"]
namespace = os.environ["NAMESPACE"]
count = int(os.environ["SECONDARIES"])
admin_user = os.environ["ADMIN_USER"]
password = os.environ["ADMIN_PASSWORD"]
totp = os.environ.get("PRIMARY_TOTP", "")
pod_networks = os.environ["POD_NETWORKS"]
http_port = os.environ["HTTP_PORT"]
https_port = os.environ["HTTPS_PORT"]
allow_initialize = os.environ.get("ALLOW_INITIALIZE", "false") == "true"
dns_context = ssl._create_unverified_context()


def request(url, *, method="GET", headers=None, form=None, context=None, timeout=15):
    body = None
    headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, context=context, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("API returned a non-object response")
    return result


def service_ip(name):
    address = f"{name}.{namespace}.svc"
    for attempt in range(60):
        try:
            return socket.gethostbyname(address)
        except socket.gaierror:
            if attempt == 59:
                fail(f"could not resolve peer Service {address}")
            time.sleep(2)


def api(base, path, bearer=None, form=None, timeout=30):
    headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
    response = request(
        f"{base}{path}",
        method="POST" if form is not None else "GET",
        headers=headers,
        form=form,
        context=dns_context if base.startswith("https:") else None,
        timeout=timeout,
    )
    if response.get("status") != "ok":
        raise ValueError(f"{path} returned status {response.get('status')!r}")
    return response


def login(ip):
    for attempt in range(80):
        for base in (f"https://{ip}:{https_port}", f"http://{ip}:{http_port}"):
            try:
                result = api(
                    base,
                    "/api/user/login",
                    form={"user": admin_user, "pass": password, "includeInfo": "false"},
                    timeout=5,
                )
                session = result.get("token")
                if session:
                    return base, session
            except (urllib.error.URLError, TimeoutError, ValueError):
                pass
        if attempt == 79:
            fail(f"could not log in to member at {ip}")
        time.sleep(3)


def state(base, session):
    return api(base, "/api/admin/cluster/state", bearer=session)["response"]


def wait_state(ip, expected_initialized=True):
    for attempt in range(40):
        try:
            base, session = login(ip)
            observed = state(base, session)
            if observed.get("clusterInitialized") == expected_initialized:
                return base, session, observed
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            pass
        time.sleep(3)
    fail(f"member at {ip} did not reach expected cluster state")


def check_domain(observed, expected):
    actual = observed.get("dnsServerDomain")
    if actual != expected:
        if (
            not observed.get("clusterInitialized")
            and expected == bootstrap_domain
            and expected.endswith(f".{domain}")
            and actual == expected[: -(len(domain) + 1)]
        ):
            fail(
                "former primary left the cluster during promotion; "
                "rejoin ordinal zero as a secondary to the current primary "
                "before retrying the Helm upgrade"
            )
        fail(f"member identity {actual!r} differs from expected {expected}")
    if observed.get("clusterInitialized") and observed.get("clusterDomain") != domain:
        fail(f"member {expected} belongs to another cluster domain")


def node_identity(node):
    if node is None:
        return None
    return (node.get("id"), node.get("name"), node.get("url"),
            tuple(sorted(node.get("ipAddresses", []))), node.get("type"))


def anchor_record(observed):
    for node in observed.get("clusterNodes", []):
        if node.get("name") == bootstrap_domain:
            return (node.get("id"), node.get("url"), tuple(node.get("ipAddresses", [])))
    return None


def check_membership(observed, expected, anchor):
    check_domain(observed, expected)
    if not observed.get("clusterInitialized"):
        fail(f"member {expected} lost cluster state")
    if anchor_record(observed) != anchor:
        fail(f"member {expected} has conflicting cluster membership")


bootstrap_ip = service_ip(bootstrap_service)
bootstrap_base, bootstrap_token = login(bootstrap_ip)
bootstrap_state = state(bootstrap_base, bootstrap_token)
check_domain(bootstrap_state, bootstrap_domain)

secondaries = []
for ordinal in range(count):
    member_ordinal = ordinal + 1
    peer_name = f"{member_base}-{member_ordinal}-web"
    peer_ip = service_ip(peer_name)
    peer_base, peer_token = login(peer_ip)
    peer_state = state(peer_base, peer_token)
    expected_name = f"{member_base}-{member_ordinal}.{domain}"
    check_domain(peer_state, expected_name)
    secondaries.append((peer_ip, peer_base, peer_token, peer_state, expected_name))

# Inspect every member before making any Technitium API mutation. Only an
# explicitly authorized fresh Helm install may initialize an unclustered server.
if not bootstrap_state.get("clusterInitialized"):
    if any(item[3].get("clusterInitialized") for item in secondaries):
        fail("another member is clustered while ordinal zero is unclustered; recover membership manually")
    if not allow_initialize:
        fail("ordinal zero is unclustered; initialize it explicitly or recover its saved state before retrying")
    try:
        api(
            bootstrap_base,
            "/api/admin/cluster/init",
            bearer=bootstrap_token,
            form={"clusterDomain": domain, "primaryNodeIpAddresses": bootstrap_ip},
            timeout=120,
        )
    except (urllib.error.URLError, TimeoutError, ValueError):
        print("managed cluster: init response uncertain; inspecting persisted state")
    bootstrap_base, bootstrap_token, bootstrap_state = wait_state(bootstrap_ip)
    check_domain(bootstrap_state, bootstrap_domain)

anchor = anchor_record(bootstrap_state)
if anchor is None:
    fail("bootstrap member state does not include its stable identity")
primaries = [node for node in bootstrap_state.get("clusterNodes", []) if node.get("type") == "Primary"]
if len(primaries) != 1 or not primaries[0].get("ipAddresses") or not primaries[0].get("url"):
    fail("could not identify exactly one current primary")
active_primary = primaries[0]
peers = {bootstrap_domain: bootstrap_ip}
peers.update({item[4]: item[0] for item in secondaries})
if peers.get(active_primary.get("name")) not in active_primary["ipAddresses"]:
    fail("current primary does not match an expected member's stable peer address")
active_ip = peers[active_primary["name"]]
active_base, active_token = login(active_ip)
active_state = state(active_base, active_token)
check_membership(active_state, active_primary["name"], anchor)
registered = {node["name"]: node for node in active_state.get("clusterNodes", [])}
if set(registered) - set(peers):
    fail("cluster contains members outside the requested StatefulSet; remove members deliberately before changing topology")
if node_identity(registered.get(active_primary["name"])) != node_identity(active_primary):
    fail("members disagree about the current primary; retry after membership converges")

# The primary's membership distinguishes a new secondary from an existing member
# that lost its local state. An uncertain join is accepted only after both sides
# report consistent membership. Unregistered initialized nodes require review.
for peer_ip, peer_base, peer_token, peer_state, expected_name in [
    (bootstrap_ip, bootstrap_base, bootstrap_token, bootstrap_state, bootstrap_domain)
] + secondaries:
    member = registered.get(expected_name)
    if peer_state.get("clusterInitialized"):
        check_membership(peer_state, expected_name, anchor)
        saved = {node["name"]: node for node in peer_state.get("clusterNodes", [])}
        if member is None or node_identity(saved.get(expected_name)) != node_identity(member):
            fail(f"member {expected_name} conflicts with the current primary's membership")
        if node_identity(saved.get(active_primary["name"])) != node_identity(active_primary):
            fail(f"member {expected_name} disagrees about the current primary")
        if peer_ip not in member.get("ipAddresses", []):
            fail(f"member {expected_name} has a different saved peer address")
    elif member is not None:
        fail(f"registered member {expected_name} lost local cluster state; recover it explicitly")

api(
    active_base,
    "/api/settings/set",
    bearer=active_token,
    form={
        "zoneTransferAllowedNetworks": pod_networks,
        "notifyAllowedNetworks": pod_networks,
    },
)

for peer_ip, peer_base, peer_token, peer_state, expected_name in secondaries:
    if peer_state.get("clusterInitialized"):
        continue
    try:
        api(
            peer_base,
            "/api/admin/cluster/initJoin",
            bearer=peer_token,
            form={
                "secondaryNodeIpAddresses": peer_ip,
                "primaryNodeUrl": active_primary["url"],
                "primaryNodeIpAddress": active_ip,
                "ignoreCertificateErrors": "true",
                "primaryNodeUsername": admin_user,
                "primaryNodePassword": password,
                "primaryNodeTotp": totp,
            },
            timeout=150,
        )
    except (urllib.error.URLError, TimeoutError, ValueError):
        print(f"managed cluster: join response uncertain for {expected_name}; inspecting state")
    _, _, observed = wait_state(peer_ip)
    check_membership(observed, expected_name, anchor)
    print(f"managed cluster: member {expected_name} joined")

expected = {bootstrap_domain} | {item[4] for item in secondaries}
_, _, final_primary = wait_state(active_ip)
check_membership(final_primary, active_primary["name"], anchor)
canonical = {node["name"]: node_identity(node) for node in final_primary.get("clusterNodes", [])}
if set(canonical) != expected or canonical.get(active_primary["name"]) != node_identity(active_primary):
    fail("primary membership changed unexpectedly while joining; inspect state before retrying")
for node in final_primary.get("clusterNodes", []):
    if peers[node["name"]] not in node.get("ipAddresses", []):
        fail(f"member {node['name']} has a different saved peer address")
for peer_ip, _, _, _, expected_name in [(bootstrap_ip, None, None, None, bootstrap_domain)] + secondaries:
    for attempt in range(60):
        _, _, observed = wait_state(peer_ip)
        check_membership(observed, expected_name, anchor)
        members = {node["name"]: node_identity(node) for node in observed.get("clusterNodes", [])}
        if members == canonical:
            break
        if attempt == 59:
            fail(f"member {expected_name} did not observe consistent cluster membership")
        time.sleep(5)

print(f"managed cluster: {1 + count} members verified")
