# Arch/Omarchy host setup

These are manual operator actions, not actions performed by the lab tasks.
They change host services, storage configuration, or firewall rules. Review
and run only the sections you intend to apply. No step below creates VMs.
OpenTofu, not host initialization, will create the dedicated lab bridges.

## Packages and local access

Incus and its host runtime dependencies are OS-managed; libvirt is not needed.
Ensure QEMU/KVM, OVMF firmware, virtiofsd, dnsmasq, iproute2, OpenSSL, rsync,
and Btrfs utilities are available. On Arch/Omarchy, `cdrtools` supplies the
`mkisofs` helper used for VM cloud-init configuration drives.

```sh
omarchy pkg add incus cdrtools edk2-ovmf virtiofsd iproute2 openssl rsync btrfs-progs
sudo usermod -aG incus-admin "$USER"
sudo systemctl enable --now incus.service
```

Fully log out and back in before continuing. Merely opening a new terminal
in the same desktop session does not refresh group membership. Membership
in `incus-admin` provides administrative control over Incus.

```sh
id -nG
systemctl is-active incus.service incus.socket
incus info
incus config get core.https_address
```

Expect `incus-admin` in the groups, both units active, and successful local
access without sudo. Leave `core.https_address` unset; remote API exposure is
not required. The service starts its required socket even if the socket unit
itself is not separately enabled.

## Directory pool on an existing Btrfs filesystem

This example is for disposable functional testing, not a storage-performance
baseline. The `dir` driver supports file-backed VM disks and custom block
volumes, but does not provide optimized cloning/snapshots. A dedicated NoCoW
Btrfs subvolume avoids the Btrfs storage driver's VM quota behavior. NoCoW
also disables data checksumming/compression for newly created files there;
it does not change those properties for unrelated host files.

The source subvolume shares the host filesystem's free space; it does not
reserve the lab's virtual disk capacity. No block device is formatted.

Keep these two paths distinct:

- Source you prepare: `/var/lib/incus-lab-storage/default`.
- Mount directory Incus creates: `/var/lib/incus/storage-pools/default`.

**Do not pre-create the Incus mount directory.** Incus 7.5.1 rejects an
existing directory there before the storage driver creates the pool. Moving
the source to another directory inside `/var/lib/incus` is not a workaround:
the directory driver also restricts sources inside the daemon's directory.

First inspect existing pools and instances:

```sh
incus storage list
incus list --all-projects
findmnt -T /var/lib -o TARGET,SOURCE,FSTYPE
```

For this first-use recipe, expect no pools or instances, and Btrfs backing
`/var/lib`. If resources already exist, stop and review their ownership rather
than reinitializing them. Choose either fresh setup or recovery below, not both.

### Fresh setup

The source path must not already exist. The first check also refuses a stale
Incus mount directory; use the recovery section for that case.

```sh
sudo test ! -e /var/lib/incus/storage-pools/default &&
sudo install -d -m 0700 /var/lib/incus-lab-storage &&
sudo btrfs subvolume create /var/lib/incus-lab-storage/default &&
sudo chattr +C /var/lib/incus-lab-storage/default
```

Stop on any failure. Set NoCoW while the source subvolume is empty, before
Incus creates VM disk files.

### Recovery from a pre-created pool directory

Use this only when initialization failed with:

```text
Storage pool directory "/var/lib/incus/storage-pools/default" already exists
```

Check that no pool was registered, the old path is the manually created
Btrfs subvolume, it is empty, and it has the `C` attribute:

```sh
incus storage list
sudo btrfs subvolume show /var/lib/incus/storage-pools/default
sudo ls -A /var/lib/incus/storage-pools/default
sudo lsattr -d /var/lib/incus/storage-pools/default
```

If a pool exists, the directory contains data, or its ownership is uncertain,
stop. Do not use recursive deletion or remove an existing Incus pool.

With those checks satisfied and the destination absent, move the subvolume
outside Incus's daemon directory. These GNU `mv` options refuse an existing
destination and refuse copying across filesystems, preserving a same-filesystem
subvolume rename and its NoCoW attribute:

