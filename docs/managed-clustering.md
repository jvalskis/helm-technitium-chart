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

- **Install:** keep `cluster.managed.initialize: false` in saved values; pass
  `--set cluster.managed.initialize=true` for the first install only. Upgrades
  never initialize an unclustered server.
- **Expand:** increase `cluster.managed.secondaries`. Hooks verify saved
  membership and join new members; scale-down, naming and domain changes are
  rejected.
- **Upgrade:** use `helm upgrade --wait` with hooks enabled. Pod changes roll
  highest ordinal first, regardless of primary role. The post-hook checks
  membership after the rollout. Render-only deployment and `--no-hooks` are
  unsupported.
- **Traffic:** TCP readiness does not prove membership or replication. Keep
  client traffic off `-dns` during installation, expansion and empty-PVC
  recovery; new Pods can receive queries before joining.
- **Failed install:** if ordinal zero is unclustered, initialize it in
  Technitium's UI with the configured domain and its peer Service IP, then retry
  Helm. Conflicting identities, foreign membership or lost member state require
  manual recovery.

## Promote a member

1. Pause client traffic; promotion detaches the former primary while its Pod
   may still receive queries.
2. Promote the chosen secondary in Technitium.
3. Rejoin the former primary through the UI using the new primary's member URL
   and peer Service IP. Example: `https://dns-technitium-cluster-1.cluster.example.com:53443/`
   and the IP of `dns-technitium-cluster-1-web`.
4. Set the rejoining member's own node IP to its `-cluster-N-web` Service IP.
   Do not use a Pod IP or the shared `-web` address. If stale zones block rejoin,
   verify and back up their current copies on the new primary before deleting
   stale copies from the former primary.
5. Run the [cutover checks](#verify-before-client-cutover) before resuming traffic.

Helm rollback does not undo promotion or persisted data changes.

## Migrate a standalone primary

1. Back up and stop the old server. Copy, clone or rebind its volume to
   `config-storage-<StatefulSet name>-0`; the chart does not adopt arbitrary claim
   names. Never mount the same data writable in both servers.
2. Start with `secondaries: 0`. Align the saved identity and node IP with ordinal
   zero and its `-0-web` Service; environment variables do not replace saved identity.
3. For HTTP-only source data, temporarily enable `cluster.managed.bootstrapHttp`.
   Disable it after enabling HTTPS.
4. For a new release, use the first-install flag above. For an existing release,
   initialize through Technitium's UI, then rerun Helm.
5. Verify migrated data and updates before expanding. Enroll imported zones in
   the cluster catalog.

## Verify before client cutover

- **Membership:** matching domain and member lists, exactly one primary, and
  each member's own peer Service IP.
- **Replication:** required zones enrolled in the catalog; matching SOA serials
  and representative records over UDP and TCP on every member.
- **Routing:** `-web` targets the primary; a test update through it replicates
  to every member. Query the client-facing DNS endpoint over UDP and TCP.
- **Persistence:** restart members one at a time; confirm retained PVCs/data
  and repeat the checks.
