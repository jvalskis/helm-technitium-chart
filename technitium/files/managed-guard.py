# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Check the existing StatefulSet before Helm changes managed workloads."""

import os
import sys
import urllib.parse

from managed_api import ApiError, KubernetesClient


def run(env):
    namespace, name = env["NAMESPACE"], env["STATEFULSET_NAME"]
    requested = int(env["SECONDARIES"]) + 1
    query = urllib.parse.urlencode({"labelSelector": f"app.kubernetes.io/instance={env['RELEASE_NAME']}"})
    try:
        current = KubernetesClient(namespace).request(
            f"/apis/apps/v1/namespaces/{namespace}/statefulsets?{query}")["items"]
    except (ApiError, KeyError) as exc:
        raise ApiError(f"could not inspect existing StatefulSets: {type(exc).__name__}") from None
    if len(current) > 1:
        raise ApiError("multiple managed StatefulSets belong to this release; inspect them before upgrading")
    if not current:
        print("managed cluster guard: no existing managed StatefulSet", flush=True)
        return
    workload = current[0]
    if workload["metadata"]["name"] != name:
        raise ApiError("managed StatefulSet naming cannot change during an upgrade")
    previous = workload["spec"]["replicas"]
    if requested < previous:
        raise ApiError(f"scale-down from {previous} to {requested} members is unsupported")
    containers = workload["spec"]["template"]["spec"]["containers"]
    dns = next((item for item in containers if item["name"] == "technitium-dns-server"), None)
    expected = f"$(POD_NAME).{env['CLUSTER_DOMAIN']}"
    if dns is None or not any(item.get("name") == "DNS_SERVER_DOMAIN" and item.get("value") == expected
                              for item in dns.get("env", [])):
        raise ApiError("managed cluster domain cannot change during an upgrade")
    print(f"managed cluster guard: {previous} to {requested} members permitted", flush=True)


def main():
    try:
        run(os.environ)
    except ApiError as exc:
        print(f"managed cluster guard: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
