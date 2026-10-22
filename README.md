# Incus Longhorn test environment

An additive alternative to an existing Vagrant/libvirt cluster. OpenTofu owns
Incus infrastructure; Ansible owns guest software and Kubernetes services.
The implementation is locally validated, not yet live-validated with VMs.
Do not retire the existing environment until the live acceptance checks pass.

## Ownership and layout

| Path | Responsibility |
| --- | --- |
| `infra/` | Dedicated project/profile, two bridges, VMs, NICs, block volumes, inventory |
| `ansible/roles/node` | Ubuntu packages, kernel modules, services, sysctl |
| `ansible/roles/disks` | Stable disk identities, non-forced ext4 creation, UUID mounts |
| `ansible/roles/k3s` | Checksum-verified K3s binary, configuration, systemd services |
| `ansible/roles/storage_network` | Optional guest ipvlan L3 routes restored at boot |
| `ansible/roles/cluster` | Node labels/disk annotations and optional Multus |
| `ansible/roles/backup` | Garage S3/TLS or NFS test backup store |
| `ansible/roles/longhorn` | Pinned chart, runtime settings, default backup target |
| `scripts/lab.py` | Guarded lifecycle and inventory generation |
| `tests/` | Offline tests and explicitly applied live volume smoke manifests |

Cloud-init only installs Python and configures initial NICs. Ansible uses
`community.general.incus` and the guest agent, not SSH. A broken guest NIC
therefore need not prevent provisioning or diagnosis.

## Host prerequisites

- Linux/KVM, OS-managed Incus, an initialized daemon, an existing storage pool,
  and permission to use the local Incus socket.
- OS-managed OpenSSL and iproute2; normal Internet/DNS access for downloads.
- mise installed and activated; kubectl for optional live acceptance checks.

This repository never initializes Incus or creates a storage pool. Choose
storage placement and non-overlapping subnets deliberately before applying.
Use an SSD-backed block-capable pool with enough actual free space. Incus
itself and its host runtime dependencies remain OS-managed.

Development tools are mise-managed: Python 3.13.16, OpenTofu 1.13.1,
ShellCheck 0.11.0, and Helm 4.3.0. Python dependencies use only standard-library
`python -m venv` plus pip. No uv, Poetry, pipx, or pip-tools is needed.

## Local setup and validation: no VMs

Run from the repository root:

```sh
mise trust mise.toml
mise install
mise run bootstrap
mise run check
mise run validate
mise run validate:charts
```

`check` tests the project-local toolchain and Ansible localhost access.
`validate` initializes the provider cache, checks HCL, runs mock infrastructure
and Python tests, renders guest templates, runs ShellCheck, and lints Ansible.
It never connects to Incus or Kubernetes. `validate:charts` additionally
fetches the pinned Helm charts and renders them locally, without Kubernetes.
An initial validation requires Internet access to install the pinned provider.

The standard CPython venv at `.venv/` is independent of any parent Python
environment. Collections and Ansible files live under `.ansible/`; Helm files,
mock inventory, and generated test artifacts live under `.state/`.

## Configuration

```sh
cp infra/terraform.tfvars.example infra/terraform.tfvars
cp ansible/vars.yml.example ansible/vars.local.yml
```

Both local copies are ignored. Review `infra/variables.tf` for infrastructure
settings, and `ansible/group_vars/all.yml` for guest/cluster defaults.
Guest version or package changes normally require only `provision`.
Infrastructure changes require a reviewed OpenTofu plan.

### Default topology

- Project `lh-incus`; existing pool `default`; Ubuntu 24.04 cloud VM image.
- One server: 2 CPUs / 4 GiB; three workers: 4 CPUs / 6 GiB each.
- Per-node root 30 GiB, K3s 30 GiB, logs 10 GiB.
- Per-worker V1 filesystem disk 40 GiB and optional V2 raw disk 40 GiB.
- K3s `v1.34.2+k3s1`, Longhorn chart `1.13.0`, Garage backup target.
- V2 enabled with AIO virtual disks and hugepages disabled; configurable.
- Optional shared host directory at `/incus_shared`; never use it for replicas.

This is 22 GiB guest RAM and 520 GiB **virtual** disk capacity, not a promise
that the pool reserves or has that space. Reduce `disk_sizes`/`root_size` for
a small pool, and monitor pool usage; thin allocation can still exhaust it.
The image alias may advance: pin an image fingerprint for repeatable runs.

### Networking

| Purpose | Default |
| --- | --- |
| Management, `lhmgmt0` | `172.28.155.0/24`, DHCP/static reservations, IPv4 NAT |
| Cluster, `lhstorage0` | `172.28.156.0/24`, `fd00:1c05:156::/64`, static, no NAT/RA/DHCP |
| Storage pods, optional | `10.76.0.0/16`, `fd00:1c05:168::/48`, per-node routed subnets |
| Kubernetes pods | `10.42.0.0/16`, `fd42::/48`, selected families only |
| Kubernetes services | `10.43.0.0/16`, `fd43::/112`, selected families only |

