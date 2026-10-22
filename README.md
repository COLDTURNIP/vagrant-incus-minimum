# Incus Longhorn test environment

This repository currently provides the local development toolchain only.
VM definitions and guest provisioning have not been implemented. None of the
bootstrap or check tasks initializes Incus, creates VMs, or accesses Kubernetes.

## Host prerequisites

- Linux with KVM, Incus, and permission to access the local Incus daemon.
- mise installed and activated, or use commands through `mise exec`.

Incus remains an OS-managed service. Python, OpenTofu, and ShellCheck versions
are pinned in `mise.toml`; mise downloads them when needed.

## Local setup

Run from this repository's root:

```sh
mise trust mise.toml
mise install
mise run bootstrap
mise run check
```

Mise creates and activates `.venv` using Python's standard-library
`python -m venv`. The configuration explicitly selects stdlib venv creation
even if uv is installed elsewhere. Python dependencies are installed with pip;
uv, Poetry, pipx, and pip-tools are not required.

The environment is separate from any parent workspace's Python environment.
Always run development commands here through mise, for example:

```sh
mise exec -- python -m pip check
mise exec -- ansible-galaxy collection list
mise exec -- tofu version
```

## Dependencies

- `requirements.in`: direct Python dependency names.
- `requirements.txt`: tested pins for direct and transitive Python dependencies.
- `collections/requirements.yml`: pinned Ansible collections.
- `ansible.cfg`: project-local collection and temporary-file paths.
- `mise.toml`: tool versions, venv activation, and setup/check tasks.

Ansible Core provides the CLI; the collections supply the Incus connection and
inventory plugins, POSIX system configuration modules, and Kubernetes modules.
Collections and Ansible working files stay under `.ansible/`.

The Python lock is intended for Linux with the pinned CPython version. Review
compatibility before using another interpreter version or operating system.

To deliberately update Python dependencies, install from `requirements.in`
inside the project venv, inspect `python -m pip freeze --local`, and update
`requirements.txt` with the tested result. Update collection pins separately.
Run bootstrap and check again after any dependency update.

## Version control and generated files

Git and Jujutsu use a colocated repository: both `.git/` and `.jj/` are local
to this directory. No remote is assumed. Use `jj status`, `jj diff`, and
`jj log` to inspect the working change.

The venv, installed collections, caches, shared artifacts, and future OpenTofu
state and local variable files are ignored. A future `.terraform.lock.hcl`
should be tracked when provider configuration is added.

Incus storage pools, bridges, firewall rules, and guest provisioning are
intentionally left for the next implementation stage.
