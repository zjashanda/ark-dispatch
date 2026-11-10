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


class GatePorts(unittest.TestCase):
    """gate 的端口必须来自请求 bench.ports，不能被 skill 默认值覆盖。

    回归 NOAH-20：请求 ports=COM3/COM4/COM6，gate 却写死 tcl-test-sub 的 COM20/COM22。
    """

    @staticmethod
    def hardware_preflight(plan: dict) -> dict:
        steps = [
            step
            for step in plan["steps"]
            if step["skill"] == "tcl-test-sub" and step["label"] == "硬件 preflight"
        ]
        assert len(steps) == 1, [step["label"] for step in plan["steps"]]
        return steps[0]

    @staticmethod
    def request_without_ports(example: str = "tcl-offline.json") -> dict:
        request = json.loads((REPO / "examples" / example).read_text(encoding="utf-8"))
        request["bench"].pop("ports", None)
        return request

    def test_gate_uses_request_ports_not_skill_defaults(self):
        plan = make_plan("tcl-overnight.json")
        step = self.hardware_preflight(plan)
        self.assertEqual(step["n"], 5, "验收口径：通宵计划的第 5 步是硬件 preflight")
        gate = step["gate"]
        for port in ("COM3", "COM4", "COM6"):
            self.assertIn(port, gate)
        for port in ("COM20", "COM22"):
            self.assertNotIn(port, gate, f"gate 不得出现 skill 默认端口：{port}")
        self.assertIn("来源：bench.ports", gate)

    def test_voice_test_precondition_uses_request_ports(self):
        plan = make_plan("tcl-offline.json")
        joined = " ".join(plan["preconditions"])
        self.assertIn("COM3/COM4/COM6", joined)
        self.assertNotIn("COM20", joined)
        self.assertNotIn("COM22", joined)

    def test_falls_back_to_skill_default_with_source_label(self):
        plan = dispatch.build_plan(
            self.request_without_ports(), REGISTRY, KB_ENTRIES, RULES, "20261107", "4af4f6d"
        )
        gate = self.hardware_preflight(plan)["gate"]
        self.assertIn("COM20", gate)
        self.assertIn("COM22", gate)
        self.assertIn("来源：default", gate)
        self.assertNotEqual(plan["confidence"], "high", "回落默认端口必须降置信度")
        self.assertTrue(any("默认端口" in item for item in plan["needs_confirmation"]))
        self.assertTrue(any("skill 默认端口" in item for item in plan["warnings"]))
        precondition = " ".join(plan["preconditions"])
        self.assertIn("COM20/COM22（来源：default", precondition)
        self.assertNotIn("来源未知", precondition, "前置检查也必须按真正使用端口的 skill 查默认值")

    def test_unknown_port_source_is_explicit(self):
        rules = json.loads(json.dumps(RULES))
        rules["port_defaults"].pop("tcl-test-sub", None)
        plan = dispatch.build_plan(
            self.request_without_ports(), REGISTRY, KB_ENTRIES, rules, "20261107", "4af4f6d"
        )
        gate = self.hardware_preflight(plan)["gate"]
        self.assertIn("来源未知", gate)
        self.assertTrue(any("端口来源未知" in item for item in plan["needs_confirmation"]))
        self.assertNotEqual(plan["confidence"], "high")


