# Incus Longhorn test environment

An additive alternative to an existing Vagrant/libvirt cluster. OpenTofu owns
Incus infrastructure; Ansible owns guest software and Kubernetes services.
It has been live-validated on one host; see [live validation](docs/live-validation.md).
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
| `ansible/roles/longhorn` | Released Helm chart or master-head manifest, runtime settings, default backup target |
| `cluster.yml` | Cluster requirements: Ubuntu, K3s, Longhorn, IP family, storage network |
| `scripts/lab.py` | Spec validation, guarded lifecycle, image import, inventory generation |
| `tests/` | Offline tests and explicitly applied live volume smoke manifests |

Cloud-init only installs Python and configures initial NICs. Ansible uses
`community.general.incus` and the guest agent, not SSH. A broken guest NIC
therefore need not prevent provisioning or diagnosis.

## Host prerequisites

- Linux/KVM, OS-managed Incus, an initialized daemon, an existing storage pool,
  and permission to use the local Incus socket.
- OS-managed OpenSSL, iproute2, and `mkisofs` (`cdrtools` on Arch/Omarchy);
  normal Internet/DNS access for downloads.
- mise installed and activated; kubectl for optional live acceptance checks.

The automated lab tasks never initialize Incus or create a storage pool. Choose
storage placement and non-overlapping subnets deliberately before applying.
Use an SSD-backed block-capable pool with enough actual free space. Incus
itself and its host runtime dependencies remain OS-managed.

Development tools are mise-managed: Python 3.13.16, OpenTofu 1.13.1,
ShellCheck 0.11.0, and Helm 4.3.0. Python dependencies use only standard-library
`python -m venv` plus pip. No uv, Poetry, pipx, or pip-tools is needed.

See [Arch/Omarchy host setup](docs/host-setup.md) for manual service/access
setup, a NoCoW directory pool, recovery from a pre-created pool directory,
and interface-wide UFW forwarding rules for trusted lab VMs.

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

Describe the cluster in one ignored file:

```sh
cp cluster.yml.example cluster.yml
```

| Key | Values | Default |
| --- | --- | --- |
| `ubuntu` | Ubuntu release, quoted, e.g. `"24.04"` | `"24.04"` |
| `image_source` | `images` (`images:ubuntu/<release>/cloud`) or `official` (Canonical image, see below) | `images` |
| `k3s` | K3s release, e.g. `v1.34.2+k3s1` | `v1.34.2+k3s1` |
| `longhorn` | `vX.Y.Z` (Helm chart) or `master-head` (master branch manifest) | `v1.13.0` |
| `ip_family` | `ipv4-single`, `ipv6-single`, `ipv4-dual` (IPv4 first), `ipv6-dual` (IPv6 first) | `ipv4-dual` |
| `storage_network` | `true` / `false`: Multus storage network for Longhorn | `false` |

Flags override the file for one run without changing it: `--ubuntu`,
`--image-source`, `--k3s`, `--longhorn`, `--ip-family`, and
`--storage-network` / `--no-storage-network`. For example:

```sh
mise run up --longhorn master-head --ip-family ipv6-dual
```

Overrides are not saved. Repeat them on a later `provision` or `verify`, or put
them in `cluster.yml`; a K3s or Longhorn downgrade from the last provisioned
version is refused. `scripts/lab.py` validates the spec and passes
it to OpenTofu as a `-var-file` and to Ansible as the last extra-vars file.

Changing `ubuntu`, `image_source`, `ip_family`, or `storage_network`, or
switching `longhorn` between a release and `master-head`, needs a fresh
cluster. Once a cluster exists, `plan`, `apply`, `up`, and `provision`
refuse such a change up front and say which keys changed. K3s and Longhorn
release changes are applied by `provision`; check upstream upgrade support
first. V2 needs a 6.7+ guest kernel, so Ubuntu 22.04 fails provisioning while
V2 is enabled.

Advanced settings stay in optional ignored files:

