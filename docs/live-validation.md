# Initial live validation

Validated on 2026-10-24 with Incus 7.5.1, an official Ubuntu 24.04 cloud image,
K3s v1.34.2+k3s1, and Longhorn 1.13.0. This is a functional lab check, not the
complete [acceptance matrix](acceptance.md).

## Results

| Check | Result |
| --- | --- |
| Four VMs, Incus agents, cloud-init | Passed |
| One control-plane and three Kubernetes workers Ready | Passed |
| Repeated provisioning after initial convergence | Passed |
| Final infrastructure plan | No changes |
| Management DHCP, DNS and package-download NAT | Passed during boot/provisioning |
| Multus storage-network attachment on all workers | Passed |
| IPv4 and IPv6 cross-worker storage connectivity | All 12 directed paths passed |
| Storage interface MTU | 1450; full-size IPv4/IPv6 probes passed |
| V1 and V2 volume attachment and I/O | Passed; two replicas each, 32 MiB random data |
| Data persistence across smoke-pod recreation | SHA256 matched for both engines |
| Actual Longhorn replica-network traffic | Passed; replica endpoints use 10.76.x.x and instance-manager storage-interface counters increased during I/O |
| Hugepages disabled | Passed; all four guests have vm.nr_hugepages=0, Longhorn setting is false, V2 SPDK runs with --no-huge |
| V1 and AIO V2 disk conditions | All Ready and Schedulable |
| Longhorn Node CR conditions | Seven True per worker; HugePagesAvailable=False remains |
| Strict all-green verification | Failed on the hugepage condition (since suppressed; see below) |

The three temporary storage-network probe pods were removed. The two smoke
pods and their 1 GiB PVCs remain in namespace `incus-smoke` for inspection.
Garage is running, but no backup/restore round trip has been validated.

## Nightly master-head run

On 2026-10-24 the lab was destroyed and reprovisioned from scratch with
`longhorn: master-head` (then still the Ansible `longhorn_version`). That applies `deploy/longhorn.yaml` from
the `longhorn/longhorn` master branch, with this lab's default settings and
replica count merged into its ConfigMaps. Images running during the run:

| Image | Digest |
| --- | --- |
| longhorn-manager:master-head | sha256:4d64e6032e992e199594cb4c4f486dd614e0003b8fb55e35414e63b8dd40f334 |
| longhorn-instance-manager:master-head | sha256:2aa9620c803937035a8b49156f2ef74dc136a8394355a2dd99ac0a3ca97ec5bc |
| longhorn-engine:master-head | sha256:409f82e4a6af181842522b0f080d41e8c259c66dfbabd3447cf982a55aa6ce3c |
| longhorn-share-manager:master-head | sha256:afed19d0c30436a3fb4e4e09b15b9827dae2eaac6950e7fa0dab1aa53c6c74cd |
| longhorn-ui:master-head | sha256:46ca8d7d95c6db9e64e5ce9415f3a401805d8a4dfa32c75aba6e4c16c836d068 |

| Check | Result |
| --- | --- |
| Destroy, apply, first provision | Passed without manual steps |
| Second provision | Passed; only the kubeconfig refresh and BackupTarget re-apply report changes |
| Final infrastructure plan | No changes |
| Storage network, 12 directed IPv4/IPv6 paths at MTU 1450 | Passed |
| V1 and V2 I/O, persistence across pod recreation | Passed; healthy, two replicas each |
| Replica endpoints on the storage network | Passed (10.76.x.x) |
| Hugepages disabled | Passed; vm.nr_hugepages=0, setting false, SPDK --no-huge |
| Disk Ready and Schedulable | All True |
| `mise run verify` | Passes; two false-negative conditions below are skipped |

Non-green conditions, both false negatives in this lab and skipped by verify:

- `HugePagesAvailable=False` (`HugePagesNotConfigured`) on all workers. The
  master environment checker still tests hugepage capacity whenever V2 is
  enabled, without consulting `data-engine-hugepage-enabled`.
- `HealthDataCollected=False` (`HealthDataCollectionFailed`, smartctl exit
  status 4) on all six Longhorn disks. This disk condition is new in master,
  and `node-disk-health-monitoring` defaults to true. QEMU virtual disks
  report `smart_support.available: false`, so collection always fails.

`mise run verify` leaves out exactly these two condition types, with the reasons
commented in `ansible/verify.yml`. HugePagesAvailable is left out only while
`v2_hugepages` is false. At the time of this run, every other node condition
and disk Ready and Schedulable had to be True, and `mise run verify` passed on
the master-head cluster (`master-head/verify-suppressed.log`). Longhorn Node CR
problems are now warnings; see "Disk readiness race" below.

## cluster.yml workflow check

