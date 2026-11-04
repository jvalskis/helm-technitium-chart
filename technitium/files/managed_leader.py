# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Lease ownership for the primary observer; never reacquire during reconciliation."""

from datetime import datetime, timezone
import time
import uuid

from managed_api import ApiError, HttpError


class LostLeadership(ApiError):
    pass


class Leader:
    duration = 30
    renew_deadline = 20  # Leave time for the bounded Kubernetes API requests.

    def __init__(self, kube, name, identity, stop):
        self.kube, self.name, self.identity, self.stop = kube, name, identity, stop
        self.term = identity
        self.held = False
        self.renewed = 0
        self.uid = None
        self.observed_version = None
        self.observed_at = 0

    def invalidate(self):
        # A timed-out PATCH may still arrive. Fence again before the next observation.
        self.held = False
        self.term = f"{self.identity}/{uuid.uuid4()}"

    def _observe(self):
        lease = self.kube.lease(self.name)
        version = (lease["metadata"]["uid"], lease["metadata"]["resourceVersion"])
        if version != self.observed_version:
            self.observed_version = version
            self.observed_at = time.monotonic()
        return lease

    def _write(self, lease, holder):
        metadata, spec = lease["metadata"], dict(lease.get("spec", {}))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if spec.get("holderIdentity") != holder:
            spec["acquireTime"] = now
            spec["leaseTransitions"] = spec.get("leaseTransitions", 0) + 1
        spec.update(holderIdentity=holder, leaseDurationSeconds=self.duration, renewTime=now)
        self.kube.lease(self.name, patch=[
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            {"op": "add", "path": "/spec", "value": spec},
        ])
        self.uid = metadata["uid"]

    def acquire(self):
        self.held = False
        if self.stop.is_set():
            return False
        lease = self._observe()
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity")
        # Observe another holder for a full lease period. Do not rely on clock synchronization.
        if holder and holder != self.identity:
            duration = spec.get("leaseDurationSeconds", self.duration)
            if time.monotonic() - self.observed_at < duration:
                return False
        started = time.monotonic()
        try:
            self._write(lease, self.identity)
        except HttpError as exc:
            if exc.status in (409, 422):
                return False
            raise
        self.held, self.renewed = True, started
        return True

    def renew(self):
        if self.stop.is_set() or not self.held or time.monotonic() - self.renewed >= self.renew_deadline:
            self.held = False
            raise LostLeadership("observation outlived leadership; retry with fresh state")
        started = time.monotonic()
        lease = self._observe()
        if (lease["metadata"]["uid"] != self.uid
                or lease.get("spec", {}).get("holderIdentity") != self.identity):
            self.held = False
            raise LostLeadership("another observer owns the Lease")
        try:
            self._write(lease, self.identity)
        except ApiError:
            self.held = False
            raise
        self.renewed = started
        if self.stop.is_set():
            raise LostLeadership("observer is stopping")

    def release(self):
        if self.held:
            lease = self._observe()
            if lease["metadata"]["uid"] == self.uid and lease.get("spec", {}).get("holderIdentity") == self.identity:
                self._write(lease, "")
        self.held = False