```sh
sudo install -d -m 0700 /var/lib/incus-lab-storage &&
sudo mv --no-copy --update=none-fail --no-target-directory \
  /var/lib/incus/storage-pools/default \
  /var/lib/incus-lab-storage/default
```

Stop if the move fails. Do not repeat the fresh-setup subvolume creation.

### Create the pool from the external source

After either successful path above, verify the external source and create
only the storage pool. There is no need to rerun the initialization preseed:
the running daemon already has its default project/profile. The `test` refuses
creation while the reserved mount path still exists.

```sh
sudo lsattr -d /var/lib/incus-lab-storage/default &&
sudo test ! -e /var/lib/incus/storage-pools/default &&
incus storage create default dir source=/var/lib/incus-lab-storage/default
```

This creates no `incusbr0` and does not modify the default profile. The lab uses
its own project/profile with an explicit root disk referencing pool `default`.

```sh
incus storage show default
findmnt -T /var/lib/incus/storage-pools/default -o TARGET,SOURCE,FSTYPE
sudo lsattr -d /var/lib/incus-lab-storage/default
```

Expect driver `dir`, the external source path, a mount at the Incus pool path,
and the `C` attribute on the external source. Host-side initialization success
does not establish VM, guest-agent, or Longhorn readiness.

## UFW: trust forwarding from the management bridge

These rules use the default project name `lh-incus`. Change the interface names
if you change the project. Keep UFW enabled and keep the default routed policy
deny/drop; do not replace it with a global forwarding allow.

```sh
# Allow the guests to use Incus's DHCP and DNS services on the host.
sudo ufw allow in on lh-incus-mgmt to any port 67 proto udp
sudo ufw allow in on lh-incus-mgmt to any port 53 proto udp
sudo ufw allow in on lh-incus-mgmt to any port 53 proto tcp

# Trust forwarded traffic arriving from the lab management bridge, any source.
sudo ufw route allow in on lh-incus-mgmt

# Permit forwarding within the cluster bridge, including routed storage pods.
sudo ufw route allow in on lh-incus-node out on lh-incus-node

sudo ufw status verbose
```

The management forwarding rule intentionally has no source-CIDR restriction:
all traffic arriving on that bridge is trusted for forwarding. This covers
VM traffic without coupling the rule to `172.28.155.0/24`. It does not grant
unrestricted access to host services; those use UFW input rules, so DHCP/DNS
exceptions remain explicit. Existing rules in other firewall chains may still
filter traffic. These rules do not by themselves prove Incus/UFW coexistence.

Incus normally configures forwarding when it creates managed bridges. Verify
actual forwarding and connectivity at first apply rather than blindly changing
global IPv6 forwarding, which can affect host router-advertisement behavior.

## Continue only as far as a reviewed plan

Return to the repository root and follow the README's local validation and
configuration sections. Keep `storage_pool = "default"` for the pool above.

The lab currently pins K3s `v1.34.2+k3s1`. A system `kubectl` at 1.37 is outside
the supported one-minor client/server skew. Install a compatible client through
mise without changing repository configuration:

```sh
mise install kubectl@1.34.2
mise exec kubectl@1.34.2 -- kubectl version --client
mise run check
mise run init
mise run plan
```

Use the explicit `mise exec kubectl@1.34.2 -- kubectl ...` prefix for subsequent
lab kubectl commands. A system Helm installation can remain; the lab continues
to use its pinned mise-managed Helm.

Review preflight and the plan before approving `apply`. No host setup command
above creates VMs or provisions Kubernetes. Pool/network preparation is not a
substitute for the live acceptance checks.

References:

- [Incus initialization](https://linuxcontainers.org/incus/docs/main/howto/initialize/)
- [Directory storage](https://linuxcontainers.org/incus/docs/main/reference/storage_dir/)
- [Btrfs storage caveats](https://linuxcontainers.org/incus/docs/main/reference/storage_btrfs/)
- [Incus firewall guidance](https://linuxcontainers.org/incus/docs/main/howto/network_bridge_firewalld/)
- [Kubernetes version skew](https://kubernetes.io/releases/version-skew-policy/#kubectl)
