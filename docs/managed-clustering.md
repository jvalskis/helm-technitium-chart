# Managed clustering

`cluster.managed.enabled` creates one `OrderedReady` StatefulSet. Ordinal zero
starts as primary; Technitium's saved cluster state determines the current
primary after that. `cluster.managed.secondaries` counts additional members, so
`0` creates one Pod. Each member has a stable name, peer Service, and retained
PVC.

## Configure

```yaml
config:
  adminPassword: replace-with-a-synthetic-test-password
  webServiceEnableHttps: true
  webServiceUseSelfSignedCert: false
  webServiceTlsCertificatePath: /etc/dns/tls/cert.pfx
cluster:
  enabled: true
  autoHttps: false # use the PKI-issued certificate
  domain: cluster.example.test
  podNetworks: [10.244.0.0/16]
  managed:
    enabled: true
    initialize: false # override to true for the first install only
    secondaries: 2
    tls:
      verify: true # default
      caConfigMapName: local-pki-trust
      caKey: ca.crt # PEM CA bundle
```

For release `dns`, the Pods are `dns-technitium-cluster-0` through `-2`.
Provision the server PFX on each member's PVC at the configured path before
startup. Its DNS SAN must cover that member's name, for example
`dns-technitium-cluster-0.cluster.example.test` (or `*.cluster.example.test`).
The CA bundle adds trust; it does not install or issue server certificates.
Existing servers must also have their saved TLS settings aligned.

| Service | Purpose |
| --- | --- |
| `dns-technitium-cluster-N-web` | Stable peer address for member N |
| `dns-technitium-web` | Alias for ordinal zero |
| `dns-technitium-dns` | Client queries across all ready members |
| `dns-technitium-primary` | Single-member ClusterIP for DNS UPDATE on port 53 and HTTPS API writes on 53443 |

A one-replica observer Deployment reads cluster state from the peer Services.
It labels the primary Pod only when a strict majority of configured members agree
and that primary reports itself. The `-primary` Service has a fixed selector for
`technitium.io/cluster-role: primary`. The observer clears old primary labels
before assigning a new one; conflicting or insufficient reports clear all of them.
The label is absent from the StatefulSet template, so new Pods need observation.
One Kubernetes Lease coordinates overlapping observer Pods. Pod writes check
ownership, UID, resource version, and an observer annotation; takeover or an
uncertain write invalidates older requests. Replacement may wait up to 30 seconds
for Lease expiry, plus polling. RBAC permits get/patch only on this Lease and the
named member Pods. Helm owns the Service and does not manage the Lease's state.
It does not promote or rejoin members. With two members, a promotion cannot
restore writes until the former primary rejoins.

The observer reuses the chart administrator Secret by default. For a separate
read-only cluster-state user, set `roleObserver.secretName`, `username`, and
`passwordKey`. Alternatively, set `secretName` and `tokenKey` for an API token
valid on every member with Administration View permission; this avoids password
login for a user with 2FA. The observer reuses sessions and reloads changed Secret
contents. It polls every 10 seconds by default (`pollSeconds`: 5-300).
Managed hooks send `cluster.primaryNodeTotp` to login and join, but a saved code
expires; use an automation account for unattended upgrades.

Peer API calls validate certificate chains, expiry, and member DNS names by
default (`cluster.managed.tls.verify: true`). Connections use peer Service IPs
with the member name as TLS SNI. Select a CA bundle with `caConfigMapName` or
`caSecretName` (one source only); `caKey` selects its PEM data entry. Without
either source, the client image's system CA store is used. The bundle is mounted
at `/peer-ca/ca.crt` in the coordinator, observer, and managed servers; servers
use `SSL_CERT_FILE` for verified initial joins. Restart
the observer and member Pods after rotating the CA bundle. Technitium's native
DANE checks remain unchanged.

For generated self-signed certificates without a trusted CA, explicitly set
`cluster.managed.tls.verify: false`. The setting also controls
`ignoreCertificateErrors` on managed joins. HTTPS failures never trigger HTTP
fallback or credential-bearing redirects. If the observer stops or cannot patch the
Pods, a stale primary label can remain: monitor its logs and isolate an old
primary during recovery. Earlier managed previews must remove
`updateTargetOrdinal` from values before upgrading.

Use IPv4 peer Service ClusterIPs. Managed mode requires persistence and
`cluster.podNetworks`; it does not support bootstrap zones or
`persistence.existingClaim`. Create zones on the active primary and enroll them
in the cluster catalog. PVCs are named
`config-storage-<StatefulSet name>-<ordinal>` and remain after scale-down or
StatefulSet deletion. Plan a separate migration to change claim names or
storage settings.

