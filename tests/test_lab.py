"""Offline integration tests driven by the actual OpenTofu mock plans."""

import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
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

    def test_unconfirmed_destroy_never_plans_or_applies(self):
        with patch.object(lab.shutil, "which", return_value="/bin/tool"), \
             patch.object(lab, "tofu", return_value='{"project":"lh-incus"}') as command, \
             patch.object(lab, "plan_infrastructure") as planning:
            with self.assertRaises(ValueError):
                lab.main(["destroy"])
            planning.assert_not_called()
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[:2], ("output", "-json"))

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
