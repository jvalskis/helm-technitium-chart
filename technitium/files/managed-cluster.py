# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Verify persisted membership and join new StatefulSet members."""

from dataclasses import dataclass
import os
import socket
import sys

from managed_api import ApiError, ClusterState, Deadline, InvalidResponse, TechnitiumClient, TransportError


@dataclass
class Member:
    name: str
    ip: str
    client: TechnitiumClient
    state: ClusterState


def check_domain(state, expected, bootstrap, domain):
    if state.name != expected:
        if not state.initialized and expected == bootstrap and state.name == expected[: -(len(domain) + 1)]:
            raise ApiError("former primary left the cluster during promotion; rejoin ordinal zero as a secondary to the current primary before retrying the Helm upgrade")
        raise ApiError(f"member identity {state.name!r} differs from expected {expected}")
    if state.initialized and state.domain != domain:
        raise ApiError(f"member {expected} belongs to another cluster domain")


def anchor_record(state, bootstrap):
    node = state.members.get(bootstrap)
    return (node.id, node.url, node.addresses) if node else None


def check_membership(state, expected, bootstrap, domain, anchor):
    check_domain(state, expected, bootstrap, domain)
    if not state.initialized:
        raise ApiError(f"member {expected} lost cluster state")
    if anchor_record(state, bootstrap) != anchor:
        raise ApiError(f"member {expected} has conflicting cluster membership")


def wait_state(member, deadline, predicate=lambda state: state.initialized):
    while True:
        observed = deadline.retry(lambda: member.client.state(deadline))
        if predicate(observed):
            return observed
        deadline.pause()


def resolve(service):
    try:
        return socket.gethostbyname(service)
    except socket.gaierror:
        raise TransportError(f"could not resolve peer Service {service}") from None


def run(env):
    domain, base, namespace = env["CLUSTER_DOMAIN"], env["MEMBER_BASE"], env["NAMESPACE"]
    bootstrap_name = env["BOOTSTRAP_MEMBER_DOMAIN"]
    verify_tls = env.get("VERIFY_TLS", "true") == "true"
    # One budget covers discovery, retries, joins and final convergence.
    deadline = Deadline(900)
    members = []
    for ordinal in range(int(env["SECONDARIES"]) + 1):
        service = env["BOOTSTRAP_MEMBER_SERVICE"] if ordinal == 0 else f"{base}-{ordinal}-web"
        ip = deadline.retry(lambda: resolve(f"{service}.{namespace}.svc"))
        # This explicit migration override applies only to ordinal zero. Never downgrade on failure.
        use_http = ordinal == 0 and env.get("BOOTSTRAP_HTTP", "false") == "true"
        expected = f"{base}-{ordinal}.{domain}"
        client = TechnitiumClient(ip, env["HTTP_PORT"] if use_http else env["HTTPS_PORT"],
                                  env["ADMIN_USER"], env["ADMIN_PASSWORD"],
                                  totp=env.get("PRIMARY_TOTP", ""), scheme="http" if use_http else "https",
                                  verify_tls=verify_tls, ca_file=env.get("CA_FILE"), server_name=expected)
        observed = deadline.retry(lambda: client.state(deadline))
        check_domain(observed, expected, bootstrap_name, domain)
        members.append(Member(expected, ip, client, observed))

    bootstrap = members[0]
    if not bootstrap.state.initialized:
        if any(member.state.initialized for member in members[1:]):
            raise ApiError("another member is clustered while ordinal zero is unclustered; recover membership manually")
        if env.get("ALLOW_INITIALIZE", "false") != "true":
            raise ApiError("ordinal zero is unclustered; initialize it explicitly or recover its saved state before retrying")
        try:
            bootstrap.client.call("/api/admin/cluster/init", deadline,
                                  form={"clusterDomain": domain, "primaryNodeIpAddresses": bootstrap.ip}, timeout=120)
        except (TransportError, InvalidResponse):
            print("managed cluster: init response uncertain; inspecting persisted state", flush=True)
        bootstrap.state = wait_state(bootstrap, deadline)
        check_domain(bootstrap.state, bootstrap.name, bootstrap_name, domain)

    anchor = anchor_record(bootstrap.state, bootstrap_name)
    if anchor is None:
        raise ApiError("bootstrap member state does not include its stable identity")
    primary = bootstrap.state.primary
    peers = {member.name: member for member in members}
    active = peers.get(primary.name)
    if active is None or active.ip not in primary.addresses:
        raise ApiError("current primary does not match an expected member's stable peer address")
    active_state = deadline.retry(lambda: active.client.state(deadline))
    check_membership(active_state, active.name, bootstrap_name, domain, anchor)
    registered = active_state.members
    if set(registered) - set(peers):
        raise ApiError("cluster contains members outside the requested StatefulSet; remove members deliberately before changing topology")
    if registered.get(primary.name) != primary:
        raise ApiError("members disagree about the current primary; retry after membership converges")

    # Validate every saved member before changing settings or initiating joins.
    for member in members:
        saved = registered.get(member.name)
        if member.state.initialized:
            check_membership(member.state, member.name, bootstrap_name, domain, anchor)
            if saved is None or member.state.members.get(member.name) != saved:
                raise ApiError(f"member {member.name} conflicts with the current primary's membership")
            if member.state.members.get(primary.name) != primary:
                raise ApiError(f"member {member.name} disagrees about the current primary")
            if member.ip not in saved.addresses:
                raise ApiError(f"member {member.name} has a different saved peer address")
        elif saved is not None:
            raise ApiError(f"registered member {member.name} lost local cluster state; recover it explicitly")

    active.client.call("/api/settings/set", deadline, form={
        "zoneTransferAllowedNetworks": env["POD_NETWORKS"], "notifyAllowedNetworks": env["POD_NETWORKS"],
    })
    for member in members[1:]:
        if member.state.initialized:
            continue
        try:
            member.client.call("/api/admin/cluster/initJoin", deadline, form={
                "secondaryNodeIpAddresses": member.ip,
                "primaryNodeUrl": primary.url,
                "primaryNodeIpAddress": active.ip,
                "ignoreCertificateErrors": str(not verify_tls).lower(),
                "primaryNodeUsername": env["ADMIN_USER"],
                "primaryNodePassword": env["ADMIN_PASSWORD"],
                "primaryNodeTotp": env.get("PRIMARY_TOTP", ""),
            }, timeout=150)
        except (TransportError, InvalidResponse):
            print(f"managed cluster: join response uncertain for {member.name}; inspecting state", flush=True)
        observed = wait_state(member, deadline)
        check_membership(observed, member.name, bootstrap_name, domain, anchor)
        print(f"managed cluster: member {member.name} joined", flush=True)

    final = wait_state(active, deadline)
    check_membership(final, active.name, bootstrap_name, domain, anchor)
    canonical = final.members
    if set(canonical) != set(peers) or canonical.get(primary.name) != primary:
        raise ApiError("primary membership changed unexpectedly while joining; inspect state before retrying")
    for name, node in canonical.items():
        if peers[name].ip not in node.addresses:
            raise ApiError(f"member {name} has a different saved peer address")
    for member in members:
        def converged(state):
            check_membership(state, member.name, bootstrap_name, domain, anchor)
            return state.members == canonical
        wait_state(member, deadline, converged)
    print(f"managed cluster: {len(members)} members verified", flush=True)


def main():
    try:
        run(os.environ)
    except ApiError as exc:
        print(f"managed cluster: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
