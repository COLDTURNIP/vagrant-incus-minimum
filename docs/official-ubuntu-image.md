# Official Ubuntu cloud image fallback

On this Arch/Omarchy host, Incus 7.5.1 repeatedly rebooted the
`images:ubuntu/24.04/cloud` image before its agent became usable. This matches
[a reported cloud-image boot loop](https://discuss.linuxcontainers.org/t/loud-vms-stuck-in-continuous-boot-loop/27287).
It is not a reason to reinitialize the storage pool or change host packages.

The approved workaround uses Canonical's official Ubuntu 24.04 QCOW2 image,
minimal Incus image metadata without templates, and explicit cloud-init and
agent config disks. The config disk supplies networking before the agent is
installed. Cloud-init installs the agent from Incus's config disk as described
in the [Incus agent documentation](https://linuxcontainers.org/incus/docs/main/howto/instances_create/#install-the-incus-agent-into-virtual-machine-instances).
Leave both config disks attached for subsequent boots.

## Use it

Set `image_source: official` in `cluster.yml` and import the image once per
Ubuntu release and host:

```sh
mise run image:import            # release from cluster.yml
mise run image:import -- --ubuntu 26.04
```

The helper downloads Canonical's `releases/<release>/release/SHA256SUMS` and
the host-architecture QCOW2 image into `.state/official-image/<release>/`,
checks the SHA256, writes minimal template-free metadata, and imports it as
`ubuntu-<release>-official` in the default Incus project. An existing alias is
not re-imported. It creates no VMs and leaves storage pools alone. The checksum
list comes over Canonical HTTPS; its detached signature is not verified.

`scripts/lab.py` resolves the alias to the image's bare local fingerprint and
turns on the config-drive agent bootstrap. A remote-qualified reference such as
`local:ALIAS` would make the provider pull the image from a network-listening
remote, and this environment deliberately keeps the Incus API off HTTPS.
Re-importing a newer image under the same alias changes the fingerprint, which
apply refuses on an existing cluster.

For a new lab, continue with `mise run up`. On an existing lab, switching
`image_source` or `ubuntu` is refused before planning because it replaces the
VMs; run `mise run down` first. Obtain explicit approval and review a separate
recovery plan; never bypass that safeguard as part of normal provisioning.
Preserve the custom data volumes, bridges, profile, and existing pool.

An image import alone does not establish guest boot or cluster health. Verify
the agent, cloud-init completion, Kubernetes, Longhorn conditions, and actual
storage-network traffic after provisioning.
