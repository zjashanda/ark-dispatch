"""Skill 索引路由（scripts/route.py）的离线回归。

不联网、不碰设备：用 tests/fixtures/skill-index 下的两份最小索引做结构与路由判定。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
FIXTURES = REPO / "tests" / "fixtures" / "skill-index"

sys.path.insert(0, str(SCRIPTS))
import route  # noqa: E402

BURNER = json.loads((FIXTURES / "burner.index.json").read_text(encoding="utf-8"))
LSTEST = json.loads((FIXTURES / "lstest.index.json").read_text(encoding="utf-8"))


def run_route(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "route.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class IndexCheck(unittest.TestCase):
    def test_fixture_indexes_are_valid(self):
        self.assertEqual(route.check_index(BURNER), [])
        self.assertEqual(route.check_index(LSTEST), [])

    def test_missing_route_field_fails(self):
        broken = json.loads(json.dumps(BURNER))
        del broken["capabilities"][0]["preconditions"]
        problems = route.check_index(broken)
        self.assertTrue(any("preconditions" in problem for problem in problems))

    def test_unknown_field_fails(self):
        broken = json.loads(json.dumps(BURNER))
        broken["capabilities"][0]["extra"] = "x"
        problems = route.check_index(broken)
        self.assertTrue(any("未声明字段" in problem for problem in problems))

    def test_dangling_workflow_fails(self):
        broken = json.loads(json.dumps(BURNER))
        broken["capabilities"][0]["workflow"] = "wf.missing"
        problems = route.check_index(broken)
        self.assertTrue(any("wf.missing" in problem for problem in problems))

    def test_skill_mismatch_fails(self):
        broken = json.loads(json.dumps(LSTEST))
        broken["capabilities"][0]["skill"] = "burner"
        problems = route.check_index(broken)
        self.assertTrue(any("不一致" in problem for problem in problems))


class RouteResolve(unittest.TestCase):
    def setUp(self):
        self.indexes = {"burner": BURNER, "lstest": LSTEST}

    def test_burner_route_passes_on_measured_profile(self):
        plan = route.resolve(self.indexes, "burn.fixture-cap", "fixture-chip", "fixture-project", None, False)
        self.assertEqual(plan["route_status"], "MATCHED")
        self.assertEqual(plan["gate"], "PASS")
        self.assertEqual(plan["steps"][0]["skill"], "burner")

    def test_lstest_route_blocked_by_draft_contract(self):
        plan = route.resolve(self.indexes, "test.fixture-cap", "fixture-chip", "fixture-project", None, False)
        self.assertEqual(plan["route_status"], "MATCHED")
        self.assertEqual(plan["gate"], "BLOCKED_PROFILE_CONTRACT")

    def test_lstest_readonly_allowed_on_draft_contract(self):
        plan = route.resolve(self.indexes, "test.fixture-cap", "fixture-chip", "fixture-project", None, True)
        self.assertEqual(plan["gate"], "PASS")
        self.assertTrue(any("draft" in note for note in plan["notes"]))

    def test_formal_project_passes(self):
        plan = route.resolve(self.indexes, "test.fixture-cap", "fixture-chip", "fixture-formal", None, False)
        self.assertEqual(plan["gate"], "PASS")

    def test_unknown_project_blocks(self):
        plan = route.resolve(self.indexes, "burn.fixture-any", "fixture-chip", "no-such-project", None, False)
        self.assertEqual(plan["route_status"], "MATCHED")
        self.assertEqual(plan["gate"], "BLOCKED_UNKNOWN_PROJECT")

    def test_prefix_without_filter_is_ambiguous(self):
        plan = route.resolve(self.indexes, "burn", None, None, None, False)
        self.assertEqual(plan["route_status"], "AMBIGUOUS")
        self.assertEqual(plan["gate"], "BLOCKED_AMBIGUOUS_ROUTE")

    def test_no_match_is_explicit(self):
        plan = route.resolve(self.indexes, "test.nothing", None, None, None, False)
        self.assertEqual(plan["route_status"], "NO_MATCH")
        self.assertEqual(plan["gate"], "BLOCKED_CAPABILITY_NOT_FOUND")

    def test_gap_capability_is_not_executable(self):
        gap = json.loads(json.dumps(LSTEST))
        gap["capabilities"][0]["status"] = "gap"
        plan = route.resolve({"lstest": gap}, "test.fixture-cap", "fixture-chip", "fixture-formal", None, False)
        self.assertEqual(plan["gate"], "BLOCKED_CAPABILITY_GAP")


class RouteCompose(unittest.TestCase):
    def setUp(self):
        self.indexes = {"burner": BURNER, "lstest": LSTEST}

    def test_compose_marks_handoff_and_no_duplicate(self):
        burn = route.resolve(self.indexes, "burn.fixture-cap", "fixture-chip", "fixture-project", None, False)
        test = route.resolve(self.indexes, "test.fixture-cap", "fixture-chip", "fixture-formal", None, False)
        plan = route.compose([burn, test])
        self.assertEqual(plan["gate"], "PASS")
        self.assertEqual([step["step"] for step in plan["steps"]], [1, 2])
        self.assertEqual(len(plan["handoffs"]), 1)
        self.assertEqual(plan["handoffs"][0]["from_skill"], "burner")
        self.assertEqual(plan["duplicate_capabilities"], [])

    def test_compose_surfaces_blocking_step(self):
        burn = route.resolve(self.indexes, "burn.fixture-cap", "fixture-chip", "fixture-project", None, False)
        test = route.resolve(self.indexes, "test.fixture-cap", "fixture-chip", "fixture-project", None, False)
        plan = route.compose([burn, test])
        self.assertEqual(plan["gate"], "BLOCKED_PROFILE_CONTRACT")
        self.assertEqual(len(plan["blocked_steps"]), 1)


class CliContract(unittest.TestCase):
    def test_check_cli_ok(self):
        result = run_route(
            "check",
            "--index", f"burner={FIXTURES / 'burner.index.json'}",
            "--index", f"lstest={FIXTURES / 'lstest.index.json'}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resolve_cli_writes_plan_and_exit_code_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            result = run_route(
                "resolve",
                "--capability", "test.fixture-cap",
                "--chip", "fixture-chip",
                "--project", "fixture-project",
                "--index", f"burner={FIXTURES / 'burner.index.json'}",
                "--index", f"lstest={FIXTURES / 'lstest.index.json'}",
                "--out", str(out),
            )
            self.assertEqual(result.returncode, route.EXIT_BLOCKED, result.stderr)
            plan = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(plan["gate"], "BLOCKED_PROFILE_CONTRACT")

    def test_per_capability_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            result = run_route(
                "resolve",
                "--capability", "burn.fixture-cap#chip=fixture-chip,project=fixture-project",
                "--capability", "test.fixture-cap#chip=fixture-chip,project=fixture-formal",
                "--index", f"burner={FIXTURES / 'burner.index.json'}",
                "--index", f"lstest={FIXTURES / 'lstest.index.json'}",
                "--out", str(out),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(plan["gate"], "PASS")
            self.assertEqual(plan["mode"], "composed")

    def test_bad_index_spec_is_usage_error(self):
        result = run_route("check", "--index", "burner")
        self.assertEqual(result.returncode, route.EXIT_ENV)


if __name__ == "__main__":
    unittest.main()