class ResetPolicy(unittest.TestCase):
    """reset_allowed 的口径：允许复位 ≠ 调度层排一条复位步骤。"""

    def test_allowed_true_declares_recovery_without_extra_step(self):
        plan = make_plan("tcl-overnight.json")
        policy = plan["reset_policy"]
        self.assertTrue(policy["allowed"])
        self.assertIn("不单独排复位步骤", policy["decision"])
        self.assertFalse([step for step in plan["steps"] if "复位" in step["label"]])
        channel = {item["skill"]: item for item in policy["channels"]}["tcl-test-sub"]
        self.assertEqual(channel["command"], "uut-switch1")
        self.assertFalse(channel["verified"], "uut-switch1 尚无真机证据，不能标已验证")
        self.assertIn("reset_log.jsonl", channel["log"])
        self.assertIn("实测有效", policy["requirement"])
        self.assertTrue(any("复位通道" in item for item in plan["needs_confirmation"]))
        self.assertNotEqual(plan["confidence"], "high", "未验证的复位通道必须要求人确认")

    def test_disallowed_forbids_reset(self):
        plan = make_plan("tcl-offline.json")
        policy = plan["reset_policy"]
        self.assertFalse(policy["allowed"])
        self.assertIn("禁止复位", policy["decision"])
        self.assertEqual(policy["channels"], [])


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
            "stop_rules",
            "recovery",
            "reset_policy",
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