The same master-head cluster was then driven through `cluster.yml`
(`ubuntu: "24.04"`, `image_source: official`, `longhorn: master-head`,
`ip_family: ipv4-dual`, `storage_network: true`). The existing image gained the
standard `ubuntu-24.04-official` alias with the same fingerprint, so
`mise run image:import` skipped re-importing. `mise run plan` reported no
changes. `mise run up -- --confirm lh-incus` applied nothing, provisioned
idempotently, passed verify, and recorded the applied spec. Then
`mise run plan -- --ip-family ipv6-dual` was refused before planning with
"These changes need a fresh cluster". Logs are in `.state/live/spec/`.

## Force teardown and fresh creation

With `INCUS_PROJECT=lh-incus` and the merged `KUBECONFIG` set in the
workspace environment, `mise run down` (no confirmation) force-stopped the
VMs and removed all 23 managed resources in about 2 seconds. It also deleted the
K3s token, backup credentials, TLS key, kubeconfig, and applied spec. A second
`down` with no cluster was a no-op. `mise run up -- --confirm lh-incus` then
created the cluster from scratch (23 resources, new data volumes, new secrets),
provisioned it, and passed verify in about 6 minutes. Plain `kubectl` used the
`lh-incus` context, raw `incus list local:` listed the lab VMs without
`--project`, and `mise run shell -- worker2` reached `lh-incus-worker2`.
Logs are in `.state/live/fresh/`.

## Disk readiness race

A user-run `mise run up --confirm lh-incus` on a fresh cluster failed in
verify. Provisioning returned once the Longhorn manager and CSI pods were
ready, but the three V2 block disks were still `creating` while the SPDK
instance managers set up their AIO bdevs. Verify started at 07:39:16Z; the disks
became Ready and Schedulable at 07:39:30Z. Provisioning now waits up to 5
minutes for every Longhorn disk, and verify polls for disk readiness before
checking. A repeated `mise run down` and fresh `up` needed one retry of the
provisioning wait and then passed.

Longhorn Node CR status then changed from failing verify to warnings, since
this lab exists to test Longhorn and a non-green node or disk can be the bug
under investigation. Infrastructure, Kubernetes, and configuration checks still
fail verify. Logs are in `.state/live/race/`.

## Recovery and provisioning corrections

The original `images:` cloud image boot-looped before its agent became usable.
With explicit approval, only the four unprovisioned VM root disks were
replaced using the [official Ubuntu fallback](official-ubuntu-image.md).
All 14 custom data volumes, the profile, bridges, project, and pre-existing
pool were retained. No host package, firewall, or pool reconfiguration was
used to recover the VMs.

Live provisioning also established that:

- Incus doubles hyphens in disk serials. Inventory now derives both serials
  and by-id paths from the same escaped identity.
- The K3s checksum expression needed an Ansible-compatible exact-line match.
  The downloaded binaries remain SHA256-verified.
- Ansible service facts did not include the multipath socket. Both discovered
  multipath units are now stopped, disabled, and masked inside the disposable
  guests. A Longhorn manager restart refreshed the cached environment checks;
  all Multipathd conditions are now True.

## Remaining Longhorn condition bug

Longhorn 1.13.0 reports `HugePagesAvailable=False` with reason
`HugePagesNotConfigured` on all three workers despite:

```text
data-engine-hugepage-enabled = {"v2":"false"}
vm.nr_hugepages = 0
spdk_tgt ... --no-huge ...
```

In [longhorn/longhorn-manager's v1.13.0 environment checker](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/monitor/environment_check_monitor.go),
`environmentCheck` calls `checkHugePages` whenever V2 is enabled. That check
tests node hugepage capacity without consulting the per-engine hugepage-enabled
setting. The functional V2 test therefore passes while the condition stays
False.

Hugepages were not allocated and status was not patched. `mise run verify`
leaves this condition out while `v2_hugepages` is false and reports it as a
warning otherwise.

## Access and private evidence

From the project directory, use an explicit kubeconfig so an inherited mise
environment cannot select a different cluster:

```sh
mise exec kubectl@1.34.2 -- kubectl --kubeconfig shared/k3s.config get nodes
mise exec kubectl@1.34.2 -- kubectl --kubeconfig shared/k3s.config \
  -n longhorn-system get nodes.longhorn.io
mise run verify
```

Local evidence is retained in the ignored/private `.state/live/` directory:

- `official-apply.log`, `inventory-fix-apply.log`: infrastructure convergence.
- `provision-final.log`: successful repeated provisioning.
- `storage-network.log`: directed IPv4/IPv6 probes.
- `storage-io.log`: checksums, pod recreation, replica endpoints and interface counters.
- `verify.log`: pre-suppression verification failure with complete Node CR diagnostics.
- `final-infrastructure-plan.log`: no infrastructure drift.
- `final-validation.log`: seven OpenTofu mock runs, sixteen Python tests, and Ansible/lint checks.
- `master-head/`: the nightly run (destroy, apply, provision, provision-2,
  storage-network, storage-io, verify, final-status.json with digests).

Host/guest reboot persistence, worker-failure recovery, V2 rebuild/snapshot
behavior, backup restoration, the fresh-cluster network-mode matrix, and
destructive teardown remain unvalidated.
