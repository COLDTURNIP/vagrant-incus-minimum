#!/usr/bin/env python3
"""Guarded local Incus lifecycle driven by cluster.yml. Runs inside the project venv."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
PLAN = STATE / "infra.tfplan"
INVENTORY = STATE / "inventory.json"
SPEC_EXAMPLE = ROOT / "cluster.yml.example"
SPEC_FILE = ROOT / "cluster.yml"
TF_SPEC = STATE / "cluster.tfvars.json"
ANSIBLE_SPEC = STATE / "cluster-vars.json"
APPLIED_SPEC = STATE / "applied-cluster.json"
IP_FAMILIES = {"ipv4-single": "ipv4", "ipv6-single": "ipv6", "ipv4-dual": "dual", "ipv6-dual": "dual6"}
IMAGE_SOURCES = ("images", "official")
# Changing these replaces VMs or the network layout, which apply refuses.
RECREATE_KEYS = ("ubuntu", "image_source", "ip_family", "storage_network")
OFFICIAL_IMAGES = "https://cloud-images.ubuntu.com/releases/{ubuntu}/release/"
# Per-cluster local files. Removed by down and before a fresh cluster is created,
# so a new cluster never inherits a token, credentials, or certificates.
CLUSTER_FILES = (
    INVENTORY, APPLIED_SPEC, STATE / "k3s-token", STATE / "backup-secret", STATE / "garage-rpc",
    STATE / "backup.key", STATE / "backup.crt", ROOT / "shared" / "k3s.config",
    ROOT / "shared" / "longhorn-values.yml", ROOT / "shared" / "longhorn-manifest.yml",
)


def run(args, *, capture=False):
    print("+ " + " ".join(map(str, args)), file=sys.stderr)
    result = subprocess.run(
        list(map(str, args)), cwd=ROOT, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def tofu(*args, capture=False):
    return run(["tofu", "-chdir=infra", *args], capture=capture)


def private_directory():
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)


def write_private_json(path, data):
    private_directory()
    temporary = path.with_suffix(".tmp")
    # Restrict permissions at creation, not after writing.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def inventory():
    data = json.loads(tofu("output", "-json", "inventory", capture=True))
    write_private_json(INVENTORY, data)
    return data


def load_spec(overrides=None):
    """cluster.yml.example supplies defaults; cluster.yml and per-run flags override them."""
    spec = yaml.safe_load(SPEC_EXAMPLE.read_text())
    if SPEC_FILE.exists():
        local = yaml.safe_load(SPEC_FILE.read_text()) or {}
        unknown = sorted(set(local) - set(spec))
        if unknown:
            raise ValueError("Unknown cluster.yml keys: " + ", ".join(unknown))
        spec.update(local)
    spec.update({key: value for key, value in (overrides or {}).items() if value is not None})
    if isinstance(spec["ubuntu"], float):
        # An unquoted 24.04 is a YAML float.
        spec["ubuntu"] = f"{spec['ubuntu']:.2f}"
    checks = (
        ("ubuntu", re.fullmatch(r"\d{2}\.\d{2}", str(spec["ubuntu"])), "a release such as \"24.04\""),
        ("image_source", spec["image_source"] in IMAGE_SOURCES, " or ".join(IMAGE_SOURCES)),
        ("k3s", re.fullmatch(r"v\d+\.\d+\.\d+\+k3s\d+", str(spec["k3s"])), "for example v1.34.2+k3s1"),
        ("longhorn", re.fullmatch(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?|master-head", str(spec["longhorn"])),
         "vX.Y.Z or master-head"),
        ("ip_family", spec["ip_family"] in IP_FAMILIES, ", ".join(IP_FAMILIES)),
        ("storage_network", isinstance(spec["storage_network"], bool), "true or false"),
    )
    for key, valid, expected in checks:
        if not valid:
            raise ValueError(f"Invalid {key}: {spec[key]!r}; expected {expected}")
    return spec


def official_alias(ubuntu):
    return f"ubuntu-{ubuntu}-official"


def resolve_image(spec):
    """Return the OpenTofu image reference and whether config-drive agent bootstrap is needed."""
    if spec["image_source"] == "images":
        return f"images:ubuntu/{spec['ubuntu']}/cloud", False
    alias = official_alias(spec["ubuntu"])
    images = json.loads(run(["incus", "image", "list", "local:", "--project", "default",
                             "--format", "json"], capture=True))
    for image in images:
        if any(item["name"] == alias for item in image.get("aliases") or []):
            # A bare local fingerprint; a remote-qualified alias needs a network listener.
            return image["fingerprint"], True
    raise ValueError(f"Image alias {alias} not found; run: mise run image:import -- --ubuntu {spec['ubuntu']}")


def write_tofu_spec(spec):
    image, bootstrap_agent = resolve_image(spec)
    write_private_json(TF_SPEC, {
        "image": image,
        "bootstrap_agent": bootstrap_agent,
        "network_stack": IP_FAMILIES[spec["ip_family"]],
        "enable_storage_network": spec["storage_network"],
    })
    return TF_SPEC


def write_ansible_spec(spec):
    write_private_json(ANSIBLE_SPEC, {"k3s_version": spec["k3s"], "longhorn_version": spec["longhorn"]})
    return ANSIBLE_SPEC


def applied_spec():
    """The spec recorded at the last apply, if infrastructure still exists in state."""
    if not APPLIED_SPEC.exists() or not tofu("state", "list", capture=True).strip():
        return None
    return json.loads(APPLIED_SPEC.read_text())


def release(version):
    """Numeric release tuple of v1.34.2+k3s1 or v1.13.0; suffixes are ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0]))


