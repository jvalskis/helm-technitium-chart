# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Small shared clients and validated membership for the managed chart scripts."""

from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    """A safe diagnostic which never includes credentials or server response bodies."""


class TransportError(ApiError):
    pass


class AuthenticationError(ApiError):
    pass


class CertificateError(ApiError):
    pass


class SessionExpired(AuthenticationError):
    pass


class InvalidResponse(ApiError):
    pass


class DeadlineExceeded(ApiError):
    pass


class HttpError(ApiError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirects must never carry login credentials or bearer tokens elsewhere.
        raise ApiError("API redirects are not permitted")


class PeerHTTPSConnection(http.client.HTTPConnection):
    """Connect to a stable Service IP, but authenticate the member's DNS identity."""

    def __init__(self, host, *, context, server_name, **kwargs):
        super().__init__(host, **kwargs)
        self.context, self.server_name = context, server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        try:
            self.sock = self.context.wrap_socket(sock, server_hostname=self.server_name)
        except BaseException:
            sock.close()
            raise


class PeerHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context, server_name):
        super().__init__(context=context)
        self.context, self.server_name = context, server_name

    def https_open(self, request):
        def connection(host, **kwargs):
            return PeerHTTPSConnection(host, context=self.context, server_name=self.server_name, **kwargs)
        return self.do_open(connection, request)


def open_request(request, *, context, timeout, server_name=None):
    handler = PeerHTTPSHandler(context, server_name) if server_name else urllib.request.HTTPSHandler(context=context)
    # Peer addresses are in-cluster; a proxy must not replace the connection target.
    handlers = [handler, NoRedirect()]
    if server_name:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def request_json(url, *, headers=None, form=None, patch=None, context=None, timeout=5, server_name=None):
    headers = dict(headers or {})
    data, method = None, "GET"
    if form is not None:
        data, method = urllib.parse.urlencode(form).encode(), "POST"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if patch is not None:
        data, method = json.dumps(patch).encode(), "PATCH"
        headers["Content-Type"] = "application/json-patch+json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with open_request(request, context=context, timeout=timeout, server_name=server_name) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code) from None
    except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise CertificateError("TLS certificate verification failed; check CA trust and member DNS SANs") from None
        raise TransportError(f"request failed ({type(exc).__name__})") from None
    except (ValueError, UnicodeError):
        raise InvalidResponse("API response is not valid JSON") from None
    if not isinstance(result, dict):
        raise InvalidResponse("API response must be an object")
    return result


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self, limit):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("operation deadline exceeded")
        return min(limit, remaining)

    def pause(self, seconds=3):
        time.sleep(self.remaining(seconds))

    def retry(self, operation):
        while True:
            self.remaining(1)
            try:
                return operation()
            except (TransportError, InvalidResponse):
                self.pause()


class TechnitiumClient:
    def __init__(self, ip, port, username, password="", *, token="", totp="", scheme="https",
                 verify_tls=True, ca_file=None, server_name=None):
        self.base = f"{scheme}://{ip}:{port}"
        self.username, self.password, self.totp = username, password, totp
        self.token, self.fixed_token = token, bool(token)
        self.server_name = server_name
        self.context = ssl.create_default_context()
        if ca_file:
            self.context.load_verify_locations(cafile=ca_file)
        if not verify_tls:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def _request(self, path, *, form=None, token=None, timeout=5):
        try:
            result = request_json(self.base + path, form=form,
                                  headers={"Authorization": f"Bearer {token}"} if token else {},
                                  context=self.context, timeout=timeout, server_name=self.server_name)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthenticationError(f"authentication denied for {path}") from None
            if exc.status in (408, 429) or exc.status >= 500:
                raise TransportError(str(exc)) from None
            raise
        status = result.get("status")
        if status == "invalid-token":
            raise SessionExpired("invalid or expired session token")
        if status != "ok":
            if path == "/api/user/login":
                raise AuthenticationError("login rejected; check username, password and TOTP")
            raise ApiError(f"{path} returned an error")
        return result

    def login(self, deadline):
        if not self.token:
            form = {"user": self.username, "pass": self.password, "includeInfo": "false"}
            if self.totp:
                form["totp"] = self.totp
            result = self._request("/api/user/login", form=form, timeout=deadline.remaining(5))
            token = result.get("token")
            if not isinstance(token, str) or not token:
                raise InvalidResponse("login returned no session token")
            self.token = token

    def call(self, path, deadline, *, form=None, timeout=5):
        self.login(deadline)
        try:
            return self._request(path, form=form, token=self.token, timeout=deadline.remaining(timeout))
        except SessionExpired:
            if self.fixed_token:
                raise
            self.token = ""
            self.login(deadline)
            return self._request(path, form=form, token=self.token, timeout=deadline.remaining(timeout))

    def state(self, deadline):
        result = self.call("/api/admin/cluster/state", deadline)
        return ClusterState.parse(result.get("response"))