`network_stack` is `ipv4`, `ipv6`, `dual` (IPv4-first), or `dual6` (IPv6-first).
Management remains IPv4 in every mode. IPv6 is local ULA connectivity; this
configuration does not establish public IPv6 egress. Host/VPN, Incus, pod,
service, and optional storage ranges must not overlap.

Managed bridges have project-prefixed names in the default Incus project;
VMs/profiles/custom volumes are in the dedicated project. Existing networks,
profiles, pools, and Vagrant resources are not adopted or modified. Applying
bridges does cause Incus-managed host networking/firewall changes, so review
firewall coexistence before approving it. Guarded helpers support local Incus
only; remote-host routing is not implemented.

Optional Multus uses a pinned RKE2 chart, flannel IPAM, ipvlan L3, and guest
routes on `lhstorage0`. The guest routing service runs before K3s on boot.
The second bridge permits non-node source addresses for routed storage pods.
MTU, actual storage-pod traffic, firewall behavior, and reboot recovery remain
live acceptance requirements, not claims established by mock tests.

## Live lifecycle: explicit approval required

After the host/pool/subnet decisions are made:

```sh
mise run init
mise run plan
mise run apply -- --confirm lh-incus
mise run provision
mise run verify
export KUBECONFIG="$PWD/shared/k3s.config"
kubectl get nodes -o wide
```

`plan` only reads Incus and host routes and writes a local plan. Preflight
rejects subnet collisions and existing project/network name collisions.
`apply` builds a fresh plan, requires the exact project name, and refuses any
deletion/replacement, including worker removal or a network-layout switch.
It creates infrastructure only; guest provisioning is a separate operation.
Do not use direct `tofu apply` to bypass the helper's checks accidentally.

`provision` derives inventory from applied outputs every time. It does not
wipe disks, force filesystem creation, reinstall/reset an existing cluster,
or delete obsolete backup stores. It may restart services or upgrade pinned
software when configuration changes. Detach volumes before changing engine
settings. Longhorn may reject unsafe runtime setting changes.

Disk names come from Incus device serials, not `/dev/vdb` enumeration. The
filesystem role checks the expected serial before formatting, refuses a
conflicting mount, migrates existing directory contents before first mounting,
and persists UUID mounts. An existing filesystem is not forcibly overwritten;
a formatted/partitioned V2 disk is rejected, not wiped. Growing a block volume
does not currently automate guest filesystem expansion.

Kubeconfig is `shared/k3s.config`; rendered Helm values are
`shared/longhorn-values.yml`. Tokens, credentials, and test TLS keys persist
under ignored `.state/`. Preserve that directory and OpenTofu state together;
losing the local K3s token is not an authorized cluster reset. The test
certificate lasts one year; renew it explicitly before expiry.

Garage/Nginx/NFS images are digest-pinned. Garage and NFS use distinct names
and retain their data on the server's K3s disk across pod restarts. Switching
targets does not garbage-collect the old target. These are single-node test
stores, not independent disaster-recovery backups; destroying the cluster
also destroys the backup data. NFS runs privileged inside its disposable VM.

### Destructive teardown

```sh
mise run destroy -- --confirm lh-incus
```

This explicitly deletes managed VMs, **all their managed data volumes**, owned
bridges, profile, and project. It preserves the pre-existing pool and unrelated
resources. It removes the stale inventory/kubeconfig but retains local secret
files. It does not force-remove manually added project resources. Never delete
OpenTofu state as a reset procedure. There is intentionally no implicit reset.

See [live acceptance checks](docs/acceptance.md) before adopting this as a
replacement for another environment. No live apply/provision/verify/destroy
has been performed as part of the implementation.

## Dependency maintenance and version control

- `requirements.in`: direct Python dependencies.
- `requirements.txt`: tested direct/transitive pip pins for Linux CPython.
- `collections/requirements.yml`: pinned Ansible collections.
- `infra/.terraform.lock.hcl`: tracked provider checksums; review any update.

Update Python deliberately inside the project venv, inspect
`python -m pip freeze --local`, then update the lock and rerun checks. Update
collections/tools/chart/image pins separately and retest their compatibility.

This is a standalone colocated Git/Jujutsu repository. Inspect with `jj status`,
`jj diff`, and `jj log`. The configured SSH remote is
`git@github.com:COLDTURNIP/vagrant-incus-minimum.git`; no task pushes changes.
Local state, plans, credentials, venv, caches, and generated artifacts are
ignored. Commit public configuration and lock files only.

References: [Incus disk devices](https://linuxcontainers.org/incus/docs/main/reference/devices_disk/),
[Incus provider](https://search.opentofu.org/provider/lxc/incus/latest),
[longhorn/longhorn](https://github.com/longhorn/longhorn), and
[RKE2 Multus charts](https://github.com/rancher/rke2-charts).
