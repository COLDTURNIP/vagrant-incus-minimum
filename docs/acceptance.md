# Live acceptance checks

Status: not executed. These checks mutate only an approved disposable Incus
cluster and its explicitly named test volumes. They do not authorize changing
another cluster, the host firewall, existing pools, or Vagrant resources.

## Prerequisites

- Complete the README host/pool/subnet review and approve live creation.
- Provision the default three-worker topology; these smoke volumes use two replicas.
- Use K3s/Ubuntu as configured; this validates the Incus backend, not all Longhorn platforms.
- Run commands from this repository root with kubectl and jq available.
- The V2 scenario requires `enable_v2 = true`; run V1-only first if desired.
- `tests/volume-v1.yml` and `tests/volume-v2.yml` are attached smoke manifests.
- Timeouts or nonzero assertions mean failure; preserve evidence before cleanup.

```sh
export KUBECONFIG="$PWD/shared/k3s.config"
kubectl cluster-info
mise run verify
```

## Scenario: volume data survives pod recreation and worker restart

Repeat this scenario with `ENGINE=v2` after V1 passes and V2 is enabled.

1. Given the selected engine and a ready cluster:
   ```sh
   ENGINE=v1
   kubectl wait nodes --all --for=condition=Ready --timeout=300s
   ```
2. When the smoke workload is created:
   ```sh
   kubectl apply -f "tests/volume-${ENGINE}.yml"
   kubectl -n incus-smoke wait "pod/volume-${ENGINE}" --for=condition=Ready --timeout=600s
   kubectl -n incus-smoke exec "volume-${ENGINE}" -- sh -c 'printf incus-storage-probe > /data/probe; sync'
   ```
3. Then the volume is bound and its written marker is readable:
   ```sh
   test "$(kubectl -n incus-smoke get "pvc/volume-${ENGINE}" -o jsonpath='{.status.phase}')" = Bound
   test "$(kubectl -n incus-smoke exec "volume-${ENGINE}" -- cat /data/probe)" = incus-storage-probe
   ```
4. When only the pod is recreated:
   ```sh
   kubectl -n incus-smoke delete "pod/volume-${ENGINE}" --wait=true --timeout=120s
   kubectl apply -f "tests/volume-${ENGINE}.yml"
   kubectl -n incus-smoke wait "pod/volume-${ENGINE}" --for=condition=Ready --timeout=600s
   ```
5. Then the original marker remains without being rewritten:
   ```sh
   test "$(kubectl -n incus-smoke exec "volume-${ENGINE}" -- cat /data/probe)" = incus-storage-probe
   ```
6. When the pod's worker is restarted through Incus:
   ```sh
   NODE=$(kubectl -n incus-smoke get "pod/volume-${ENGINE}" -o jsonpath='{.spec.nodeName}')
   incus restart "local:${NODE}" --project lh-incus
   kubectl -n incus-smoke delete "pod/volume-${ENGINE}" --wait=true --timeout=180s
   kubectl apply -f "tests/volume-${ENGINE}.yml"
   kubectl -n incus-smoke wait "pod/volume-${ENGINE}" --for=condition=Ready --timeout=600s
   ```
7. Then mounts, services, and stored data have recovered:
   ```sh
   mise run verify
   test "$(kubectl -n incus-smoke exec "volume-${ENGINE}" -- cat /data/probe)" = incus-storage-probe
   ```
8. When ordinary provisioning is repeated:
   ```sh
   mise run provision
   ```
9. Then the cluster and data still exist without disk formatting or cluster reset:
   ```sh
   mise run verify
   test "$(kubectl -n incus-smoke exec "volume-${ENGINE}" -- cat /data/probe)" = incus-storage-probe
   ```

## Additional acceptance gates before replacing another environment

The smoke scenario alone is insufficient. Record these as pending until
separate, explicitly approved live verification establishes each result:

| Gate | Required observable result |
| --- | --- |
| Guest agent | Ansible still executes while the selected guest cluster NIC is disabled; restore the NIC afterward |
| Disk identity | Every expected by-id serial exists; UUID mounts survive VM restart; V2 remains unformatted |
| V2 | AIO volume creates, attaches, writes, snapshots, and rebuilds; validate hugepages separately if enabled |
| Backup | For both Garage and NFS, back up the written volume and restore it as a different volume; the same marker must read back |
| Multus | Instance-manager storage interfaces have expected per-node addresses; same-node and cross-node storage traffic succeeds without using the primary CNI path |
| Routing | Storage-pod routes and effective MTU recover after every worker restarts |
| Network matrix | Repeat with fresh clusters for ipv4, ipv6, dual, and dual6; confirm pod/service primary family |
| Host coexistence | Existing Vagrant guests, VPN routes, DNS, and firewall-dependent connectivity remain functional |
| Teardown | Only owned VMs/volumes/bridges disappear; the pre-existing pool and unrelated Incus/Vagrant resources remain |

Do not infer routed storage traffic, backup round-trip correctness, or V2
support from Ready pods or successful template rendering. Do not change
network layout in place to run the matrix: ordinary apply intentionally
refuses the required replacements.

## Failure evidence and cleanup

Before removing a failed workload, retain Ansible output and inspect:

```sh
kubectl -n incus-smoke get pods,pvc -o wide
kubectl -n incus-smoke get events --sort-by=.metadata.creationTimestamp
kubectl -n longhorn-system get pods,nodes.longhorn.io,volumes.longhorn.io
incus list local: --project lh-incus
```

Cleanup deletes the selected smoke PVC and its data; deleting the namespace
also removes the other engine's smoke workload if it is still present.

```sh
kubectl delete -f tests/volume-v1.yml --ignore-not-found --timeout=180s
kubectl delete -f tests/volume-v2.yml --ignore-not-found --timeout=180s
```

Retain the cluster for diagnosis, or use the README's separately confirmed
destructive teardown. Overall adoption passes only when both volume scenarios
and every relevant additional gate pass. Local-only checks do not satisfy it.
