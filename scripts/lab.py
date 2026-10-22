#!/usr/bin/env python3
"""Guarded local Incus lifecycle. No third-party Python dependencies."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
PLAN = STATE / "infra.tfplan"
INVENTORY = STATE / "inventory.json"


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


def inventory():
    data = json.loads(tofu("output", "-json", "inventory", capture=True))
    private_directory()
    temporary = INVENTORY.with_suffix(".tmp")
    # Restrict permissions at creation, not after writing.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    temporary.replace(INVENTORY)
    return data


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


def plan_infrastructure(*, destroy=False):
    private_directory()
    tofu("plan", "-input=false", *(["-destroy"] if destroy else []), "-out=" + str(PLAN))
    return json.loads(tofu("show", "-json", str(PLAN), capture=True))


def require_confirmation(actual, supplied):
    if actual == "default" or supplied != actual:
        raise ValueError(f"Explicit confirmation required: --confirm {actual}")


def provision(playbook="site.yml"):
    inventory()
    args = ["ansible-playbook", "-i", INVENTORY, ROOT / "ansible" / playbook]
    local_vars = ROOT / "ansible" / "vars.local.yml"
    if local_vars.exists():
        args += ["--extra-vars", "@" + str(local_vars)]
    run(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "plan", "apply", "inventory", "provision", "verify", "destroy"])
    parser.add_argument("--confirm", help="Exact project name for apply or destructive destroy")
    args = parser.parse_args(argv)
    for program in ("tofu", "incus", "ip", "ansible-playbook"):
        if not shutil.which(program):
            raise ValueError(f"Missing required command: {program}; run through mise")
    if args.command == "init":
        tofu("init", "-input=false")
    elif args.command in ("plan", "apply"):
        plan = plan_infrastructure()
        project = preflight(plan)
        if args.command == "apply":
            refuse_deletions(plan)
            require_confirmation(project, args.confirm)
            tofu("apply", "-input=false", str(PLAN))
            inventory()
    elif args.command == "inventory":
        inventory()
    elif args.command in ("provision", "verify"):
        provision("verify.yml" if args.command == "verify" else "site.yml")
    elif args.command == "destroy":
        topology = json.loads(tofu("output", "-json", "topology", capture=True))
        require_confirmation(topology["project"], args.confirm)
        plan_infrastructure(destroy=True)
        tofu("apply", "-input=false", str(PLAN))
        INVENTORY.unlink(missing_ok=True)
        (ROOT / "shared" / "k3s.config").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
