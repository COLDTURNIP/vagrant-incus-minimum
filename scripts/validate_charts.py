"""Download pinned charts and render locally; never access Kubernetes."""

from pathlib import Path
import subprocess
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_URL = "https://raw.githubusercontent.com/longhorn/longhorn/master/deploy/longhorn.yaml"
example = yaml.safe_load((ROOT / "cluster.yml.example").read_text())
local = ROOT / "cluster.yml"
selected = (yaml.safe_load(local.read_text()) or {}) if local.exists() else {}
defaults = yaml.safe_load((ROOT / "ansible/group_vars/all.yml").read_text())
# Validate the tracked default and, when different, the locally selected version.
versions = sorted({example["longhorn"], selected.get("longhorn", example["longhorn"])})
charts = ROOT / ".state/charts"
charts.mkdir(parents=True, exist_ok=True)
values_files = sorted((ROOT / ".state/rendered").glob("longhorn-*-v2-*.yml"))
if not values_files:
    raise RuntimeError("Run mise run validate:offline first")


def validate_manifest():
    with urllib.request.urlopen(MANIFEST_URL, timeout=60) as response:
        documents = [doc for doc in yaml.safe_load_all(response.read()) if doc]
    configmaps = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "ConfigMap"}
    for name, key in (("longhorn-default-setting", "default-setting.yaml"),
                      ("longhorn-storageclass", "storageclass.yaml")):
        if key not in configmaps.get(name, {}).get("data", {}):
            raise RuntimeError("Manifest lacks ConfigMap " + name + " key " + key)
    managers = [doc for doc in documents if doc["kind"] == "DaemonSet"
                and doc["metadata"]["name"] == "longhorn-manager"]
    images = [c["image"] for c in managers[0]["spec"]["template"]["spec"]["containers"]] if managers else []
    if not any(image.endswith("/longhorn-manager:master-head") for image in images):
        raise RuntimeError("Manifest does not deploy longhorn-manager:master-head")
    print("Longhorn master-head manifest OK: " + str(len(documents)) + " documents")


for version in versions:
    if version == "master-head":
        validate_manifest()
        continue
    chart_version = version.removeprefix("v")
    subprocess.run([
        "helm", "pull", "longhorn", "--repo", "https://charts.longhorn.io",
        "--version", chart_version, "--destination", str(charts),
    ], cwd=ROOT, check=True)
    for values in values_files:
        result = subprocess.run([
            "helm", "template", "longhorn", str(charts / ("longhorn-" + chart_version + ".tgz")),
            "--namespace", "longhorn-system", "--values", str(values),
        ], cwd=ROOT, check=True, text=True, capture_output=True)
        documents = list(yaml.safe_load_all(result.stdout))
        settings = [doc for doc in documents if doc and doc.get("kind") == "ConfigMap"
                    and any("v2-data-engine" in str(value) for value in doc.get("data", {}).values())]
        if not settings:
            raise RuntimeError("Chart did not render expected Longhorn default settings")
        print("Longhorn chart " + chart_version + " OK: " + values.name)
result = subprocess.run([
    "helm", "template", "multus", defaults["multus_chart_url"],
    "--namespace", "kube-system",
    "--set", "config.fullnameOverride=multus",
    "--set", "config.cni_conf.confDir=/var/lib/rancher/k3s/agent/etc/cni/net.d",
    "--set", "config.cni_conf.binDir=/var/lib/rancher/k3s/data/cni/",
    "--set", "config.cni_conf.kubeconfig=/var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d/multus.kubeconfig",
    "--set", "config.cni_conf.multusAutoconfigDir=/var/lib/rancher/k3s/agent/etc/cni/net.d",
    "--set", "rke2-whereabouts.enabled=false",
], cwd=ROOT, check=True, text=True, capture_output=True)
if not any(doc and doc.get("kind") == "DaemonSet" for doc in yaml.safe_load_all(result.stdout)):
    raise RuntimeError("Multus chart did not render a DaemonSet")
print("Multus chart OK")