def refuse_recreation(spec, applied):
    if not applied:
        return
    changed = [f"{key} {applied[key]!r} -> {spec[key]!r}" for key in RECREATE_KEYS
               if key in applied and applied[key] != spec[key]]
    if "longhorn" in applied and (applied["longhorn"] == "master-head") != (spec["longhorn"] == "master-head"):
        changed.append(f"longhorn {applied['longhorn']!r} -> {spec['longhorn']!r} (Helm vs manifest)")
    for key in ("k3s", "longhorn"):
        if key in applied and "master-head" not in (applied[key], spec[key]) \
                and release(spec[key]) < release(applied[key]):
            changed.append(f"{key} {applied[key]!r} -> {spec[key]!r} (downgrades are unsupported)")
    if changed:
        raise ValueError("These changes need a fresh cluster: " + "; ".join(changed)
                         + ". Run mise run down, then mise run up.")


def record_applied(spec, *keys):
    data = json.loads(APPLIED_SPEC.read_text()) if APPLIED_SPEC.exists() else {}
    data.update({key: spec[key] for key in keys})
    write_private_json(APPLIED_SPEC, data)


def import_image(spec):
    """Import Canonical's checksum-verified cloud image as ubuntu-<version>-official."""
    alias = official_alias(spec["ubuntu"])
    existing = json.loads(run(["incus", "image", "list", "local:", "--project", "default",
                               "--format", "json"], capture=True))
    if any(item["name"] == alias for image in existing for item in image.get("aliases") or []):
        print(f"Image alias {alias} already exists; not re-importing", file=sys.stderr)
        return
    machine = platform.machine()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine)
    if not arch:
        raise ValueError(f"Unsupported host architecture {machine}")
    name = f"ubuntu-{spec['ubuntu']}-server-cloudimg-{arch}.img"
    base = OFFICIAL_IMAGES.format(ubuntu=spec["ubuntu"])
    # Downloads are data only and get their own directory.
    directory = STATE / "official-image" / spec["ubuntu"]
    private_directory()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with urllib.request.urlopen(base + "SHA256SUMS", timeout=60) as response:
        sums = response.read().decode("ascii")
    expected = [line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*") == name]
    if len(expected) != 1:
        raise ValueError(f"{name} is not listed in {base}SHA256SUMS")
    image = directory / name
    urllib.request.urlretrieve(base + name, image)
    digest = hashlib.sha256()
    with image.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected[0]:
        image.unlink()
        raise ValueError(f"Checksum mismatch for {name}")
    metadata = directory / "metadata.yaml"
    metadata.write_text(yaml.safe_dump({
        "architecture": machine, "creation_date": int(time.time()),
        "properties": {"description": f"Official Ubuntu {spec['ubuntu']} cloud image with config-drive agent bootstrap",
                       "os": "Ubuntu", "release": spec["ubuntu"]},
    }), encoding="ascii")
    with tarfile.open(directory / "metadata.tar.xz", "w:xz") as archive:
        archive.add(metadata, arcname="metadata.yaml")
    run(["incus", "image", "import", directory / "metadata.tar.xz", image,
         "--alias", alias, "--project", "default"])


def walk_resources(module):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from walk_resources(child)


def network(value):
    if value in (None, "", "none", "auto", "default"):
        return None
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def assert_no_overlap(requested, existing):
    for name, subnet in requested:
        for other_name, other in existing:
            if subnet.version == other.version and subnet.overlaps(other):
                raise ValueError(f"Network overlap: {name} ({subnet}) and {other_name} ({other})")


def refuse_deletions(plan):
    deletions = [change["address"] for change in plan.get("resource_changes", [])
                 if "delete" in change["change"]["actions"]]
    if deletions:
        raise ValueError("Apply refuses deletions/replacements: " + ", ".join(deletions)
                         + ". Review the plan; use an explicit full destroy/recreate instead.")


def require_fresh_disks(plan):
    """A VM created by this plan must get newly created data volumes, never existing ones."""
    actions = {change["address"]: change["change"]["actions"] for change in plan.get("resource_changes", [])}
    created = [address for address, action in actions.items()
               if address.startswith("incus_instance.node[") and action == ["create"]]
    for address in created:
        node = address.split('"')[1]
        reused = [volume for volume, action in actions.items()
                  if volume.startswith(f'incus_storage_volume.data["{node}-') and action != ["create"]]
        if reused:
            raise ValueError(f"{address} would reuse existing data volumes {', '.join(reused)}; "
                             "run mise run down and create the cluster from scratch.")


def clear_cluster_files():
    for path in CLUSTER_FILES:
        path.unlink(missing_ok=True)


def preflight(plan):
    values = plan["planned_values"]
    variables = values["outputs"]["inventory"]["value"]["all"]["vars"]
    topology = values["outputs"]["topology"]["value"]
    if topology["remote"] != "local":
        raise ValueError("Guarded helpers support the local Incus daemon only")
    project = topology["project"]
    resources = list(walk_resources(values["root_module"]))
    owned = {resource["values"]["name"] for resource in resources if resource["type"] == "incus_network"}
    requested = []
    for resource in resources:
        if resource["type"] == "incus_network":
            for family in ("ipv4", "ipv6"):
                cidr = network(resource["values"]["config"].get(family + ".address"))
                if cidr:
                    requested.append((resource["values"]["name"], cidr))
    for cidr in variables["incus_pod_cidrs"] + variables["incus_service_cidrs"]:
        requested.append(("Kubernetes", ipaddress.ip_network(cidr)))
    if variables["incus_storage_network"]:
        for family in ("ipv4", "ipv6"):
            if variables["incus_network_stack"] != ("ipv6" if family == "ipv4" else "ipv4"):
                requested.append(("storage pods", ipaddress.ip_network(variables[f"incus_storage_{family}_cidr"])))
    for index, item in enumerate(requested):
        assert_no_overlap([item], requested[index + 1:])

    projects = json.loads(run(["incus", "project", "list", "local:", "--format", "json"], capture=True))
    project_creates = any(change["type"] == "incus_project" and change["change"]["actions"] == ["create"]
                          for change in plan.get("resource_changes", []))
    if project_creates and any(item["name"] == project for item in projects):
        raise ValueError(f"Project {project} already exists outside this state; choose another name")
    run(["incus", "storage", "show", "local:" + topology["storage_pool"], "--project", "default"])
    existing_networks = json.loads(run(
        ["incus", "network", "list", "local:", "--project", "default", "--format", "json"], capture=True))
    creates = {change["change"]["after"]["name"] for change in plan.get("resource_changes", [])
               if change["type"] == "incus_network" and change["change"]["actions"] == ["create"]}
    existing = []
    for item in existing_networks:
        if item["name"] in creates:
            raise ValueError(f"Network {item['name']} already exists outside this state")
        if item["name"] in owned:
            continue
        for family in ("ipv4", "ipv6"):
            cidr = network(item.get("config", {}).get(family + ".address"))
            if cidr:
                existing.append(("Incus " + item["name"], cidr))
    for family in ("-4", "-6"):
        routes = json.loads(run(["ip", "-json", family, "route", "show", "table", "all"], capture=True))
        for route in routes:
            if route.get("dev") in owned:
                continue
            cidr = network(route.get("dst"))
            if cidr and cidr.prefixlen:
                existing.append(("host route on " + route.get("dev", "unknown"), cidr))
    assert_no_overlap(requested, existing)
    return project


def plan_infrastructure(*, destroy=False, var_file=None):
    private_directory()
    tofu("plan", "-input=false", *(["-destroy"] if destroy else []),
         *(["-var-file=" + str(var_file)] if var_file else []), "-out=" + str(PLAN))
    return json.loads(tofu("show", "-json", str(PLAN), capture=True))


def refuse_default_project(project):
    if project == "default":
        raise ValueError("Refusing to manage the default Incus project; use a dedicated project")


def summarize_plan(plan):
    counts = {}
    for change in plan.get("resource_changes", []):
        action = "/".join(change["change"]["actions"])
        if action != "no-op":
            counts[action] = counts.get(action, 0) + 1
    return ", ".join(f"{count} to {action}" for action, count in sorted(counts.items())) or "no changes"


def provision(spec, playbook="site.yml"):
    inventory()
    args = ["ansible-playbook", "-i", INVENTORY, ROOT / "ansible" / playbook]
    local_vars = ROOT / "ansible" / "vars.local.yml"
    if local_vars.exists():
        args += ["--extra-vars", "@" + str(local_vars)]
    # Later extra vars win: cluster.yml selects the K3s and Longhorn versions.
    args += ["--extra-vars", "@" + str(write_ansible_spec(spec))]
    run(args)


def apply_infrastructure(spec, dry_run=False):
    """Plan, run every guard, then apply unless dry_run. Returns True when applied."""
    refuse_recreation(spec, applied_spec())
    plan = plan_infrastructure(var_file=write_tofu_spec(spec))
    refuse_default_project(preflight(plan))
    refuse_deletions(plan)
    require_fresh_disks(plan)
    if dry_run:
        print("Dry run: all checks passed; infrastructure would get " + summarize_plan(plan) + ".",
              file=sys.stderr)
        return False
    if any(change["type"] == "incus_project" and change["change"]["actions"] == ["create"]
           for change in plan.get("resource_changes", [])):
        # A new project means a new cluster: drop anything left from an older one.
        clear_cluster_files()
    tofu("apply", "-input=false", str(PLAN))
    record_applied(spec, *RECREATE_KEYS)
    inventory()
    return True


def project_name():
    return json.loads(tofu("output", "-json", "topology", capture=True))["project"]


def destroy(dry_run=False):
    """Force teardown, like vagrant destroy -f: no prompt, no data preservation."""
    if tofu("state", "list", capture=True).strip():
        project = project_name()
        refuse_default_project(project)
        instances = json.loads(run(["incus", "list", "local:", "--project", project, "--format", "json"],
                                   capture=True))
        running = [item["name"] for item in instances if item["status"] == "Running"]
        if dry_run:
            plan = plan_infrastructure(destroy=True)
            print(f"Dry run: would force-stop {', '.join(running) or 'no running VMs'} and destroy "
                  f"{summarize_plan(plan)}.", file=sys.stderr)
        else:
            # Graceful guest shutdown only slows teardown of disposable VMs.
            if running:
                run(["incus", "stop", "--force", *(f"local:{name}" for name in running), "--project", project])
            tofu("apply", "-destroy", "-auto-approve", "-input=false")
    elif dry_run:
        print("Dry run: no cluster in state; nothing to destroy.", file=sys.stderr)
    if dry_run:
        files = [str(path.relative_to(ROOT)) for path in CLUSTER_FILES if path.exists()]
        print("Dry run: would delete local files: " + (", ".join(files) or "none") + ".", file=sys.stderr)
        return
    clear_cluster_files()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "plan", "apply", "inventory", "provision", "verify",
                                            "destroy", "down", "up", "import-image", "shell"])
    parser.add_argument("node", nargs="?", default="server",
                        help="shell only: server, worker<N>, or a full instance name")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan, apply, up, down, destroy: run every check and show the changes "
                             "without making them")
    overrides = parser.add_argument_group("per-run overrides of cluster.yml (not persisted)")
    overrides.add_argument("--ubuntu", help="Ubuntu release, for example 24.04")
    overrides.add_argument("--image-source", choices=IMAGE_SOURCES)
    overrides.add_argument("--k3s", help="K3s release, for example v1.34.2+k3s1")
    overrides.add_argument("--longhorn", help="vX.Y.Z (Helm) or master-head (manifest)")
    overrides.add_argument("--ip-family", choices=list(IP_FAMILIES))
    overrides.add_argument("--storage-network", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)
    if args.dry_run and args.command not in ("plan", "apply", "up", "down", "destroy"):
        raise ValueError(f"--dry-run is not supported by {args.command}")
    for program in ("tofu", "incus", "ip", "ansible-playbook"):
        if not shutil.which(program):
            raise ValueError(f"Missing required command: {program}; run through mise")
    if args.command in ("destroy", "down"):
        # Teardown uses state only, so an invalid cluster.yml cannot block it.
        destroy(args.dry_run)
        return
    if args.command == "shell":
        project = project_name()
        name = args.node if args.node.startswith(project + "-") else f"{project}-{args.node}"
        os.execvp("incus", ["incus", "shell", f"local:{name}", "--project", project])
        return
    if args.command in ("init", "inventory"):
        if args.command == "init":
            tofu("init", "-input=false")
        else:
            inventory()
        return
    spec = load_spec({"ubuntu": args.ubuntu, "image_source": args.image_source, "k3s": args.k3s,
                      "longhorn": args.longhorn, "ip_family": args.ip_family,
                      "storage_network": args.storage_network})
    print("Cluster: " + json.dumps(spec, sort_keys=True), file=sys.stderr)
    if args.command == "import-image":
        import_image(spec)
    elif args.command in ("plan", "apply"):
        apply_infrastructure(spec, dry_run=args.dry_run or args.command == "plan")
    elif args.command == "provision":
        refuse_recreation(spec, applied_spec())
        provision(spec)
        record_applied(spec, "k3s", "longhorn")
    elif args.command == "verify":
        provision(spec, "verify.yml")
    elif args.command == "up":
        tofu("init", "-input=false")
        if not apply_infrastructure(spec, dry_run=args.dry_run):
            print(f"Dry run: would then provision K3s {spec['k3s']} and Longhorn {spec['longhorn']}, "
                  "and verify.", file=sys.stderr)
            return
        provision(spec)
        record_applied(spec, "k3s", "longhorn")
        provision(spec, "verify.yml")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