```sh
cp infra/terraform.tfvars.example infra/terraform.tfvars   # project, pool, sizes, CIDRs, worker count
cp ansible/vars.yml.example ansible/vars.local.yml         # backup target, hugepages, install_longhorn
```

See `infra/variables.tf` and `ansible/group_vars/all.yml` for every setting.

### Default topology

- Project `lh-incus`; existing pool `default`; Ubuntu 24.04 cloud VM image.
- One server: 2 CPUs / 4 GiB; three workers: 4 CPUs / 6 GiB each.
- Per-node root 30 GiB, K3s 30 GiB, logs 10 GiB.
- Per-worker V1 filesystem disk 40 GiB and optional V2 raw disk 40 GiB.
- K3s `v1.34.2+k3s1`, Longhorn `v1.13.0`, `ipv4-dual`, no storage network, Garage backup target.
- V2 enabled with AIO virtual disks and hugepages disabled; configurable.
- Optional shared host directory at `/incus_shared`; never use it for replicas.

A released `longhorn` version such as `v1.13.0` installs that chart from
https://charts.longhorn.io. Only
`master-head` uses a manifest: the role applies `deploy/longhorn.yaml` from the
`longhorn/longhorn` master branch, which deploys nightly `master-head` images.
Before the first apply it merges this lab's default settings and replica count
into the manifest ConfigMaps, then saves the applied manifest to
`shared/longhorn-manifest.yml`. The image tags move nightly, so record image
digests with any result.

This is 22 GiB guest RAM and 520 GiB **virtual** disk capacity, not a promise
that the pool reserves or has that space. Reduce `disk_sizes`/`root_size` for
a small pool, and monitor pool usage; thin allocation can still exhaust it.
`image_source: images` follows the moving `images:ubuntu/<release>/cloud`
alias. On hosts hit by the Incus 7.5.1 cloud-image boot loop, use
`image_source: official` and `mise run image:import`; see the
[official Ubuntu image fallback](docs/official-ubuntu-image.md).

### Networking

| Purpose | Default |
| --- | --- |
| Management, `lhmgmt0` | `172.28.155.0/24`, DHCP/static reservations, IPv4 NAT |
| Cluster, `lhstorage0` | `172.28.156.0/24`, `fd00:1c05:156::/64`, static, no NAT/RA/DHCP |
| Storage pods, optional | `10.76.0.0/16`, `fd00:1c05:168::/48`, per-node routed subnets |
| Kubernetes pods | `10.42.0.0/16`, `fd42::/48`, selected families only |
| Kubernetes services | `10.43.0.0/16`, `fd43::/112`, selected families only |

`ip_family` selects `ipv4-single`, `ipv6-single`, `ipv4-dual` (IPv4 first), or
`ipv6-dual` (IPv6 first) for cluster, pod, and service addresses.
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

## Live lifecycle

After the host/pool/subnet decisions are made and `cluster.yml` is written:

```sh
mise run up --dry-run                # optional: every check and the plan, no changes
mise run up                          # init, apply, provision, verify
kubectl get nodes -o wide             # with KUBECONFIG set as below
```

`up` runs the individual steps, which remain available: `init`, `apply`,
`provision`, and `verify`. All of them read `cluster.yml` and accept the same
per-run flags. `--dry-run` works with `up`, `apply`, `down`, and `destroy`;
`plan` is the same as `apply --dry-run`.

`apply` (and `up`) builds a fresh plan and runs every check before changing
anything. Preflight rejects subnet collisions, existing project/network name
collisions, and the `default` Incus project. Any deletion or replacement is
refused, including worker removal or a network-layout switch, and so are new VMs
on existing data volumes. With `--dry-run` it stops after these checks and
prints the planned changes. Do not use direct `tofu apply` to bypass the
helper's checks.

