# Managed clustering

`cluster.managed.enabled` creates one `OrderedReady` StatefulSet. Ordinal zero
starts as primary; Technitium's saved cluster state determines the current
primary after that. `cluster.managed.secondaries` counts additional members, so
`0` creates one Pod. Each member has a stable name, peer Service, and retained
PVC.

## Configure

```yaml
config:
  webServiceEnableHttps: true
  webServiceUseSelfSignedCert: false
  webServiceTlsCertificatePath: /peer-tls/server/keystore.p12
cluster:
  enabled: true
  autoHttps: false
  domain: cluster.example.com
  managed:
    enabled: true
    initialize: false # override to true for the first install only
    secondaries: 2
    tls:
      verify: true # default
      caConfigMapName: local-pki-trust
      caKey: ca.crt # PEM CA bundle
      serverCertificate:
        secretName: cluster-server-tls
        key: keystore.p12
        passwordSecretName: cluster-keystore-password
        passwordKey: password
```

The server Secret must contain PKCS#12 data, with TLS verification enabled. Its
certificate must cover every member's DNS name. Set
`config.webServiceTlsCertificatePath` to an absolute nested path, such as
`/peer-tls/server/keystore.p12`, for automatic certificate reload. Existing servers
must have matching saved certificate paths and passwords. This certificate serves
the web UI and cluster API; ingress TLS is separate.

| Service | Purpose |
| --- | --- |
| `dns-technitium-cluster-N-web` | Stable peer address for member N |
| `dns-technitium-dns` | Client queries across all ready members |
| `dns-technitium-web` | Observed primary for DNS UPDATE on 53 and web/API access on 5380/53443 |

### Admin UI through ingress

The existing `ingress` values also work in managed mode:

```yaml
ingress:
  enabled: true
  className: traefik
  hosts:
    - host: dns-admin.example.com
      paths:
        - path: /
          pathType: Prefix
          port: 5380
```

The ingress forwards HTTP to `-web`. Until a primary is identified, this
route has no backend. For bootstrap or recovery,
access a member's `-cluster-N-web` Service directly. A primary change may require
logging in again. Ingress TLS configuration is unchanged.

`service.web` configures the admin Service in every mode.

## Primary routing and peer TLS

`-web` follows the primary reported by a strict majority of members, including
the primary itself. The observer uses the admin Secret by default; configure
`cluster.managed.roleObserver` for separate credentials and polling. It does not
promote or rejoin nodes. Routing can remain stale if the observer stops.

Peer API calls verify TLS against member DNS names by default
(`cluster.managed.tls.verify: true`). Supply a private CA through `caConfigMapName`
or `caSecretName`, with `caKey`; otherwise system CAs apply. Restart the observer
and members after CA rotation. Explicitly setting `verify: false` disables
verification for managed API calls and joins.

Managed mode requires IPv4 peer Service addresses, persistence and
`cluster.podNetworks`. Bootstrap zones and `persistence.existingClaim` are
unsupported. Enroll replicated zones in the cluster catalog.

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
   `https://dns-technitium-cluster-1.cluster.example.com:53443/` and the IP is
   from `dns-technitium-cluster-1-web`.
2. Set the former primary's **own node IP** to its `-0-web` Service IP. Never
   save a Pod IP or the movable `-web` Service IP as a node address. If an
   unstable address was saved, use Technitium's
   [Update Node IP Addresses API](https://github.com/TechnitiumSoftware/DnsServer/blob/v15.6.0/APIDOCS.md#update-node-ip-addresses),
   wait for membership to reflect it, and retry.
3. If a stale local copy of the cluster zone blocks the join, verify that the
   new primary has the zone and its records, back it up, then delete the stale
   copy on the **former primary only**. For self-signed TLS, select **Ignore
   Certificate Errors** in the manual join form.
4. Confirm all members agree on membership. Wait for the observer to route
   `-web` to the promoted Pod, check its endpoint and DNS answers, then
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
3. Confirm `-web` points to the observed primary. Change a test record
   through it, confirm it on every member, restart Pods one at a time, and
   repeat the checks.