## Install, expand, and update

A managed upgrade is `helm upgrade` with `cluster.managed.enabled: true`.
Pod template changes roll existing members automatically, highest ordinal
first, one at a time. Increasing `secondaries` creates new Pods. Kubernetes
waits for TCP readiness between members; the post-upgrade hook checks
Technitium membership after the rollout. A promoted primary may be the
highest ordinal and restart first. TCP readiness does not prove replication;
verify DNS answers and zone serials after each upgrade.

1. Keep `initialize: false` in saved values. For the first install, pass
   `--set cluster.managed.initialize=true`. Upgrades ignore this flag, but a
   later fresh install would honor a retained `true` value.
2. To add members, increase `secondaries` and run Helm with hooks enabled. The
   pre-hook rejects scale-down, naming changes, and cluster-domain changes.
   The post-hook checks Technitium's saved membership and joins new members.
3. For image or Pod template changes, run Helm with `--wait` and check the
   coordinator result, membership, replication, and DNS answers afterward.

The default TCP readiness probe allows the post-hook to run; Ready does not
mean joined. The shared `-dns` Service can route to a new Pod before the hook
verifies it. Keep production traffic away during installation, expansion, and
empty-PVC recovery. Do not use `--no-hooks` or a render-only deployment path for
managed changes.

If installation fails and the retry is an upgrade, inspect ordinal zero's
Technitium state. If it is unclustered, initialize it deliberately in the UI
with the configured domain and its `-0-web` Service IP, then retry Helm. If it
is clustered, the hook verifies the saved state. Do not use reinstall as a
recovery shortcut. Conflicting identities, foreign membership, and a member
registered in the cluster but missing local state require manual recovery.

## Promote a member

Technitium removes the former primary from membership during promotion, but its
Pod can remain TCP Ready. Keep client queries and updates off the managed
Services until recovery is complete.

1. Promote the chosen secondary in Technitium. Rejoin the former primary through
   the UI using the new primary's cluster node URL and peer Service IP. For
   ordinal one in the example, the URL is
   `https://dns-technitium-cluster-1.cluster.example.test:53443/` and the IP is
   from `dns-technitium-cluster-1-web`.
2. Set the former primary's **own node IP** to its `-0-web` Service IP. Never
   save a Pod IP or the movable `-primary` Service IP as a node address. If an
   unstable address was saved, use Technitium's
   [Update Node IP Addresses API](https://github.com/TechnitiumSoftware/DnsServer/blob/v15.6.0/APIDOCS.md#update-node-ip-addresses),
   wait for membership to reflect it, and retry.
3. If a stale local copy of the cluster zone blocks the join, verify that the
   new primary has the zone and its records, back it up, then delete the stale
   copy on the **former primary only**. For self-signed TLS, select **Ignore
   Certificate Errors** in the manual join form.
4. Confirm all members agree on membership. Wait for the observer to route
   `-primary` to the promoted Pod, check its endpoint and DNS answers, then
   restore client traffic.

Helm rollback does not reverse Technitium promotion or persisted zone changes.

## Migrate a standalone primary

The chart does not move or rename a standalone PVC. Prepare
`config-storage-<StatefulSet name>-0` by copying, cloning, or rebinding the old
volume after stopping the old server. Check the storage driver's behavior and
keep an independent backup. Do not mount the same writable data twice.

Align the saved server identity and node IP with ordinal zero and its `-0-web`
Service; environment variables do not rename persisted identity. Start with
`secondaries: 0` and verify data and updates before expanding. A new Helm
release can use `initialize: true` on its first install. Converting an existing
release is an upgrade: initialize through Technitium's UI, then rerun Helm.
If ordinal zero uses the old standalone server's saved configuration and HTTPS
is still disabled, temporarily set `cluster.managed.bootstrapHttp: true`. This
lets the hook inspect that member over HTTP during migration. Reset it to
`false` after enabling HTTPS. Other members always use HTTPS.

## Verify before client cutover

1. Every member reports the same domain and member list, exactly one primary,
   and its own stable peer Service IP.
2. Intended zones are enrolled in the catalog. Check serials and representative
   records over UDP and TCP against each member's peer Service.
3. Confirm `-primary` points to the observed primary. Change a test record
   through it, confirm it on every member, restart Pods one at a time, and
   repeat the checks.