`verify` fails on infrastructure, Kubernetes, and configuration problems: K3s
services, mounts, Kubernetes node readiness, the Longhorn manager, and the
storage-network and hugepage settings. Longhorn Node CR status only produces
warnings, because a non-green node or disk can be exactly the Longhorn bug under
test. It waits up to 5 minutes for disks to become ready, then prints a
`WARNING` listing every node or disk condition that is not True, every
missing Node CR, and every missing disk. Two known false negatives in these VMs
are not reported: `HugePagesAvailable` while V2 hugepages are disabled, and
`HealthDataCollected` because virtual disks have no SMART support. The
reasons are commented in `ansible/verify.yml`. Provisioning also waits up to 5
minutes for every Longhorn disk to become ready and warns, rather than fails,
if one does not.

### Access

Each provision writes the kubeconfig to `shared/k3s.config` (mode 0600). Its
server address is the control plane on the lab bridge, reachable from the host.
Its cluster, user, and context are named after the project (`lh-incus`), not
K3s's `default`, so it can share a `KUBECONFIG` list with other clusters:

```sh
export KUBECONFIG=/path/to/this/repo/shared/k3s.config:$HOME/.kube/other-cluster.config
kubectl config use-context lh-incus
```

The file is replaced on every provision and removed by `down`. kubectl skips
missing files in the list.

There is no SSH. Shells and commands go through the Incus guest agent:

```sh
mise run shell                       # root shell on the server
mise run shell -- worker2            # or worker<N>, or a full instance name
incus exec local:lh-incus-server --project lh-incus -- bash
incus exec local:lh-incus-worker2 --project lh-incus -- journalctl -u k3s-agent -f
incus file pull local:lh-incus-worker3/var/log/syslog . --project lh-incus
```

`mise run shell` runs `incus shell`, Incus's built-in alias for
`exec ... -- su -l`. Set `INCUS_PROJECT=lh-incus` in your environment to drop
`--project` from raw incus commands. If a guest's agent is not running, for
example during a boot problem, read its serial console with
`incus console local:<vm> --project lh-incus --show-log`. The cloud image has
no password login, so the console is for logs only.

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
`shared/longhorn-values.yml` (master-head: `shared/longhorn-manifest.yml`). Tokens, credentials, and test TLS keys persist
under ignored `.state/` for the life of a cluster; `down` deletes them, and a
new cluster gets new ones. Provisioning an existing cluster without its K3s
token is refused rather than resetting it. The test
certificate lasts one year; renew it explicitly before expiry.

Garage/Nginx/NFS images are digest-pinned. Garage and NFS use distinct names
and retain their data on the server's K3s disk across pod restarts. Switching
targets does not garbage-collect the old target. These are single-node test
stores, not independent disaster-recovery backups; destroying the cluster
also destroys the backup data. NFS runs privileged inside its disposable VM.

### Teardown and fresh creation

```sh
mise run down --dry-run   # list the VMs, resources, and local files that would go
mise run down             # same as mise run destroy; like vagrant destroy -f
```

The cluster is disposable, so `down` asks nothing and keeps nothing. It
force-stops running VMs, deletes the managed VMs, **all their data volumes**,
owned bridges, profile, and project, and then removes the per-cluster local
files: inventory, kubeconfig, rendered Longhorn values or manifest, applied
spec, K3s token, backup credentials, and test TLS keys. It acts only on
resources in OpenTofu state, refuses the `default` project, and does nothing
when no cluster exists. The pre-existing pool, imported images, chart cache,
and unrelated resources stay.

Every cluster starts from scratch. When `apply` or `up` creates the project,
it first deletes any per-cluster local files left by an older cluster. A plan
that would attach existing data volumes to a newly created VM is refused, and
an existing VM is never replaced in place, so guests always start from blank
data disks. Never delete OpenTofu state as a reset procedure; use `down`.

See [live acceptance checks](docs/acceptance.md) before adopting this as a
replacement for another environment. The [initial live validation](docs/live-validation.md)
passed V1/V2 I/O and storage networking with both v1.13.0 and master-head.

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
