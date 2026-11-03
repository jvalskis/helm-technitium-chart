# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Check the existing StatefulSet before Helm changes managed workloads."""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def fail(message):
    print(f"managed cluster guard: {message}", file=sys.stderr)
    sys.exit(1)


namespace = os.environ["NAMESPACE"]
name = os.environ["STATEFULSET_NAME"]
release = os.environ["RELEASE_NAME"]
domain = os.environ["CLUSTER_DOMAIN"]
requested = int(os.environ["SECONDARIES"]) + 1
host = os.environ["KUBERNETES_SERVICE_HOST"]
port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
selector = f"app.kubernetes.io/instance={release}"
query = urllib.parse.urlencode({"labelSelector": selector})
url = f"https://{host}:{port}/apis/apps/v1/namespaces/{namespace}/statefulsets?{query}"
token = open("/var/run/secrets/kubernetes.io/serviceaccount/token", encoding="utf-8").read()
context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, context=context, timeout=15) as response:
        current = json.load(response)["items"]
except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
    fail(f"could not inspect existing StatefulSets: {type(exc).__name__}")

if len(current) > 1:
    fail("multiple managed StatefulSets belong to this release; inspect them before upgrading")
if current:
    workload = current[0]
    if workload["metadata"]["name"] != name:
        fail("managed StatefulSet naming cannot change during an upgrade")
    previous = workload["spec"]["replicas"]
    if requested < previous:
        fail(f"scale-down from {previous} to {requested} members is unsupported")
    containers = workload["spec"]["template"]["spec"]["containers"]
    dns = next((item for item in containers if item["name"] == "technitium-dns-server"), None)
    expected = f"$(POD_NAME).{domain}"
    if dns is None or not any(item.get("name") == "DNS_SERVER_DOMAIN" and item.get("value") == expected for item in dns.get("env", [])):
        fail("managed cluster domain cannot change during an upgrade")
    print(f"managed cluster guard: {previous} to {requested} members permitted")
else:
    print("managed cluster guard: no existing managed StatefulSet")
