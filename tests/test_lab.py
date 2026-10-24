"""Offline integration tests driven by the actual OpenTofu mock plans."""

import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lab", ROOT / "scripts/lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["tofu", "-chdir=infra", "test", "-json", "-verbose"],
            cwd=ROOT, check=True, text=True, capture_output=True,
        )
        events = [json.loads(line) for line in result.stdout.splitlines()]
        cls.plans = [event["test_plan"] for event in events
                     if event.get("type") == "test_plan"
                     and "outputs" in event["test_plan"]["planned_values"]]
        cls.inventory = cls.plans[2]["planned_values"]["outputs"]["inventory"]["value"]
        lab.private_directory()
        (lab.STATE / "mock-inventory.json").write_text(json.dumps(cls.inventory), encoding="ascii")
        cls.defaults = yaml.safe_load((ROOT / "ansible/group_vars/all.yml").read_text())
        cls.environment = Environment(undefined=StrictUndefined, trim_blocks=True)
        cls.environment.filters["to_json"] = json.dumps
        cls.environment.filters["hash"] = lambda value, algorithm: __import__("hashlib").new(
            algorithm, value.encode()).hexdigest()
        cls.environment.globals["lookup"] = lambda *args: "offline-test-certificate"

    def setUp(self):
        # Never read the developer's cluster.yml or touch the real spec files.
        scratch = Path(tempfile.mkdtemp(dir=lab.STATE))
        self.addCleanup(shutil.rmtree, scratch)
        self.scratch = scratch
        patcher = patch.multiple(lab, SPEC_FILE=scratch / "cluster.yml", TF_SPEC=scratch / "tf.json",
                                 ANSIBLE_SPEC=scratch / "vars.json", APPLIED_SPEC=scratch / "applied.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def context(self, plan, hostname, target="garage"):
        inventory = plan["planned_values"]["outputs"]["inventory"]["value"]["all"]
        hosts = {}
        groups = {}
        for group, definition in inventory["children"].items():
            groups[group] = list(definition["hosts"])
            hosts.update(definition["hosts"])
        hosts["localhost"]["cluster_token"] = "offline-token"
        return {
            **self.defaults, **inventory["vars"], **hosts[hostname],
            "inventory_hostname": hostname, "groups": groups, "hostvars": hosts,
            "project_root": str(ROOT), "backup_target": target, "backup_name": "backupstore-" + target,
            "longhorn_backup_url": {
                "garage": "s3://backupbucket@us-east-1/",
                "nfs": "nfs://backupstore-nfs.longhorn-test:/opt/backupstore",
                "none": "",
            }[target],
            "backup_access_key": "test-access-key", "backup_secret_key": "test-secret-key",
            "backup_rpc_secret": "a" * 64,
        }

    def render(self, path, context):
        return self.environment.from_string((ROOT / path).read_text()).render(context)

    def test_all_mock_modes_pass_preflight_on_an_empty_host(self):
        for plan in self.plans:
            with self.subTest(stack=plan["planned_values"]["outputs"]["topology"]["value"]["network_stack"]):
                with patch.object(lab, "run", return_value="[]"):
                    self.assertEqual(lab.preflight(plan), "lh-incus")

    def test_existing_project_is_not_adopted(self):
        with patch.object(lab, "run", return_value='[{"name":"lh-incus"}]'):
            with self.assertRaisesRegex(ValueError, "already exists"):
                lab.preflight(self.plans[0])

    def test_existing_network_is_not_adopted(self):
        with patch.object(lab, "run", side_effect=["[]", "", '[{"name":"lh-incus-mgmt"}]']):
            with self.assertRaisesRegex(ValueError, "already exists"):
                lab.preflight(self.plans[0])

    def test_host_route_collision_is_rejected(self):
        with patch.object(lab, "run", side_effect=[
            "[]", "", "[]", '[{"dst":"172.28.156.0/24","dev":"virbr-existing"}]', "[]",
        ]):
            with self.assertRaisesRegex(ValueError, "overlap"):
                lab.preflight(self.plans[0])

    def test_internal_subnet_collision_is_rejected(self):
        plan = copy.deepcopy(self.plans[0])
        plan["planned_values"]["outputs"]["inventory"]["value"]["all"]["vars"]["incus_pod_cidrs"] = ["172.28.155.0/24"]
        with self.assertRaisesRegex(ValueError, "overlap"):
            lab.preflight(plan)

    def test_own_bridge_routes_are_accepted(self):
        with patch.object(lab, "run", side_effect=[
            "[]", "", "[]", '[{"dst":"172.28.156.0/24","dev":"lh-incus-node"}]', "[]",
        ]):
            self.assertEqual(lab.preflight(self.plans[0]), "lh-incus")

    def test_destructive_plans_are_refused(self):
        for actions in (["delete"], ["delete", "create"], ["create", "delete"]):
            with self.subTest(actions=actions):
                with self.assertRaises(ValueError):
                    lab.refuse_deletions({"resource_changes": [
                        {"address": "incus_instance.node", "change": {"actions": actions}},
                    ]})
        lab.refuse_deletions(self.plans[0])

    def test_exact_confirmation_is_required(self):
        for supplied in (None, "", "other"):
            with self.assertRaises(ValueError):
                lab.require_confirmation("lh-incus", supplied)
        with self.assertRaises(ValueError):
            lab.require_confirmation("default", "default")
        lab.require_confirmation("lh-incus", "lh-incus")

    def test_unconfirmed_apply_never_executes_apply(self):
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "plan_infrastructure", return_value=self.plans[0]), \
             patch.object(lab, "preflight", return_value="lh-incus"), \
             patch.object(lab, "tofu") as command:
            with self.assertRaises(ValueError):
                lab.main(["apply"])
            command.assert_not_called()

    def test_confirmed_apply_still_refuses_replacements(self):
        plan = copy.deepcopy(self.plans[0])
        plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "plan_infrastructure", return_value=plan), \
             patch.object(lab, "preflight", return_value="lh-incus"), \
             patch.object(lab, "tofu") as command:
            with self.assertRaisesRegex(ValueError, "refuses deletions"):
                lab.main(["apply", "--confirm", "lh-incus"])
            command.assert_not_called()

    def test_down_force_destroys_and_clears_cluster_files(self):
        cluster_files = [self.scratch / name for name in ("k3s-token", "backup.key", "k3s.config")]
        instances = json.dumps([{"name": "lh-incus-server", "status": "Running"},
                                {"name": "lh-incus-worker1", "status": "Stopped"}])
        for command_name in ("down", "destroy"):
            with self.subTest(command=command_name):
                for path in cluster_files:
                    path.write_text("old cluster")
                with patch.object(lab, "CLUSTER_FILES", tuple(cluster_files)), \
                     patch.object(lab.shutil, "which", return_value="/bin/tool"), \
                     patch.object(lab, "tofu", side_effect=["incus_project.cluster\n", '{"project":"lh-incus"}', ""]) as command, \
                     patch.object(lab, "run", side_effect=[instances, ""]) as incus:
                    lab.main([command_name])
                self.assertEqual(incus.call_args_list[1].args[0],
                                 ["incus", "stop", "--force", "local:lh-incus-server", "--project", "lh-incus"])
                self.assertEqual(command.call_args_list[2].args, ("apply", "-destroy", "-auto-approve", "-input=false"))
                self.assertFalse(any(path.exists() for path in cluster_files))
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "tofu", side_effect=["incus_project.cluster\n", '{"project":"default"}']), \
             patch.object(lab, "run") as incus:
            with self.assertRaisesRegex(ValueError, "default Incus project"):
                lab.main(["down"])
            incus.assert_not_called()
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "CLUSTER_FILES", tuple(cluster_files)), \
             patch.object(lab, "tofu", return_value="") as command:
            lab.main(["down"])
        command.assert_called_once_with("state", "list", capture=True)

    def test_new_vms_never_reuse_existing_data_volumes(self):
        lab.require_fresh_disks(self.plans[0])
        plan = copy.deepcopy(self.plans[0])
        volume = next(c for c in plan["resource_changes"]
                      if c["address"].startswith('incus_storage_volume.data["worker1-'))
        volume["change"]["actions"] = ["no-op"]
        with self.assertRaisesRegex(ValueError, 'incus_instance.node\\["worker1"\\] would reuse'):
            lab.require_fresh_disks(plan)

    def test_fresh_cluster_creation_clears_old_cluster_files(self):
        stale = self.scratch / "k3s-token"
        stale.write_text("old cluster")
        with patch.object(lab, "CLUSTER_FILES", (stale,)), \
             patch.object(lab, "plan_infrastructure", return_value=self.plans[0]), \
             patch.object(lab, "preflight", return_value="lh-incus"), \
             patch.object(lab, "inventory"), patch.object(lab, "tofu"):
            lab.apply_infrastructure(lab.load_spec(), "lh-incus")
        self.assertFalse(stale.exists())

    def test_shell_maps_short_node_names(self):
        for node, expected in (([], "lh-incus-server"), (["worker2"], "lh-incus-worker2"),
                               (["lh-incus-worker3"], "lh-incus-worker3")):
            with self.subTest(node=node), \
                 patch.object(lab.shutil, "which", return_value="/bin/tool"), \
                 patch.object(lab, "tofu", return_value='{"project":"lh-incus"}'), \
                 patch.object(lab.os, "execvp") as execute:
                lab.main(["shell", *node])
            execute.assert_called_once_with(
                "incus", ["incus", "shell", "local:" + expected, "--project", "lh-incus"])

    def test_kubeconfig_names_follow_the_project(self):
        playbook = yaml.safe_load((ROOT / "ansible/site.yml").read_text())
        task = next(task for play in playbook for task in play.get("tasks", [])
                    if task["name"] == "Name the cluster, user and context after the project")
        module = task["ansible.builtin.replace"]
        k3s = ("apiVersion: v1\nclusters:\n- cluster:\n    server: https://172.28.156.20:6443\n"
               "  name: default\ncontexts:\n- context:\n    cluster: default\n    user: default\n"
               "  name: default\ncurrent-context: default\nkind: Config\nusers:\n- name: default\n"
               "  user:\n    client-certificate-data: ZGVmYXVsdA==\n")
        replacement = module["replace"].replace("{{ incus_project }}", "lh-incus")
        renamed = yaml.safe_load(re.sub(module["regexp"], replacement, k3s, flags=re.MULTILINE))
        self.assertEqual(renamed["current-context"], "lh-incus")
        self.assertEqual(renamed["contexts"][0], {"context": {"cluster": "lh-incus", "user": "lh-incus"},
                                                  "name": "lh-incus"})
        self.assertEqual((renamed["clusters"][0]["name"], renamed["users"][0]["name"]), ("lh-incus", "lh-incus"))
        self.assertEqual(renamed["users"][0]["user"]["client-certificate-data"], "ZGVmYXVsdA==")

    def test_cluster_spec_defaults_overrides_and_validation(self):
        example = yaml.safe_load((ROOT / "cluster.yml.example").read_text())
        self.assertEqual(lab.load_spec(), example)
        self.assertEqual((self.defaults["k3s_version"], self.defaults["longhorn_version"]),
                         (example["k3s"], example["longhorn"]), "group_vars defaults drifted from cluster.yml.example")
        lab.SPEC_FILE.write_text("ubuntu: 26.04\nip_family: ipv6-dual\n")
        spec = lab.load_spec({"longhorn": "master-head", "storage_network": True, "k3s": None})
        self.assertEqual((spec["ubuntu"], spec["ip_family"], spec["longhorn"], spec["storage_network"], spec["k3s"]),
                         ("26.04", "ipv6-dual", "master-head", True, example["k3s"]))
        lab.SPEC_FILE.write_text("ubuntu_version: '24.04'\n")
        with self.assertRaisesRegex(ValueError, "Unknown cluster.yml keys"):
            lab.load_spec()
        lab.SPEC_FILE.unlink()
        for key, value in (("ubuntu", "noble"), ("image_source", "local"), ("k3s", "1.34.2"),
                           ("longhorn", "master"), ("ip_family", "dual"), ("storage_network", "yes")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Invalid " + key):
                lab.load_spec({key: value})

    def test_cluster_spec_maps_to_tofu_and_ansible_variables(self):
        for family, stack in lab.IP_FAMILIES.items():
            with self.subTest(family=family):
                lab.write_tofu_spec(lab.load_spec({"ip_family": family, "storage_network": True}))
                self.assertEqual(json.loads(lab.TF_SPEC.read_text()), {
                    "image": "images:ubuntu/24.04/cloud", "bootstrap_agent": False,
                    "network_stack": stack, "enable_storage_network": True})
        images = json.dumps([{"fingerprint": "f" * 64, "aliases": [{"name": "ubuntu-24.04-official"}]},
                             {"fingerprint": "e" * 64, "aliases": None}])
        with patch.object(lab, "run", return_value=images):
            self.assertEqual(lab.resolve_image(lab.load_spec({"image_source": "official"})), ("f" * 64, True))
            with self.assertRaisesRegex(ValueError, "image:import -- --ubuntu 26.04"):
                lab.resolve_image(lab.load_spec({"image_source": "official", "ubuntu": "26.04"}))
        lab.write_ansible_spec(lab.load_spec({"longhorn": "master-head"}))
        self.assertEqual(json.loads(lab.ANSIBLE_SPEC.read_text()),
                         {"k3s_version": lab.load_spec()["k3s"], "longhorn_version": "master-head"})

    def test_recreation_changes_are_refused_before_planning(self):
        applied = {**lab.load_spec(), "longhorn": "v1.13.0"}
        for change in ({"ip_family": "ipv6-dual"}, {"storage_network": True}, {"ubuntu": "26.04"},
                       {"image_source": "official"}, {"longhorn": "master-head"},
                       {"longhorn": "v1.12.4"}, {"k3s": "v1.33.9+k3s1"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "fresh cluster"):
                lab.refuse_recreation({**applied, **change}, applied)
        for change in ({"longhorn": "v1.14.0"}, {"longhorn": "v1.13.1-rc1"}, {"k3s": "v1.35.0+k3s1"},
                       {"k3s": "v1.34.2+k3s2"}):
            lab.refuse_recreation({**applied, **change}, applied)
        lab.APPLIED_SPEC.write_text(json.dumps(applied))
        with patch.object(lab, "tofu", return_value=""):
            self.assertIsNone(lab.applied_spec(), "no resources in state means nothing to protect")
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "tofu", return_value="incus_project.cluster\n"), \
             patch.object(lab, "plan_infrastructure") as planning:
            with self.assertRaisesRegex(ValueError, "ip_family 'ipv4-dual' -> 'ipv6-dual'"):
                lab.main(["apply", "--confirm", "lh-incus", "--ip-family", "ipv6-dual"])
            planning.assert_not_called()

    def test_confirmed_apply_uses_cluster_var_file_and_records_it(self):
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "plan_infrastructure", return_value=self.plans[0]) as planning, \
             patch.object(lab, "preflight", return_value="lh-incus"), \
             patch.object(lab, "inventory"), \
             patch.object(lab, "tofu") as command:
            lab.main(["apply", "--confirm", "lh-incus", "--storage-network"])
        planning.assert_called_once_with(var_file=lab.TF_SPEC)
        command.assert_called_once_with("apply", "-input=false", str(lab.PLAN))
        recorded = json.loads(lab.APPLIED_SPEC.read_text())
        self.assertEqual(set(recorded), set(lab.RECREATE_KEYS))
        self.assertTrue(recorded["storage_network"])
        with patch.object(lab, "tofu", side_effect=["", "{}"]) as command:
            lab.plan_infrastructure(var_file=lab.TF_SPEC)
        self.assertIn("-var-file=" + str(lab.TF_SPEC), command.call_args_list[0].args)

    def test_official_image_import_verifies_checksum(self):
        import hashlib
        import io
        payload = b"not really a disk image"
        name = "ubuntu-26.04-server-cloudimg-amd64.img"
        sums = (hashlib.sha256(payload).hexdigest() + " *" + name + "\n").encode()
        spec = lab.load_spec({"ubuntu": "26.04"})
        state = patch.object(lab, "STATE", self.scratch)
        state.start()
        self.addCleanup(state.stop)
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), \
                 patch.object(lab.platform, "machine", return_value="x86_64"), \
                 patch.object(lab.urllib.request, "urlopen", return_value=io.BytesIO(sums)), \
                 patch.object(lab.urllib.request, "urlretrieve",
                              side_effect=lambda url, path: Path(path).write_bytes(payload + (b"!" if corrupt else b""))), \
                 patch.object(lab, "run", return_value="[]") as command:
                if corrupt:
                    with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                        lab.import_image(spec)
                    self.assertEqual(command.call_count, 1, "nothing is imported after a mismatch")
                else:
                    lab.import_image(spec)
                    imported = [str(arg) for arg in command.call_args.args[0]]
                    self.assertEqual(imported[:3], ["incus", "image", "import"])
                    self.assertEqual(imported[-4:], ["--alias", "ubuntu-26.04-official", "--project", "default"])
        with patch.object(lab, "run", return_value=json.dumps(
                [{"fingerprint": "f" * 64, "aliases": [{"name": "ubuntu-26.04-official"}]}])) as command:
            lab.import_image(spec)
        self.assertEqual(command.call_count, 1, "an existing alias is not re-imported")

    def test_provision_passes_cluster_versions_last(self):
        with patch.object(lab, "inventory"), patch.object(lab, "run") as command:
            lab.provision(lab.load_spec({"k3s": "v1.35.0+k3s1"}))
        args = [str(arg) for arg in command.call_args.args[0]]
        self.assertEqual(args[-2:], ["--extra-vars", "@" + str(lab.ANSIBLE_SPEC)])
        self.assertEqual(json.loads(lab.ANSIBLE_SPEC.read_text())["k3s_version"], "v1.35.0+k3s1")

    def test_strict_longhorn_health_gate_with_ansible(self):
        playbook = yaml.safe_load((ROOT / "ansible/verify.yml").read_text())
        gate = next(task for task in playbook[1]["tasks"]
                    if task["name"] == "Require every Longhorn node and disk condition to be green")
        node = {"metadata": {"name": "test-worker"}, "status": {
            "conditions": [{"type": str(i), "status": "True"} for i in range(8)],
            "diskStatus": {name: {"conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Schedulable", "status": "True"}]} for name in ("v1", "v2")}}}
        hugepages_off = {"type": "HugePagesAvailable", "status": "False", "reason": "HugePagesNotConfigured"}
        # (case, hugepages enabled, expected to pass)
        cases = (("green", False, True),
                 ("extra-true-disk-condition", False, True),
                 ("hugepages-disabled", False, True),
                 ("hugepages-enabled", True, False),
                 ("smart-unsupported", False, True),
                 ("node-condition", False, False),
                 ("disk-ready", False, False))
        for case, hugepages, expected in cases:
            with self.subTest(case=case):
                sample = copy.deepcopy(node)
                conditions = sample["status"]["conditions"]
                disks = sample["status"]["diskStatus"]
                if case == "extra-true-disk-condition":
                    disks["v1"]["conditions"].append({"type": "HealthDataCollected", "status": "True"})
                elif case.startswith("hugepages"):
                    conditions[-1] = hugepages_off
                elif case == "smart-unsupported":
                    for disk in disks.values():
                        disk["conditions"].append({"type": "HealthDataCollected", "status": "False",
                                                   "reason": "HealthDataCollectionFailed"})
                elif case == "node-condition":
                    conditions[-1] = {"type": "Multipathd", "status": "False", "reason": "MultipathdIsRunning"}
                elif case == "disk-ready":
                    disks["v2"]["conditions"][0]["status"] = "False"
                play = [{"name": "Check strict node health", "hosts": "localhost",
                         "gather_facts": False, "vars": {"install_longhorn": True,
                             "incus_enable_v2": True, "v2_hugepages": hugepages,
                             "verification_longhorn_nodes": {"resources": [sample]}},
                         "tasks": [gate]}]
                path = lab.STATE / "node-health-test.yml"
                path.write_text(yaml.safe_dump(play), encoding="ascii")
                result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected, result.stdout)

    def test_master_head_manifest_customization_with_ansible(self):
        tasks = yaml.safe_load((ROOT / "ansible/roles/longhorn/tasks/manifest.yml").read_text())
        names = ("Parse the manifest documents",
                 "Customize default settings and the default StorageClass before first start",
                 "Order the namespace and customized ConfigMaps before Longhorn workloads",
                 "Require the expected master-head manifest content")
        selected = [task for task in tasks if task["name"] in names]
        self.assertEqual(len(selected), len(names))
        storageclass = {"kind": "StorageClass", "apiVersion": "storage.k8s.io/v1",
                        "metadata": {"name": "longhorn"},
                        "parameters": {"numberOfReplicas": "3", "dataEngine": "v1"}}
        upstream = [
            {"kind": "Namespace", "apiVersion": "v1", "metadata": {"name": "longhorn-system"}},
            {"kind": "CustomResourceDefinition", "apiVersion": "apiextensions.k8s.io/v1",
             "metadata": {"name": "nodes.longhorn.io"}},
            {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "longhorn-default-resource"},
             "data": {"default-resource.yaml": ""}},
            {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "longhorn-default-setting"},
             "data": {"default-setting.yaml": 'priority-class: "longhorn-critical"\n'}},
            {"kind": "ConfigMap", "apiVersion": "v1", "metadata": {"name": "longhorn-storageclass"},
             "data": {"storageclass.yaml": yaml.safe_dump(storageclass)}},
            {"kind": "DaemonSet", "apiVersion": "apps/v1", "metadata": {"name": "longhorn-manager"}},
        ]
        settings = {"create-default-disk-labeled-nodes": "true",
                    "data-engine-hugepage-enabled": '{"v2":"false"}',
                    "storage-network": "longhorn-system/incus-storage-network"}
        output = lab.STATE / "manifest-test.json"
        play = [{"name": "Check manifest customization", "hosts": "localhost", "gather_facts": False,
                 "vars": {"longhorn_manifest": {"content": yaml.safe_dump_all(upstream)},
                          "longhorn_settings": settings, "longhorn_replica_count": 2},
                 "tasks": selected + [{"name": "Write the result", "ansible.builtin.copy": {
                     "content": "{{ longhorn_manifest_documents | to_json }}",
                     "dest": str(output), "mode": "0600"}}]}]
        path = lab.STATE / "manifest-test.yml"
        path.write_text(yaml.safe_dump(play), encoding="ascii")
        subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                       cwd=ROOT, check=True, capture_output=True, text=True)
        documents = json.loads(output.read_text())
        self.assertEqual([d["kind"] for d in documents[:4]], ["Namespace", "ConfigMap", "ConfigMap", "ConfigMap"])
        self.assertEqual(len(documents), len(upstream))
        configmaps = {d["metadata"]["name"]: d["data"] for d in documents if d["kind"] == "ConfigMap"}
        merged = yaml.safe_load(configmaps["longhorn-default-setting"]["default-setting.yaml"])
        self.assertEqual(merged, {"priority-class": "longhorn-critical", **settings})
        rendered_class = yaml.safe_load(configmaps["longhorn-storageclass"]["storageclass.yaml"])
        self.assertEqual(rendered_class["parameters"], {"numberOfReplicas": "2", "dataEngine": "v1"})
        self.assertEqual(configmaps["longhorn-default-resource"], {"default-resource.yaml": ""})

    def test_k3s_checksum_selection_with_ansible(self):
        tasks = yaml.safe_load((ROOT / "ansible/roles/k3s/tasks/main.yml").read_text())
        selection = next(task for task in tasks if task["name"] == "Select the exact binary checksum")
        for artifact in ("k3s", "k3s-arm64"):
            with self.subTest(artifact=artifact):
                checksum = "a" * 64
                content = checksum + "  " + artifact + "\n" + "b" * 64 + " *" + artifact + "-other\n"
                play = [{"name": "Check checksum selection", "hosts": "localhost",
                         "gather_facts": False, "vars": {
                             "k3s_artifact": artifact, "k3s_checksums": {"content": content}},
                         "tasks": [selection, {"name": "Require exact checksum",
                                              "ansible.builtin.assert": {
                                                  "that": "k3s_checksum == ['" + checksum + "']"}}]}]
                path = lab.STATE / "checksum-test.yml"
                path.write_text(yaml.safe_dump(play), encoding="ascii")
                subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                               cwd=ROOT, check=True, capture_output=True, text=True)

    def test_k3s_and_route_templates_for_every_mode(self):
        rendered = lab.STATE / "rendered"
        rendered.mkdir(exist_ok=True)
        for plan in self.plans:
            inv = plan["planned_values"]["outputs"]["inventory"]["value"]["all"]
            for hostname in inv["vars"]["incus_peers"]:
                context = self.context(plan, hostname)
                key = context["incus_network_stack"] + "-" + hostname
                config = yaml.safe_load(self.render("ansible/roles/k3s/templates/config.yaml.j2", context))
                self.assertEqual(config["node-ip"], ",".join(context["incus_node_ips"]))
                self.assertEqual(config["token"], "offline-token")
                if context["incus_role"] == "server":
                    self.assertEqual(config["cluster-cidr"], ",".join(context["incus_pod_cidrs"]))
                    self.assertIn(context["incus_node_ips"][0], config["tls-san"])
                else:
                    self.assertEqual(config["server"], context["incus_server_url"])
                (rendered / (key + ".yml")).write_text(yaml.safe_dump(config))
                unit = self.render("ansible/roles/k3s/templates/k3s.service.j2", context)
                self.assertIn("\n[Service]\n", unit)
                self.assertIn("\nExecStart=/usr/local/bin/k3s ", unit)
                routes = self.render("ansible/roles/storage_network/templates/routes.sh.j2", context)
                self.assertEqual("ip -6 route replace" in routes, context["incus_network_stack"] != "ipv4")
                self.assertEqual("\nip route replace" in routes, context["incus_network_stack"] != "ipv6")
                self.assertNotIn("via  dev", routes)
                route_file = rendered / (key + ".sh")
                route_file.write_text(routes)
                subprocess.run(["bash", "-n", str(route_file)], check=True)
                subprocess.run(["shellcheck", str(route_file)], check=True)

    def test_backup_and_longhorn_templates(self):
        for plan in self.plans:
            hostname = next(iter(plan["planned_values"]["outputs"]["inventory"]["value"]["all"]["children"]["workers"]["hosts"]))
            for target in ("garage", "nfs", "none"):
                context = self.context(plan, hostname, target)
                values = yaml.safe_load(self.render("ansible/roles/longhorn/templates/values.yml.j2", context))
                self.assertEqual(values["defaultSettings"]["v2DataEngine"], context["incus_enable_v2"])
                self.assertEqual(values["defaultBackupStore"]["backupTarget"], context["longhorn_backup_url"])
                self.assertEqual(bool(values["defaultSettings"]["storageNetwork"]), context["incus_storage_network"])
                values_file = lab.STATE / "rendered" / (
                    "longhorn-" + context["incus_network_stack"] + "-" + target
                    + "-v2-" + str(context["incus_enable_v2"])
                    + "-storage-" + str(context["incus_storage_network"]) + ".yml")
                values_file.parent.mkdir(exist_ok=True)
                values_file.write_text(yaml.safe_dump(values))
                if target == "none":
                    continue
                manifests = list(yaml.safe_load_all(self.render(
                    "ansible/roles/backup/templates/" + target + ".yml.j2", context)))
                kinds = [manifest["kind"] for manifest in manifests]
                self.assertIn("Deployment", kinds)
                self.assertIn("Service", kinds)
                deployment = next(manifest for manifest in manifests if manifest["kind"] == "Deployment")
                self.assertEqual(deployment["spec"]["replicas"], 1)

    def test_swap_regex_and_k3s_checksum_regex(self):
        tasks = yaml.safe_load((ROOT / "ansible/roles/node/tasks/main.yml").read_text())
        task = tasks[-1]["ansible.builtin.replace"]
        line = "/swapfile none swap sw 0 0"
        self.assertEqual(re.sub(task["regexp"], task["replace"], line),
                         "# Disabled for Kubernetes: " + line)
        self.assertEqual(re.sub(task["regexp"], task["replace"], "# " + line), "# " + line)
        expression = yaml.safe_load((ROOT / "ansible/roles/k3s/tasks/main.yml").read_text())[2]["ansible.builtin.set_fact"]["k3s_checksum"]
        self.environment.filters["regex_findall"] = lambda value, pattern: re.findall(pattern, value)
        checksum = "a" * 64
        rendered = self.environment.from_string(expression).render(
            k3s_checksums={"content": checksum + "  k3s\n" + "b" * 64 + "  k3s-arm64\n"},
            k3s_artifact="k3s",
        )
        self.assertEqual(rendered, repr([checksum]))


if __name__ == "__main__":
    unittest.main()