class EvidenceGapIsolation(unittest.TestCase):
    """证据不全的候选条目不得进入调度依据。"""

    def write_kb(self, directory, entries):
        target = directory / "entries" / "knowledge"
        target.mkdir(parents=True, exist_ok=True)
        with open(target / "entries.jsonl", "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entry(self, entry_id, tags, status="pending"):
        return {
            "schema": "entry.v1",
            "id": entry_id,
            "type": "knowledge",
            "scope": {"device": None, "project": None, "firmware": None, "platform": None},
            "title": entry_id,
            "claim": "占位",
            "evidence": [],
            "confidence": "low",
            "status": status,
            "tags": tags,
            "validations": [],
        }

    def test_gap_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_kb(
                root,
                [
                    self.entry("kb-knowledge-gap", ["evidence-gap"]),
                    self.entry("kb-knowledge-ok", ["台架"]),
                ],
            )
            ids = [entry["id"] for entry in dispatch.load_kb(root)]
            self.assertNotIn("kb-knowledge-gap", ids)
            self.assertIn("kb-knowledge-ok", ids)


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


# --------------------------------------------------------------------------- #
# 第二阶段消费：trap / knowledge → recovery + stop_rules（NOAH-27）
# --------------------------------------------------------------------------- #

TRAP_CLAIM = (
    "长跑中 13/14 次崩溃都发生在 round 19，崩溃后 CSK 停在 boot 的 app client 创建阶段不再前进；"
    "唯一被验证有效的恢复手段是向 CSK shell 发送 algo.restart 1（0.2 秒恢复）。"
)

ROUND_RULE = "连续 3 段都在同一轮次崩溃 → 判设备不稳定并停止"
SIGNATURE_RULE = "同一崩溃签名出现 ≥5 次 → 停止并出崩溃分布报告"


def make_entry(
    entry_id: str,
    *,
    entry_type: str = "bug",
    device="TCL-DUT",
    project="TCL",
    title="CSK 崩溃规律与恢复手段",
    claim=TRAP_CLAIM,
    tags=(),
) -> dict:
    """构造一条知识条目（字段与 ark-kb entry.v1 对齐，不写盘、不入库）。"""
    return {
        "schema": "entry.v1",
        "id": entry_id,
        "type": entry_type,
        "scope": {"device": device, "project": project, "firmware": None, "platform": "windows"},
        "title": title,
        "claim": claim,
        "evidence": [{"kind": "issue", "ref": "NOAH-27", "note": "单测构造的条目"}],
        "confidence": "medium",
        "status": "active",
        "superseded_by": None,
        "source_skill": None,
        "created": "2026-11-08T00:00:00Z",
        "updated": "2026-11-08T00:00:00Z",
        "hits": 0,
        "validations": [],
        "tags": list(tags),
    }


def build_with(entries, example="tcl-overnight.json", rules=None, request=None, date="20261108"):
    if request is None:
        request = json.loads((REPO / "examples" / example).read_text(encoding="utf-8"))
    return dispatch.build_plan(
        request,
        REGISTRY,
        entries,
        rules if rules is not None else RULES,
        date,
        "4af4f6d",
    )


class KbTrapBecomesActions(unittest.TestCase):
    """trap 条目必须同时变成恢复步骤与停止条件，且每条都带 kb 出处。"""

    @classmethod
    def setUpClass(cls):
        cls.entries = [make_entry("kb-bug-fixture-round19")]
        cls.plan = build_with(cls.entries)
        cls.recovery = cls.plan["recovery"]
        cls.stop_rules = {item["rule"]: item for item in cls.plan["stop_rules"]}

    def test_recovery_step_is_generated_from_trap(self):
        self.assertEqual(len(self.recovery), 1, self.recovery)
        step = self.recovery[0]
        self.assertEqual(step["n"], "R1")
        self.assertEqual(step["skill"], "tcl-test-sub")
        self.assertIn("algo.restart 1", step["cmd"])
        self.assertIn("COM3", step["cmd"])
        self.assertIn("921600", step["cmd"])
        self.assertEqual(step["record"], "reset_log.jsonl")
        self.assertTrue(step["after_fail"])
        self.assertTrue(step["verify"])
        self.assertFalse(step["cmd_verified"], "发送方式未在真机验证过，不能标已验证")

    def test_recovery_cites_the_trap_entry(self):
        step = self.recovery[0]
        self.assertEqual(step["basis"], "kb:kb-bug-fixture-round19")
        self.assertIn("kb:kb-bug-fixture-round19", step["why"])
        self.assertIn("kb:kb-bug-fixture-round19", self.plan["sources"])
        ids = [note["id"] for note in self.plan["kb"]]
        self.assertIn("kb-bug-fixture-round19", ids, "被引用的条目必须出现在 plan.kb 里")

    def test_round_cluster_stop_rule_is_generated_from_entry(self):
        self.assertIn(ROUND_RULE, self.stop_rules)
        rule = self.stop_rules[ROUND_RULE]
        self.assertEqual(rule["source"], "kb", "条目自己写了 round 19，规则出处应是 kb 而不是默认检测器")
        self.assertEqual(rule["basis"], "kb:kb-bug-fixture-round19")

    def test_every_stop_rule_carries_kb_basis(self):
        self.assertTrue(self.stop_rules, "有 trap 条目时 stop_rules 不能为空")
        for rule in self.plan["stop_rules"]:
            self.assertTrue(rule["basis"].startswith("kb:"), rule)
            self.assertIn(rule["basis"], self.plan["sources"], rule)
            self.assertIn(rule["source"], {"kb", "default"})
            self.assertTrue(rule["rule"])

    def test_legacy_fields_are_untouched(self):
        baseline = build_with([])
        for key in ("steps", "stop_conditions", "patrol", "reset_policy", "excluded"):
            self.assertEqual(
                json.dumps(baseline[key], ensure_ascii=False, sort_keys=True),
                json.dumps(self.plan[key], ensure_ascii=False, sort_keys=True),
                f"{key} 必须与无 kb 时一致：新增字段不能改写旧字段",
            )

    def test_plan_passes_self_validation(self):
        self.assertEqual(dispatch.validate_plan(self.plan, REGISTRY, RULES), [])

    def test_text_rendering_shows_recovery_and_stop_rules(self):
        text = dispatch.render_text(self.plan)
        self.assertIn("恢复预案", text)
        self.assertIn("algo.restart 1", text)
        self.assertIn(ROUND_RULE, text)


class KbActionsAreConservative(unittest.TestCase):
    """不为了"看起来生效"硬塞：没有 kb、没有映射、设备不符时都不生成动作。"""

    def test_without_kb_both_lists_are_empty(self):
        plan = build_with([])
        self.assertEqual(plan["recovery"], [])
        self.assertEqual(plan["stop_rules"], [])

    def test_unmappable_knowledge_stays_in_kb_only(self):
        entry = make_entry(
            "kb-knowledge-fixture-venv",
            entry_type="knowledge",
            title="仓库提交了 .venv",
            claim="仓库根目录存在 .venv/，发布前应清理并补 .gitignore。",
            tags=("venv", "发布门禁"),
        )
        plan = build_with([entry])
        self.assertEqual(plan["recovery"], [])
        self.assertEqual(plan["stop_rules"], [])
        self.assertIn("kb-knowledge-fixture-venv", [note["id"] for note in plan["kb"]])

    def test_reset_disallowed_suppresses_recovery(self):
        plan = build_with([make_entry("kb-bug-fixture-round19")], example="tcl-offline.json")
        self.assertFalse(plan["request"]["constraints"]["reset_allowed"])
        self.assertEqual(plan["recovery"], [])
        self.assertTrue(any("reset_allowed=false" in item for item in plan["warnings"]), plan["warnings"])

    def test_other_project_does_not_inherit_tcl_trap(self):
        plan = build_with([make_entry("kb-bug-fixture-round19")], example="csk6012-burn.json")
        self.assertEqual(plan["recovery"], [], "TCL 的 trap 不能套到 CSK6012 台架上")
        self.assertEqual(plan["stop_rules"], [])

    def test_recovery_requires_selected_skill(self):
        rules = json.loads(json.dumps(RULES, ensure_ascii=False))
        rules["kb_actions"]["recovery"][0]["skill"] = "mideaSmoke"
        plan = build_with([make_entry("kb-bug-fixture-round19")], rules=rules)
        self.assertEqual(plan["recovery"], [], "没有选中执行 skill 时不得凭空生成恢复步骤")
        self.assertTrue(any("没有选中它" in item for item in plan["warnings"]), plan["warnings"])

    def test_default_port_must_be_confirmed(self):
        request = json.loads((REPO / "examples" / "tcl-overnight.json").read_text(encoding="utf-8"))
        request["bench"].pop("ports", None)
        plan = build_with([make_entry("kb-bug-fixture-round19")], request=request)
        step = plan["recovery"][0]
        self.assertEqual(step["port"], "COM20", "请求没给端口时回落到 tcl-test-sub 的 CSK AP 默认口")
        self.assertEqual(step["port_source"], "default")
        self.assertNotEqual(plan["confidence"], "high")
        self.assertTrue(any("默认值" in item for item in plan["needs_confirmation"]), plan["needs_confirmation"])

    def test_two_entries_with_same_command_are_deduplicated(self):
        bug = make_entry("kb-bug-fixture-round19")
        method = make_entry(
            "kb-method-fixture-recover",
            entry_type="method",
            title="用 algo.restart 1 + 1 轮冒烟做两级恢复判定",
            claim="watchdog 判静默后发 algo.restart 1，等 10 秒看是否重新开口，再跑 1 轮 smoke。",
        )
        plan = build_with([bug, method])
        self.assertEqual(len(plan["recovery"]), 1, "同一条命令只生成一个步骤")
        step = plan["recovery"][0]
        self.assertEqual(step["basis"], "kb:kb-bug-fixture-round19")
        self.assertIn("kb:kb-method-fixture-recover", step["supporting"])
        rules = {item["rule"]: item for item in plan["stop_rules"]}
        self.assertTrue(any("algo.restart 1" in rule["rule"] for rule in rules.values()))


class KbActionGuards(unittest.TestCase):
    """校验器必须拦住被篡改的 recovery / stop_rules。"""

    def setUp(self):
        self.plan = build_with([make_entry("kb-bug-fixture-round19")])

    def test_rejects_recovery_without_kb_basis(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["recovery"][0]["basis"] = "registry:tcl-test-sub"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("recovery" in problem and "kb:" in problem for problem in problems), problems)

    def test_rejects_recovery_basis_missing_from_sources(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["recovery"][0]["basis"] = "kb:kb-not-referenced-anywhere"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("不在 sources" in problem for problem in problems), problems)

    def test_rejects_recovery_numbering_gap(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["recovery"][0]["n"] = "R2"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("n 必须是 R1" in problem for problem in problems), problems)

    def test_rejects_archived_skill_in_recovery(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["recovery"][0]["skill"] = "polaris-device-validation"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("archived" in problem for problem in problems), problems)

    def test_rejects_stop_rule_without_kb_basis(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["stop_rules"][0]["basis"] = "routing:long_run_stop_conditions"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("stop_rules" in problem and "kb:" in problem for problem in problems), problems)

    def test_rejects_stop_rule_with_unknown_source(self):
        broken = json.loads(json.dumps(self.plan, ensure_ascii=False))
        broken["stop_rules"][0]["source"] = "model"
        problems = dispatch.validate_plan(broken, REGISTRY, RULES)
        self.assertTrue(any("source" in problem for problem in problems), problems)

    def test_old_plan_without_new_fields_still_validates(self):
        legacy = json.loads(json.dumps(self.plan, ensure_ascii=False))
        legacy.pop("recovery")
        legacy.pop("stop_rules")
        self.assertEqual(dispatch.validate_plan(legacy, REGISTRY, RULES), [])


class KbActionRulesConsistency(unittest.TestCase):
    """kb_actions 映射表本身要与台账、端口角色、正则一致。"""

    def test_kb_actions_match_registry(self):
        self.assertEqual(dispatch.check_rules(RULES, REGISTRY), [])

    def test_detects_broken_recovery_spec(self):
        rules = json.loads(json.dumps(RULES, ensure_ascii=False))
        spec = rules["kb_actions"]["recovery"][0]
        spec["skill"] = "not-in-registry"
        spec["command_regex"] = "(unclosed"
        problems = dispatch.check_rules(rules, REGISTRY)
        self.assertTrue(any("不在台账中" in problem for problem in problems), problems)
        self.assertTrue(any("command_regex" in problem for problem in problems), problems)

    def test_detects_port_role_not_declared(self):
        rules = json.loads(json.dumps(RULES, ensure_ascii=False))
        rules["kb_actions"]["recovery"][0]["port_role"] = "flash"
        problems = dispatch.check_rules(rules, REGISTRY)
        self.assertTrue(any("port_role" in problem for problem in problems), problems)


ARK_KB = REPO.parent / "ark-kb"


@unittest.skipUnless((ARK_KB / "entries").is_dir(), "需要同级 ark-kb 检出（真实知识库回归）")
class RealKbConsumption(unittest.TestCase):
    """真实知识库快照的回归：有 kb 时出恢复预案，无 kb 时为空。"""

    @classmethod
    def setUpClass(cls):
        cls.entries = dispatch.load_kb(ARK_KB)
        cls.with_kb = build_with(cls.entries)
        cls.without_kb = build_with([])

    def test_real_kb_yields_algo_restart_recovery(self):
        commands = [step["cmd"] for step in self.with_kb["recovery"]]
        self.assertTrue(any("algo.restart 1" in command for command in commands), commands)
        for step in self.with_kb["recovery"]:
            self.assertTrue(step["basis"].startswith("kb:"), step)

    def test_real_kb_yields_targeted_stop_rules(self):
        rules = [item["rule"] for item in self.with_kb["stop_rules"]]
        self.assertIn(ROUND_RULE, rules)
        for item in self.with_kb["stop_rules"]:
            self.assertTrue(item["basis"].startswith("kb:"), item)

    def test_without_kb_nothing_changes_execution(self):
        self.assertEqual(self.without_kb["recovery"], [])
        self.assertEqual(self.without_kb["stop_rules"], [])

    def test_real_kb_plan_self_validates(self):
        self.assertEqual(dispatch.validate_plan(self.with_kb, REGISTRY, RULES), [])


if __name__ == "__main__":
    unittest.main()