@dataclass(frozen=True)
class Node:
    id: int
    name: str
    url: str
    addresses: tuple
    role: str

    @classmethod
    def parse(cls, node):
        if not isinstance(node, dict):
            raise InvalidResponse("cluster node must be an object")
        node_id, name, url = node.get("id"), node.get("name"), node.get("url")
        addresses, role = node.get("ipAddresses"), node.get("type")
        if (type(node_id) is not int or not isinstance(name, str) or not name
                or not isinstance(url, str) or not url.startswith("https://")
                or role not in ("Primary", "Secondary")
                or not isinstance(addresses, list) or not addresses
                or not all(isinstance(ip, str) and ip for ip in addresses)):
            raise InvalidResponse("invalid cluster node identity")
        return cls(node_id, name, url, tuple(sorted(addresses)), role)


@dataclass(frozen=True)
class ClusterState:
    initialized: bool
    name: str
    domain: str
    nodes: tuple

    @classmethod
    def parse(cls, state):
        if not isinstance(state, dict) or type(state.get("clusterInitialized")) is not bool:
            raise InvalidResponse("missing cluster initialization state")
        name = state.get("dnsServerDomain")
        if not isinstance(name, str) or not name:
            raise InvalidResponse("missing server identity")
        if not state["clusterInitialized"]:
            return cls(False, name, "", ())
        domain, nodes = state.get("clusterDomain"), state.get("clusterNodes")
        if not isinstance(domain, str) or not domain or not isinstance(nodes, list) or not nodes:
            raise InvalidResponse("missing cluster membership")
        nodes = tuple(sorted((Node.parse(node) for node in nodes), key=lambda node: node.name))
        if len({node.name for node in nodes}) != len(nodes) or len({node.id for node in nodes}) != len(nodes):
            raise InvalidResponse("duplicate cluster node identity")
        return cls(True, name, domain, nodes)

    @property
    def members(self):
        return {node.name: node for node in self.nodes}

    @property
    def primary(self):
        primaries = [node for node in self.nodes if node.role == "Primary"]
        if len(primaries) != 1:
            raise InvalidResponse("expected exactly one primary")
        return primaries[0]


class KubernetesClient:
    def __init__(self, namespace):
        self.namespace = namespace
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base = f"https://{host}:{port}"
        self.token_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def request(self, path, *, patch=None):
        if patch is not None:
            path += "?fieldManager=technitium-primary-observer"
        try:
            return request_json(self.base + path, patch=patch, context=self.context,
                                headers={"Authorization": f"Bearer {self.token_file.read_text()}"})
        except HttpError as exc:
            if exc.status in (408, 429) or exc.status >= 500:
                raise TransportError(str(exc)) from None
            raise

    def pod(self, name, *, patch=None):
        return self.request(f"/api/v1/namespaces/{self.namespace}/pods/{name}", patch=patch)

    def lease(self, name, *, patch=None):
        return self.request(f"/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases/{name}", patch=patch)
