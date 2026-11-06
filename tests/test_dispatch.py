"""方舟调度 v1 的离线回归：5 组 golden case + 失败关闭 + 规则完整性。

不访问真实设备、不联网：台账与知识库都用 tests/fixtures 下的固定快照。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
FIXTURES = REPO / "tests" / "fixtures"
REGISTRY_PATH = FIXTURES / "registry.skills.json"
KB_PATH = FIXTURES / "kb"
RULES_PATH = SCRIPTS / "routing.json"

sys.path.insert(0, str(SCRIPTS))
import dispatch  # noqa: E402

REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))
KB_ENTRIES = dispatch.load_kb(KB_PATH)
BY_NAME = {skill["name"]: skill for skill in REGISTRY}


def make_plan(example: str, date: str = "20261107", registry=REGISTRY, kb_entries=KB_ENTRIES, registry_rev="4af4f6d"):
    request = json.loads((REPO / "examples" / example).read_text(encoding="utf-8"))
    return dispatch.build_plan(
        request,
        registry,
        kb_entries,
        RULES,
        date,
        registry_rev,
    )


def step_skills(plan: dict) -> list[str]:
    seen: list[str] = []
    for step in plan["steps"]:
        if not seen or seen[-1] != step["skill"]:
            seen.append(step["skill"])
    return seen


def excluded_map(plan: dict) -> dict[str, str]:
    return {item["skill"]: item["reason"] for item in plan["excluded"]}


class GoldenCase1TclOffline(unittest.TestCase):
    """TCL 离线唤醒 + 命令词：usb-tree → listenai-play → tcl-test-sub，不选 mideaSmoke。"""

    @classmethod
    def setUpClass(cls):
        cls.plan = make_plan("tcl-offline.json")

    def test_selection_and_order(self):
        self.assertEqual(step_skills(self.plan), ["usb-tree", "listenai-play", "tcl-test-sub"])
        phases = [step["phase"] for step in self.plan["steps"]]
        self.assertEqual(phases[0], "access")
        self.assertTrue(all(phase == "action" for phase in phases[1:3]))
        self.assertTrue(all(phase == "execute" for phase in phases[3:]))

    def test_device_specific_skills_are_excluded_with_reason(self):
        excluded = excluded_map(self.plan)
        self.assertIn("mideaSmoke", excluded, "设备型号不匹配的烧录 skill 必须出现在排除清单里")
        self.assertIn("CSK6012", excluded["mideaSmoke"])
        self.assertNotIn("mideaSmoke", step_skills(self.plan))
        self.assertNotIn("tcl-test-sub", excluded, "TCL 任务必须选中 tcl-test-sub")

    def test_plan_passes_self_validation(self):
        self.assertEqual(dispatch.validate_plan(self.plan, REGISTRY, RULES), [])

    def test_every_step_is_runnable(self):
        for step in self.plan["steps"]:
            self.assertTrue(step["cmd"])
            self.assertTrue(step["gate"])
            self.assertIn(step["on_fail"], dispatch.ON_FAIL_VALUES)
            self.assertTrue(step["rollback"])

    def test_kb_preconditions_injected(self):
        joined = " ".join(self.plan["preconditions"])
        self.assertIn("kb:kb-knowledge-0e8f85cddf7a", joined)
        self.assertIn("uut-switch1", joined)


class GoldenCase2Csk6012Burn(unittest.TestCase):
    """CSK6012 烧录：选 mideaSmoke，不选 tcl-test-sub，通用烧录实现让位。"""

    @classmethod
    def setUpClass(cls):
        cls.plan = make_plan("csk6012-burn.json")

    def test_selects_device_specific_burn_skill(self):
        self.assertEqual(step_skills(self.plan)[-1], "mideaSmoke")
        self.assertNotIn("tcl-test-sub", step_skills(self.plan))
        self.assertNotIn("cskflashtest", step_skills(self.plan))

    def test_ports_are_resolved_into_command(self):
        burn_steps = [step for step in self.plan["steps"] if step["skill"] == "mideaSmoke"]
        command = burn_steps[-1]["cmd"]
        for port in ("COM23", "COM27", "COM29"):
            self.assertIn(port, command)

    def test_exclusions_explain_device_boundary(self):
        excluded = excluded_map(self.plan)
        self.assertIn("tcl-test-sub", excluded)
        self.assertIn("TCL-ac", excluded["tcl-test-sub"])
        self.assertIn("cskflashtest", excluded)
        self.assertIn("mideaSmoke", excluded["cskflashtest"])

    def test_plan_passes_self_validation(self):
        self.assertEqual(dispatch.validate_plan(self.plan, REGISTRY, RULES), [])


class GoldenCase3Synthesis(unittest.TestCase):
    """需要新语料：计划里带 lstts，且理由引用知识库的合成规范。"""

    @classmethod
    def setUpClass(cls):
        cls.plan = make_plan("tts-corpus.json")

    def test_lstts_is_selected(self):
        self.assertIn("lstts", step_skills(self.plan))

    def test_material_step_comes_before_execute(self):
        skills = step_skills(self.plan)
        self.assertLess(skills.index("lstts"), skills.index("tcl-test-sub"))

    def test_kb_policy_is_cited_in_step_basis(self):
        basis = [item for step in self.plan["steps"] if step["skill"] == "lstts" for item in step["basis"]]
        self.assertTrue(any("kb:kb-knowledge-d81c3d1877cc" in item for item in basis))
        self.assertIn("kb:kb-knowledge-d81c3d1877cc", self.plan["sources"])

    def test_melotts_is_explained(self):
        excluded = excluded_map(self.plan)
        self.assertIn("melotts", excluded)
        self.assertTrue(excluded["melotts"])


class GoldenCase4Overnight(unittest.TestCase):
    """8 小时通宵：长跑命令带截止时长，计划带停止条件与巡检项。"""

    @classmethod
    def setUpClass(cls):
        cls.plan = make_plan("tcl-overnight.json")

    def test_long_run_command_has_deadline(self):
        long_run = [step for step in self.plan["steps"] if step["label"].startswith("正式长跑")]
        self.assertEqual(len(long_run), 1, "通宵任务必须生成一条长跑步骤")
        command = long_run[0]["cmd"]
        self.assertIn("--duration-minutes 480", command)
        self.assertIn("--rounds 100000", command)
        labels = [step["label"] for step in self.plan["steps"]]
        self.assertNotIn("正式短跑（默认 20 轮）", labels)

    def test_stop_conditions_and_patrol(self):
        joined = " ".join(self.plan["stop_conditions"])
        self.assertIn("180s", joined)
        self.assertIn("连续崩溃 ≥3 次/30min", joined)
        self.assertIn("STOP", joined)
        self.assertGreaterEqual(len(self.plan["patrol"]), 3)
        self.assertTrue(any("progress.json" in item for item in self.plan["patrol"]))
        self.assertTrue(any("磁盘" in item for item in self.plan["preconditions"]))

    def test_long_run_preconditions_include_reset_channel(self):
        joined = " ".join(self.plan["preconditions"])
        self.assertIn("reset_log.jsonl", joined)


class GoldenCase5UnknownDevice(unittest.TestCase):
    """设备型号未知：confidence low + 要求确认，不猜设备专用 skill。"""

    @classmethod
    def setUpClass(cls):
        cls.plan = make_plan("unknown-device.json")

    def test_confidence_is_low_and_confirmation_required(self):
        self.assertEqual(self.plan["confidence"], "low")
        self.assertTrue(self.plan["needs_confirmation"])
        joined = " ".join(self.plan["needs_confirmation"])
        self.assertIn("设备型号", joined)

    def test_no_device_bound_skill_is_guessed(self):
        for skill in step_skills(self.plan):
            self.assertNotEqual(BY_NAME[skill].get("reuse"), "low", f"{skill} 是设备专用 skill，型号未知时不得硬选")
            self.assertNotEqual(BY_NAME[skill].get("status"), "archived")

    def test_low_confidence_plan_still_validates(self):
        self.assertEqual(dispatch.validate_plan(self.plan, REGISTRY, RULES), [])


class PlanContract(unittest.TestCase):
    """plan.v1 的结构契约与确定性。"""

    def test_plan_has_required_fields(self):
        plan = make_plan("tcl-offline.json")
        for key in (
            "schema",
            "plan_id",
            "task",
            "confidence",
            "steps",
            "excluded",
            "preconditions",
            "stop_conditions",
            "sources",
        ):
            self.assertIn(key, plan)
        self.assertRegex(plan["plan_id"], r"^p-\d{8}-[0-9a-f]{4}$")

    def test_same_request_is_deterministic(self):
        first = make_plan("tcl-offline.json")
        second = make_plan("tcl-offline.json")
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(json.dumps(first["steps"], ensure_ascii=False), json.dumps(second["steps"], ensure_ascii=False))

    def test_sources_cite_registry_and_kb(self):
        plan = make_plan("tcl-offline.json")
        self.assertTrue(any(source.startswith("registry/skills.json@") for source in plan["sources"]))
        self.assertTrue(any(source.startswith("kb:") for source in plan["sources"]))

    def test_no_skill_outside_registry(self):
        for example in (
            "tcl-offline.json",
            "csk6012-burn.json",
            "tts-corpus.json",
            "tcl-overnight.json",
            "unknown-device.json",
        ):
            plan = make_plan(example)
            for step in plan["steps"]:
                self.assertIn(step["skill"], BY_NAME)
            for item in plan["excluded"]:
                self.assertIn(item["skill"], BY_NAME)

    def test_kb_injection_is_capped_at_five(self):
        plan = make_plan("tcl-offline.json")
        self.assertLessEqual(len(plan["kb"]), 5)

    def test_limits_state_no_execution(self):
        plan = make_plan("tcl-offline.json")
        self.assertTrue(any("不执行设备动作" in item for item in plan["limits"]))


class ValidationGuards(unittest.TestCase):
    """校验器必须拦住被篡改的计划。"""

    def setUp(self):
        self.plan = make_plan("tcl-offline.json")

    def test_rejects_archived_skill_step(self):
        broken = json.loads(json.dumps(self.plan))
        broken["steps"][0]["skill"] = "polaris-device-validation"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("archived" in problem for problem in problems))

    def test_rejects_step_outside_registry(self):
        broken = json.loads(json.dumps(self.plan))
        broken["steps"][0]["skill"] = "not-in-registry"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("不在台账" in problem for problem in problems))

    def test_rejects_low_confidence_without_confirmation(self):
        broken = json.loads(json.dumps(self.plan))
        broken["confidence"] = "low"
        broken["needs_confirmation"] = []
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("needs_confirmation" in problem for problem in problems))

    def test_rejects_exclusion_without_reason(self):
        broken = json.loads(json.dumps(self.plan))
        broken["excluded"][0]["reason"] = ""
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("理由" in problem for problem in problems))

    def test_rejects_markdown_only_skill_as_step(self):
        broken = json.loads(json.dumps(self.plan))
        broken["steps"][0]["skill"] = "trails"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any(".md" in problem for problem in problems))


class RulesConsistency(unittest.TestCase):
    """routing.json 不能引用台账里不存在的 skill。"""

    def test_rules_match_registry(self):
        self.assertEqual(dispatch.check_rules(RULES, REGISTRY), [])

    def test_planner_has_no_network_or_device_access(self):
        source = (SCRIPTS / "dispatch.py").read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import socket", "import serial", "http://", "https://"):
            self.assertNotIn(forbidden, source, f"调度器不得联网或访问设备：出现 {forbidden}")

    def test_every_template_has_verifiable_command(self):
        for name, templates in RULES["steps"].items():
            for template in templates:
                self.assertIn('{skill_dir}', template['cmd'], f'{name} 的 cmd 必须锚定 skill 安装根目录')
                self.assertNotIn("TODO", template["cmd"])

    def test_roles_are_covered(self):
        for role in RULES["roles"]:
            hits = [skill["name"] for skill in REGISTRY if dispatch.matched_role_keywords(skill, role, RULES)]
            self.assertTrue(hits, f"角色 {role} 在台账里没有可用 skill")


class FailClosed(unittest.TestCase):
    """台账/知识库缺失或请求非法时，CLI 必须非 0 退出，不能静默继续。"""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "dispatch.py"), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )

    def test_missing_registry_exits_3(self):
        result = self.run_cli(
            "plan",
            "--request",
            str(REPO / "examples" / "tcl-offline.json"),
            "--registry",
            "/nonexistent/skills.json",
            "--kb",
            str(KB_PATH),
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("台账", result.stderr)

    def test_missing_kb_exits_3(self):
        result = self.run_cli(
            "plan",
            "--request",
            str(REPO / "examples" / "tcl-offline.json"),
            "--registry",
            str(REGISTRY_PATH),
            "--kb",
            "/nonexistent/kb",
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("知识库", result.stderr)

    def test_missing_kb_can_be_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            result = self.run_cli(
                "plan",
                "--request",
                str(REPO / "examples" / "tcl-offline.json"),
                "--registry",
                str(REGISTRY_PATH),
                "--kb",
                "/nonexistent/kb",
                "--allow-missing-kb",
                "--out",
                str(out),
                "--date",
                "20261107",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotEqual(plan["confidence"], "high")
            self.assertTrue(any("知识库不可用" in warning for warning in plan["warnings"]))

    def test_invalid_request_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "bad.json"
            request.write_text(json.dumps({"device": {}}), encoding="utf-8")
            result = self.run_cli(
                "plan",
                "--request",
                str(request),
                "--registry",
                str(REGISTRY_PATH),
                "--kb",
                str(KB_PATH),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("task", result.stderr)

    def test_require_confidence_blocks_low(self):
        result = self.run_cli(
            "plan",
            "--request",
            str(REPO / "examples" / "unknown-device.json"),
            "--registry",
            str(REGISTRY_PATH),
            "--kb",
            str(KB_PATH),
            "--require-confidence",
            "high",
        )
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_check_subcommand_accepts_generated_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            generated = self.run_cli(
                "plan",
                "--request",
                str(REPO / "examples" / "tcl-offline.json"),
                "--registry",
                str(REGISTRY_PATH),
                "--kb",
                str(KB_PATH),
                "--registry-rev",
                "4af4f6d",
                "--date",
                "20261107",
                "--out",
                str(out),
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            checked = self.run_cli("check", "--plan", str(out), "--registry", str(REGISTRY_PATH))
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("OK", checked.stdout)


if __name__ == "__main__":
    unittest.main()
