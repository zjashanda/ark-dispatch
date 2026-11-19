#!/usr/bin/env python3
"""Skill 索引路由（ark-dispatch 的一部分）：只读索引、只出路由计划，不执行任何设备动作。

输入：burner / lstest 的 index/index.json（ark.skill-index.v1）
输出：skill-route-plan.v1 —— 按 capability → skill → chip/project → profile → workflow 解析出的路由，
      含前置检查、执行入口、证据 schema 与执行门禁。

设计口径：
- 不猜：能力匹配到多条一律判 AMBIGUOUS，要求调用方补 chip/project/profile。
- 不越权：status=gap 的能力禁止执行；draft profile 只允许只读/预检。
- 不联网、不碰设备、不写索引。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

SCHEMA_INDEX = "ark.skill-index.v1"
SCHEMA_ROUTE = "ark.skill-route.v1"
SCHEMA_PLAN = "skill-route-plan.v1"
ROUTE_FIELDS = (
    "capability_id",
    "skill",
    "module",
    "chip",
    "project",
    "firmware_family",
    "profile",
    "workflow",
    "inputs",
    "outputs",
    "preconditions",
    "evidence_schema",
    "version",
    "status",
)
STATUSES = ("active", "draft", "gap", "blocked")
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_ENV = 3
EXIT_BLOCKED = 4
EXECUTABLE_STATUSES = ("active",)
READONLY_STATUSES = ("active", "draft")
FORMAL_MODES = ("formal", "measured")


class RouteError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_INPUT) -> None:
        super().__init__(message)
        self.code = code


def now_bj() -> str:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RouteError(f"索引不存在：{path}", EXIT_ENV) from None
    except json.JSONDecodeError as exc:
        raise RouteError(f"索引不是合法 JSON：{path}: {exc}", EXIT_ENV) from None


def load_index(path: Path) -> dict:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise RouteError(f"索引顶层必须是对象：{path}", EXIT_ENV)
    problems = check_index(payload, path)
    if problems:
        raise RouteError("索引不合格：" + "；".join(problems), EXIT_ENV)
    return payload


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def check_index(payload: dict, path: Path | None = None) -> list[str]:
    """结构校验：schema、必填、引用闭合。返回问题列表，空列表为合格。"""
    label = str(path) if path else "index"
    problems: list[str] = []
    if payload.get("schema") != SCHEMA_INDEX:
        problems.append(f"{label}: schema 必须是 {SCHEMA_INDEX}")
    skill = payload.get("skill")
    if skill not in ("burner", "lstest"):
        problems.append(f"{label}: skill 必须是 burner 或 lstest")
    if payload.get("route_schema") != SCHEMA_ROUTE:
        problems.append(f"{label}: route_schema 必须是 {SCHEMA_ROUTE}")
    order = payload.get("route_order") or []
    for required in ("capability", "skill", "chip", "project", "profile", "workflow"):
        if required not in order:
            problems.append(f"{label}: route_order 缺少 {required}")

    chips = {str(item.get("chip_id")) for item in payload.get("chips") or [] if isinstance(item, dict)}
    projects = {str(item.get("project_id")) for item in payload.get("projects") or [] if isinstance(item, dict)}
    workflows = {str(item.get("workflow_id")) for item in payload.get("workflows") or [] if isinstance(item, dict)}

    seen: set[str] = set()
    for index, cap in enumerate(payload.get("capabilities") or []):
        where = f"{label}.capabilities[{index}]"
        if not isinstance(cap, dict):
            problems.append(f"{where}: 必须是对象")
            continue
        missing = [name for name in ROUTE_FIELDS if name not in cap]
        if missing:
            problems.append(f"{where}: 缺少字段 {', '.join(missing)}")
        extra = [name for name in cap if name not in ROUTE_FIELDS]
        if extra:
            problems.append(f"{where}: 未声明字段 {', '.join(extra)}（元数据只允许 14 个固定字段）")
        cap_id = str(cap.get("capability_id") or "")
        if cap_id in seen:
            problems.append(f"{where}: capability_id 重复 {cap_id}")
        seen.add(cap_id)
        if not cap_id.startswith(("burn.", "test.")):
            problems.append(f"{where}: capability_id 必须以 burn. 或 test. 开头")
        if skill and cap.get("skill") != skill:
            problems.append(f"{where}: skill {cap.get('skill')!r} 与索引 {skill!r} 不一致")
        for field_name in ("inputs", "outputs", "preconditions"):
            values = cap.get(field_name)
            if not isinstance(values, list) or not values:
                problems.append(f"{where}: {field_name} 必须是非空数组")
        if cap.get("status") not in STATUSES:
            problems.append(f"{where}: status {cap.get('status')!r} 不在 {STATUSES}")
        workflow = cap.get("workflow")
        if workflow and workflow not in workflows:
            problems.append(f"{where}: workflow {workflow} 不在 workflows[]")
        for chip in _as_list(cap.get("chip")):
            if chip not in chips and chip not in ("any", "通用"):
                problems.append(f"{where}: chip {chip} 不在 chips[]")
        for project in _as_list(cap.get("project")):
            if project not in projects and project not in ("any", "通用"):
                problems.append(f"{where}: project {project} 不在 projects[]")
        if not str(cap.get("profile") or "").strip():
            problems.append(f"{where}: profile 不能为空")
        if not str(cap.get("evidence_schema") or "").strip():
            problems.append(f"{where}: evidence_schema 不能为空")

    for index, workflow in enumerate(payload.get("workflows") or []):
        if not isinstance(workflow, dict):
            problems.append(f"{label}.workflows[{index}]: 必须是对象")
            continue
        if not str(workflow.get("workflow_id") or "").startswith("wf."):
            problems.append(f"{label}.workflows[{index}]: workflow_id 必须以 wf. 开头")
        if not workflow.get("steps"):
            problems.append(f"{label}.workflows[{index}]: steps 不能为空")

    for index, project in enumerate(payload.get("projects") or []):
        if not isinstance(project, dict) or not project.get("project_id"):
            problems.append(f"{label}.projects[{index}]: 缺少 project_id")
    for index, chip in enumerate(payload.get("chips") or []):
        if not isinstance(chip, dict) or not chip.get("chip_id"):
            problems.append(f"{label}.chips[{index}]: 缺少 chip_id")
    return problems


def _cap_matches(
    cap: dict,
    capability: str,
    chip: str | None,
    project: str | None,
    profile: str | None,
) -> bool:
    cap_id = str(cap.get("capability_id") or "")
    if not (cap_id == capability or cap_id.startswith(capability.rstrip(".") + ".")):
        return False
    if chip:
        chips = _as_list(cap.get("chip"))
        if chip not in chips and "any" not in chips and "通用" not in chips:
            return False
    if project:
        projects = _as_list(cap.get("project"))
        if project not in projects and "any" not in projects and "通用" not in projects:
            return False
    if profile:
        if profile not in str(cap.get("profile") or ""):
            return False
    return True


def _project_contract(index: dict, project: str | None) -> tuple[str | None, str | None]:
    if not project:
        return None, None
    for item in index.get("projects") or []:
        if isinstance(item, dict) and item.get("project_id") == project:
            mode = item.get("contract_mode") or item.get("profile_status")
            return (str(mode) if mode else None), str(item.get("profile_status") or "")
    return None, None


def _gate(index: dict, cap: dict, project: str | None, readonly: bool) -> tuple[str, list[str]]:
    """返回 (gate 结论, 说明)。gate 结论：PASS / BLOCKED_* 前缀。"""
    notes: list[str] = []
    status = cap.get("status")
    if status == "gap":
        return "BLOCKED_CAPABILITY_GAP", ["能力状态 gap：工具或证据缺失，禁止执行"]
    if status == "blocked":
        return "BLOCKED_CAPABILITY", ["能力状态 blocked"]
    if status == "draft":
        notes.append("能力状态 draft：只允许只读/预检")
        if not readonly:
            return "BLOCKED_DRAFT_CAPABILITY", notes
    mode, profile_status = _project_contract(index, project)
    if project and mode is None:
        return "BLOCKED_UNKNOWN_PROJECT", [f"项目 {project} 不在索引 projects[]，不得猜 profile"]
    if mode and mode.lower() not in FORMAL_MODES:
        notes.append(f"项目合同 {mode}（profile_status={profile_status}）：正式执行需 formal/measured")
        if not readonly:
            return "BLOCKED_PROFILE_CONTRACT", notes
    return "PASS", notes


def resolve(
    indexes: dict[str, dict],
    capability: str,
    chip: str | None,
    project: str | None,
    profile: str | None,
    readonly: bool,
) -> dict:
    matches: list[tuple[dict, dict]] = []
    for index in indexes.values():
        for cap in index.get("capabilities") or []:
            if isinstance(cap, dict) and _cap_matches(cap, capability, chip, project, profile):
                matches.append((index, cap))
    request = {
        "capability": capability,
        "chip": chip,
        "project": project,
        "profile": profile,
        "readonly": readonly,
    }
    if not matches:
        return {
            "schema": SCHEMA_PLAN,
            "route_status": "NO_MATCH",
            "request": request,
            "steps": [],
            "gate": "BLOCKED_CAPABILITY_NOT_FOUND",
            "notes": ["索引里没有匹配能力；补 chip/project/profile 或先登记能力，禁止按名字猜工具"],
            "generated_at": now_bj(),
        }
    if len(matches) > 1:
        return {
            "schema": SCHEMA_PLAN,
            "route_status": "AMBIGUOUS",
            "request": request,
            "steps": [],
            "gate": "BLOCKED_AMBIGUOUS_ROUTE",
            "candidates": [
                {
                    "skill": cap.get("skill"),
                    "capability_id": cap.get("capability_id"),
                    "chip": cap.get("chip"),
                    "project": cap.get("project"),
                    "profile": cap.get("profile"),
                }
                for _, cap in matches
            ],
            "notes": ["命中多条能力；按 route_order 补 chip→project→profile 收窄，禁止自行挑选"],
            "generated_at": now_bj(),
        }

    index, cap = matches[0]
    gate, gate_notes = _gate(index, cap, project, readonly)
    workflow_id = cap.get("workflow")
    workflow = next(
        (item for item in index.get("workflows") or [] if item.get("workflow_id") == workflow_id),
        {},
    )
    step = {
        "step": 1,
        "skill": cap.get("skill"),
        "skill_version": index.get("skill_version"),
        "repo": index.get("repo"),
        "capability_id": cap.get("capability_id"),
        "module": cap.get("module"),
        "chip": cap.get("chip"),
        "project": cap.get("project"),
        "firmware_family": cap.get("firmware_family"),
        "profile": cap.get("profile"),
        "workflow": workflow_id,
        "workflow_steps": workflow.get("steps"),
        "entry": workflow.get("entry"),
        "inputs": cap.get("inputs"),
        "outputs": cap.get("outputs"),
        "preconditions": cap.get("preconditions"),
        "evidence_schema": cap.get("evidence_schema"),
        "capability_status": cap.get("status"),
        "version": cap.get("version"),
    }
    return {
        "schema": SCHEMA_PLAN,
        "route_status": "MATCHED",
        "request": request,
        "gate": gate,
        "steps": [step],
        "notes": gate_notes,
        "sources": {
            "index": f"{index.get('repo')}@{index.get('skill_version')}",
            "capability": cap.get("capability_id"),
            "workflow": workflow_id,
        },
        "generated_at": now_bj(),
    }


def compose(plans: list[dict]) -> dict:
    """把多条路由按给定顺序拼成一条链路，显式标注证据交接。"""
    if not plans:
        raise RouteError("compose 至少需要一条路由", EXIT_INPUT)
    failed = [plan for plan in plans if plan.get("route_status") != "MATCHED"]
    steps: list[dict] = []
    handoffs: list[dict] = []
    counter = 1
    previous: dict | None = None
    for plan in plans:
        for step in plan.get("steps") or []:
            item = dict(step)
            item["step"] = counter
            counter += 1
            if previous and previous.get("skill") != item.get("skill"):
                handoffs.append(
                    {
                        "from_skill": previous.get("skill"),
                        "from_capability": previous.get("capability_id"),
                        "to_skill": item.get("skill"),
                        "to_capability": item.get("capability_id"),
                        "evidence": previous.get("outputs"),
                        "evidence_schema": previous.get("evidence_schema"),
                        "note": "前一步产物作为后一步输入证据；能力不复制，回执不重写",
                    }
                )
            steps.append(item)
            previous = item
    skills = [step.get("skill") for step in steps]
    cap_ids = [step.get("capability_id") for step in steps]
    gates = [plan.get("gate") for plan in plans]
    blocking = [value for value in gates if value != "PASS"]
    gate = "PASS" if not blocking and not failed else (blocking[0] if len(set(blocking)) == 1 else "BLOCKED")
    notes = [note for plan in plans for note in (plan.get("notes") or [])]
    return {
        "schema": SCHEMA_PLAN,
        "route_status": "MATCHED" if not failed else "PARTIAL",
        "mode": "composed",
        "gate": gate,
        "blocked_steps": [
            {"capability": plan.get("request", {}).get("capability"), "gate": plan.get("gate")}
            for plan in plans
            if plan.get("gate") != "PASS"
        ],
        "steps": steps,
        "handoffs": handoffs,
        "distinct_skills": sorted(set(skills)),
        "duplicate_capabilities": sorted({cap for cap in cap_ids if cap_ids.count(cap) > 1}),
        "notes": notes,
        "sub_plans": [
            {"capability": plan.get("request", {}).get("capability"), "gate": plan.get("gate")} for plan in plans
        ],
        "generated_at": now_bj(),
    }


def command_check(args) -> int:
    problems: list[str] = []
    for spec in args.index:
        name, _, raw = spec.partition("=")
        path = Path(raw).expanduser() if raw else Path(name).expanduser()
        payload = read_json(path)
        problems.extend(check_index(payload, path))
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return EXIT_INPUT
    print(f"OK {len(args.index)} 个索引通过结构与引用校验")
    return EXIT_OK


def parse_capability_spec(spec: str, defaults: dict[str, str | None]) -> tuple[str, dict[str, str | None]]:
    """`cap.id` 或 `cap.id#chip=csk7,project=venusws63`：每一步可覆盖全局过滤。"""
    capability, _, raw_filters = spec.partition("#")
    filters = dict(defaults)
    if raw_filters:
        for pair in raw_filters.split(","):
            name, _, value = pair.partition("=")
            name = name.strip()
            if name not in ("chip", "project", "profile"):
                raise RouteError(f"不支持的能力过滤项 {name!r}（只允许 chip/project/profile）", EXIT_INPUT)
            if not value.strip():
                raise RouteError(f"能力过滤项 {name} 不能为空", EXIT_INPUT)
            filters[name] = value.strip()
    return capability.strip(), filters


def command_resolve(args) -> int:
    indexes: dict[str, dict] = {}
    for spec in args.index:
        name, _, raw = spec.partition("=")
        if not raw:
            raise RouteError("--index 必须是 name=path 形式", EXIT_INPUT)
        indexes[name] = load_index(Path(raw).expanduser())
    defaults = {"chip": args.chip, "project": args.project, "profile": args.profile}
    plans = []
    for spec in args.capability:
        capability, filters = parse_capability_spec(spec, defaults)
        plan = resolve(
            indexes,
            capability=capability,
            chip=filters.get("chip"),
            project=filters.get("project"),
            profile=filters.get("profile"),
            readonly=args.readonly,
        )
        if len(args.capability) > 1:
            plan["request"]["filters_from"] = "per-capability" if "#" in spec else "global"
        plans.append(plan)
    plan = plans[0] if len(plans) == 1 else compose(plans)
    text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    if plan.get("route_status") != "MATCHED":
        return EXIT_INPUT
    return EXIT_OK if plan.get("gate") == "PASS" else EXIT_BLOCKED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="burner/lstest Skill 索引路由（只读，不执行）")
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="校验索引结构与引用闭合")
    check_parser.add_argument("--index", action="append", required=True, metavar="NAME=PATH")
    check_parser.set_defaults(func=command_check)

    resolve_parser = sub.add_parser("resolve", help="按 capability→chip→project→profile 解析路由")
    resolve_parser.add_argument("--capability", action="append", required=True, metavar="CAP_ID")
    resolve_parser.add_argument("--chip")
    resolve_parser.add_argument("--project")
    resolve_parser.add_argument("--profile")
    resolve_parser.add_argument("--readonly", action="store_true", help="只读/预检任务")
    resolve_parser.add_argument("--index", action="append", required=True, metavar="NAME=PATH")
    resolve_parser.add_argument("--out")
    resolve_parser.set_defaults(func=command_resolve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RouteError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
