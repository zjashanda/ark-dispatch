#!/usr/bin/env python3
"""方舟调度（ark-dispatch）：只做决策，不执行任何设备动作。

输入：任务 + 设备 + 台架条件（dispatch.request.v1）
读取：本机快照中的能力台账 registry/skills.json 与知识库 entries/*.jsonl
输出：plan.v1 —— 选哪些 skill、按什么顺序、每一步的入口/门禁/失败退出点/回滚方式。

设计口径：
- 选型是确定性的：关键词 → 能力角色 → 台账门禁 → 角色竞标 → 依赖闭包 → 拓扑+阶段排序。
- 不联网、不碰设备、不写台账；知识库只读，不回写命中计数。
- 台账里查不到的 skill 一律不选；archived 一律不选。
- 没有把握就给 confidence: low + needs_confirmation，不硬选。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import heapq
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_REQUEST = "dispatch.request.v1"
SCHEMA_PLAN = "plan.v1"
CONFIDENCES = ("high", "medium", "low")
ON_FAIL_VALUES = ("stop", "retry", "skip")
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_ENV = 3
EXIT_PLAN = 4

UNKNOWN_VALUES = {"", "未知", "unknown", "n/a", "na", "none", "null", "待定", "未提供"}
PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")
PLACEHOLDER_LABELS = {
    "ports": "端口列表（bench.ports）",
    "port": "端口",
    "playback_key": "播报声卡 Key（bench.playback_key）",
    "log_port": "日志口（bench.ports 或 bench.port_roles.log）",
    "control_port": "控制口（bench.ports 或 bench.port_roles.control）",
    "flash_port": "烧录口（bench.ports 或 bench.port_roles.flash）",
    "minutes": "长跑时长（constraints.hours）",
    "text_file": "待合成文本文件（bench.text_file）",
    "profile": "lstest profile 路径",
    "model": "设备型号（device.model）",
    "project": "项目名（device.project）",
    "firmware": "固件路径（device.firmware）",
    "effective_ports": "端口与来源（bench.ports 或 skill 默认值）",
}
LAYER_RANK = {
    "spec": 0,
    "kernel": 1,
    "tool": 2,
    "capability": 3,
    "knowledge": 4,
    "orchestration": 5,
}
REUSE_WEIGHT = {"low": 6, "medium": 3, "high": 0}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


class DispatchError(RuntimeError):
    """可预期失败：由 main() 转成退出码，不打堆栈。"""

    def __init__(self, message: str, code: int = EXIT_INPUT) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def norm(value) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).lower()
    return re.sub(r"[\s，。、；：！？,.;:!?/\\|()（）\[\]【】<>《》\"'`~_\-+*=]+", "", text)


def clean_text(value) -> str:
    return unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()


def snippet(text: str, limit: int = 90) -> str:
    text = clean_text(text).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DispatchError(f"读不到 {path}: {exc}", EXIT_ENV) from exc
    except json.JSONDecodeError as exc:
        raise DispatchError(f"{path} 不是合法 JSON: {exc}", EXIT_ENV) from exc


# --------------------------------------------------------------------------- #
# 输入加载
# --------------------------------------------------------------------------- #

def resolve_registry_path(raw: str | None) -> Path:
    if raw:
        explicit = Path(raw).expanduser()
        if explicit.is_file():
            return explicit
        raise DispatchError(f"--registry 指定的台账不存在: {explicit}", EXIT_ENV)
    candidates = []
    if os.environ.get("DISPATCH_REGISTRY"):
        candidates.append(Path(os.environ["DISPATCH_REGISTRY"]).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "ark-kb" / "registry" / "skills.json")
    candidates.append(Path.home() / ".codex" / "skills" / "ark-kb" / "registry" / "skills.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DispatchError(
        "找不到能力台账 registry/skills.json；用 --registry 指定 ark-kb 检出的台账路径（离线快照）",
        EXIT_ENV,
    )


def resolve_kb_path(raw: str | None) -> Path | None:
    if raw:
        explicit = Path(raw).expanduser()
        if explicit.is_dir() and (explicit / "entries").is_dir():
            return explicit
        if explicit.is_file():
            return explicit
        return None
    candidates = []
    if os.environ.get("DISPATCH_KB"):
        candidates.append(Path(os.environ["DISPATCH_KB"]).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "ark-kb")
    candidates.append(Path.home() / ".codex" / "skills" / "ark-kb")
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "entries").is_dir():
            return candidate
        if candidate.is_file():
            return candidate
    return None


def load_registry(path: Path) -> list[dict]:
    data = read_json(path)
    if not isinstance(data, list):
        raise DispatchError(f"{path} 顶层必须是数组", EXIT_ENV)
    required = ("name", "layer", "status", "capability", "platform", "projects", "reuse", "entry")
    problems = []
    for index, skill in enumerate(data):
        if not isinstance(skill, dict):
            problems.append(f"第 {index + 1} 条不是对象")
            continue
        missing = [key for key in required if key not in skill]
        if missing:
            problems.append(f"{skill.get('name') or index}: 缺字段 {missing}")
    if problems:
        raise DispatchError("台账不合格：" + "；".join(problems), EXIT_ENV)
    return data


def load_kb(path: Path | None) -> list[dict]:
    if path is None:
        return []
    if path.is_file():
        files = [path]
    elif (path / "entries").is_dir():
        files = sorted((path / "entries").rglob("*.jsonl"))
    else:
        files = sorted(path.rglob("entries.jsonl"))
    if not files:
        raise DispatchError(f"{path} 下没找到 entries/*.jsonl", EXIT_ENV)
    entries: list[dict] = []
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DispatchError(f"读不到知识库文件 {file}: {exc}", EXIT_ENV) from exc
        for line_no, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DispatchError(f"{file}:{line_no} 不是合法 JSON: {exc}", EXIT_ENV) from exc
            if not isinstance(entry, dict) or not entry.get("id"):
                raise DispatchError(f"{file}:{line_no} 缺 id", EXIT_ENV)
            if entry.get("status") in ("stale", "superseded"):
                continue
            entries.append(entry)
    return entries


def load_rules(path: Path) -> dict:
    rules = read_json(path)
    if not isinstance(rules, dict):
        raise DispatchError(f"{path} 顶层必须是对象", EXIT_ENV)
    return rules


# --------------------------------------------------------------------------- #
# 请求解析
# --------------------------------------------------------------------------- #

@dataclass
class Intent:
    task: str
    model: str | None
    project: str | None
    firmware: str | None
    os_name: str | None
    node: str | None
    ports: list[str]
    explicit_port_roles: dict[str, str]
    playback_key: str | None
    text_file: str | None
    hours: float | None
    overnight: bool
    reset_allowed: bool
    signals: set[str] = field(default_factory=set)
    roles: list[str] = field(default_factory=list)
    stress: bool = False
    long_run: bool = False
    needs_confirmation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.task


def normalize_platform(value) -> str | None:
    token = norm(value)
    aliases = {
        "windows": "windows",
        "win": "windows",
        "win10": "windows",
        "win11": "windows",
        "linux": "linux",
        "ubuntu": "linux",
        "darwin": "macos",
        "macos": "macos",
        "mac": "macos",
    }
    return aliases.get(token, token or None)


def extract_signals(text: str, rules: dict) -> set[str]:
    haystack = norm(text)
    found = {
        signal
        for signal, keywords in rules.get("task_signals", {}).items()
        if any(norm(keyword) in haystack for keyword in keywords)
    }
    changed = True
    while changed:
        changed = False
        for signal in list(found):
            for implied in rules.get("signal_implies", {}).get(signal, []):
                if implied not in found:
                    found.add(implied)
                    changed = True
    return found


def parse_request(data: dict, rules: dict) -> Intent:
    if not isinstance(data, dict):
        raise DispatchError("请求必须是 JSON 对象", EXIT_INPUT)
    task = clean_text(data.get("task"))
    if not task:
        raise DispatchError("请求缺 task：至少给一句要完成的事", EXIT_INPUT)
    device = data.get("device") or {}
    bench = data.get("bench") or {}
    constraints = data.get("constraints") or {}
    if not isinstance(device, dict) or not isinstance(bench, dict) or not isinstance(constraints, dict):
        raise DispatchError("device / bench / constraints 必须是对象", EXIT_INPUT)

    notes: list[str] = []
    needs: list[str] = []

    model_raw = clean_text(device.get("model"))
    model = None if norm(model_raw) in UNKNOWN_VALUES else model_raw
    if model is None:
        needs.append("设备型号（device.model）：台账无法判断设备专用 skill 是否适用，本轮不猜")
    project_raw = clean_text(device.get("project"))
    project = None if norm(project_raw) in UNKNOWN_VALUES else project_raw
    if project is None:
        notes.append("未提供项目名（device.project），项目专用 skill 只能按型号匹配")

    os_name = normalize_platform(bench.get("os"))
    if os_name is None:
        needs.append("台架操作系统（bench.os）：无法做 platform 门禁，默认按 windows 平台模板生成")

    ports_raw = bench.get("ports") or []
    if isinstance(ports_raw, str):
        ports_raw = [part.strip() for part in ports_raw.split(",")]
    if not isinstance(ports_raw, list):
        raise DispatchError("bench.ports 必须是数组", EXIT_INPUT)
    ports = [clean_text(item) for item in ports_raw if clean_text(item)]

    explicit_port_roles: dict[str, str] = {}
    raw_roles = bench.get("port_roles") or {}
    if isinstance(raw_roles, dict):
        explicit_port_roles = {norm(key): clean_text(value) for key, value in raw_roles.items() if clean_text(value)}

    hours = constraints.get("hours")
    if hours is not None:
        try:
            hours = float(hours)
        except (TypeError, ValueError) as exc:
            raise DispatchError("constraints.hours 必须是数字", EXIT_INPUT) from exc
        if hours <= 0:
            raise DispatchError("constraints.hours 必须大于 0", EXIT_INPUT)

    overnight = bool(constraints.get("overnight"))
    reset_allowed = bool(constraints.get("reset_allowed"))
    stress_from_text = any(norm(keyword) in norm(task) for keyword in rules.get("stress_keywords", []))
    stress = bool(overnight or stress_from_text or (hours is not None and hours >= 6))
    long_run = bool(stress and hours is not None)
    if stress and hours is None:
        needs.append("长跑时长（constraints.hours）：未提供时长，本轮只排 smoke + 短跑，不排通宵长跑")

    signals = extract_signals(task, rules)
    roles = [role for role in rules.get("phase_order", []) if role in signals]
    for role in sorted(signals):
        if role not in roles:
            roles.append(role)

    return Intent(
        task=task,
        model=model,
        project=project,
        firmware=clean_text(device.get("firmware")) or None,
        os_name=os_name,
        node=clean_text(bench.get("node")) or None,
        ports=ports,
        explicit_port_roles=explicit_port_roles,
        playback_key=clean_text(bench.get("playback_key")) or None,
        text_file=clean_text(bench.get("text_file")) or None,
        hours=hours,
        overnight=overnight,
        reset_allowed=reset_allowed,
        signals=signals,
        roles=roles,
        stress=stress,
        long_run=long_run,
        needs_confirmation=needs,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 台账门禁与竞标
# --------------------------------------------------------------------------- #

def skill_text(skill: dict) -> str:
    return norm((skill.get("name") or "") + " " + (skill.get("capability") or ""))


def matched_role_keywords(skill: dict, role: str, rules: dict) -> list[str]:
    text = skill_text(skill)
    return [keyword for keyword in rules["roles"][role]["keywords"] if norm(keyword) in text]


def platform_ok(skill: dict, os_name: str | None) -> bool:
    if not os_name:
        return True
    platforms = [normalize_platform(item) for item in skill.get("platform") or []]
    return os_name in platforms


def project_tokens(value) -> set[str]:
    text = unicodedata.normalize("NFKC", clean_text(value)).lower()
    return {token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", text) if token}


def project_match(skill: dict, project: str | None) -> bool:
    """项目名按分隔符切词比较，避免 'tcl' 命中 'httclothes' 这类子串误判。"""
    if not project:
        return False
    needle_tokens = project_tokens(project)
    if not needle_tokens:
        return False
    for item in skill.get("projects") or []:
        candidate_tokens = project_tokens(item)
        if not candidate_tokens:
            continue
        if norm(item) == norm(project):
            return True
        if needle_tokens <= candidate_tokens or candidate_tokens <= needle_tokens:
            return True
    return False


def model_match(skill: dict, model: str | None) -> bool:
    if not model:
        return False
    haystack = norm(
        " ".join(
            [
                skill.get("name") or "",
                skill.get("capability") or "",
                skill.get("scope_note") or "",
                " ".join(skill.get("projects") or []),
            ]
        )
    )
    return norm(model) in haystack


@dataclass
class Assessment:
    skill: dict
    roles: list[str]
    reason: str | None = None
    code: str | None = None


def assess(skill: dict, intent: Intent, rules: dict) -> Assessment:
    roles = [role for role in rules["roles"] if matched_role_keywords(skill, role, rules)]
    name = skill["name"]
    status = skill.get("status")
    projects = skill.get("projects") or []

    if status == "archived":
        replaced = skill.get("replaced_by")
        detail = f"；已被 {replaced} 取代" if replaced else ""
        return Assessment(
            skill,
            roles,
            f"status=archived（registry:{name}.status）{detail}：{snippet(skill.get('scope_note'))}",
            "archived",
        )

    if not platform_ok(skill, intent.os_name):
        return Assessment(
            skill,
            roles,
            f"platform={skill.get('platform')} 不含 {intent.os_name}（registry:{name}.platform）",
            "platform",
        )

    generic = any(norm(item) == "通用" for item in projects)
    pm = project_match(skill, intent.project)
    mm = model_match(skill, intent.model)
    if not generic and not pm and not mm:
        reason = (
            f"projects={projects} 与任务项目 {intent.project or '（未提供）'} 不匹配，"
            f"型号 {intent.model or '（未提供）'} 也未命中（registry:{name}.projects/.scope_note）"
        )
        if skill.get("reuse") == "low":
            reason += f"；reuse=low：{snippet(skill.get('scope_note'), 70)}"
        return Assessment(skill, roles, reason, "project")
    if skill.get("reuse") == "low" and not (pm or mm):
        return Assessment(
            skill,
            roles,
            f"reuse=low（设备专用）且设备型号/项目未命中：{snippet(skill.get('scope_note'))}（registry:{name}.reuse）",
            "device",
        )

    overlap = [role for role in roles if role in intent.roles]
    if not overlap:
        if not roles:
            detail = f"capability 未命中本轮任何能力角色（能力：{snippet(skill.get('capability'), 60)}；registry:{name}.capability）"
        else:
            detail = f"任务未出现它覆盖的能力信号（能力：{snippet(skill.get('capability'), 60)}；registry:{name}.capability）"
        return Assessment(skill, roles, detail, "not_required")
    return Assessment(skill, roles)


def role_phase(role: str, rules: dict, skill: dict | None = None) -> str:
    phase = rules.get("roles", {}).get(role, {}).get("phase")
    if phase:
        return phase
    if skill is not None:
        layer = skill.get("layer")
        return {
            "kernel": "action",
            "capability": "execute",
            "tool": "evidence",
            "knowledge": "evidence",
            "orchestration": "evidence",
        }.get(layer, "execute")
    return "execute"


@dataclass
class Pick:
    skill: dict
    role: str
    score: int
    basis: list[str] = field(default_factory=list)
    dependency: bool = False


def kb_preferences(entries: list[dict], names: list[str]) -> dict[str, str]:
    """从知识条目里抽 '统一走 <skill>' 这类规范，返回 skill -> entry_id。"""
    prefs: dict[str, str] = {}
    for entry in entries:
        text = f"{entry.get('title') or ''} {entry.get('claim') or ''}"
        for name in names:
            if name and re.search(r"统一.{0,6}" + re.escape(name), text, re.IGNORECASE):
                prefs.setdefault(name, entry["id"])
    return prefs


def score_candidate(
    skill: dict,
    role: str,
    keywords: list[str],
    intent: Intent,
    prefs: dict[str, str],
) -> tuple[int, list[str]]:
    score = 100
    basis: list[str] = []
    name = skill["name"]
    if model_match(skill, intent.model):
        score += 40
        basis.append(f"registry:{name} 命中设备型号 {intent.model}")
    if project_match(skill, intent.project):
        score += 30
        basis.append(f"registry:{name}.projects={skill.get('projects')}")
    reuse = skill.get("reuse")
    score += REUSE_WEIGHT.get(reuse, 0)
    if reuse == "low":
        basis.append(f"registry:{name}.reuse=low（设备专用，优先于通用实现）")
    if skill.get("status") == "legacy":
        score -= 4
        basis.append(f"registry:{name}.status=legacy（仍可用但需确认）")
    score += 2 * len(keywords)
    basis.append(f"registry:{name}.capability 命中「{'/'.join(keywords)}」")
    if name in prefs:
        score += 12
        basis.append(f"kb:{prefs[name]} 规定统一走 {name}")
    return score, basis


def select_skills(
    registry: list[dict],
    intent: Intent,
    rules: dict,
    kb_entries: list[dict],
) -> tuple[dict[str, Pick], list[dict], list[str], list[str], list[str], list[str]]:
    """返回 (selected, excluded, unfilled_roles, standards, runtime_notes, warnings)。"""
    names = [skill["name"] for skill in registry]
    prefs = kb_preferences(kb_entries, names)
    assessments = [assess(skill, intent, rules) for skill in registry]
    candidates: dict[str, list[Pick]] = {role: [] for role in intent.roles}
    blocked: list[Assessment] = []

    for assessment in assessments:
        if assessment.reason:
            blocked.append(assessment)
            continue
        for role in assessment.roles:
            if role not in candidates:
                continue
            keywords = matched_role_keywords(assessment.skill, role, rules)
            score, basis = score_candidate(assessment.skill, role, keywords, intent, prefs)
            candidates[role].append(Pick(assessment.skill, role, score, basis))

    selected: dict[str, Pick] = {}
    losers: dict[str, dict] = {}
    unfilled: list[str] = []
    for role in intent.roles:
        ranked = sorted(candidates.get(role, []), key=lambda pick: (-pick.score, pick.skill["name"]))
        if not ranked:
            unfilled.append(role)
            continue
        chosen = next((pick for pick in ranked if pick.skill["name"] not in selected), None)
        if chosen is None:
            # 该角色由已选中的 skill 覆盖，不重复出步骤。
            continue
        selected[chosen.skill["name"]] = chosen
        for pick in ranked:
            if pick.skill["name"] == chosen.skill["name"] or pick.skill["name"] in selected:
                continue
            losers.setdefault(
                pick.skill["name"],
                {"role": role, "winner": chosen.skill["name"], "winner_basis": chosen.basis},
            )

    excluded: list[dict] = []
    excluded_names: set[str] = set()
    superseded: dict[str, str] = {}
    for other, target in rules.get("supersedes", {}).items():
        if target in selected and other not in selected:
            superseded[other] = target

    for assessment in blocked:
        name = assessment.skill["name"]
        if name in superseded:
            target = superseded[name]
            excluded.append(
                {
                    "skill": name,
                    "reason": f"能力已被 {target} 覆盖：{snippet(assessment.skill.get('scope_note'))}（registry:{name}.scope_note）",
                }
            )
            excluded_names.add(name)
            superseded.pop(name, None)
            continue
        if assessment.code == "not_required" and assessment.skill.get("reuse") != "low":
            continue
        if assessment.code in ("archived", "platform", "project", "device") or assessment.skill.get("reuse") == "low":
            excluded.append({"skill": name, "reason": assessment.reason})
            excluded_names.add(name)
    for name, target in superseded.items():
        if name in excluded_names:
            continue
        skill = next((item for item in registry if item["name"] == name), None)
        excluded.append(
            {
                "skill": name,
                "reason": f"能力已被 {target} 覆盖：{snippet((skill or {}).get('scope_note'))}（registry:{name}.scope_note）",
            }
        )
        excluded_names.add(name)
    for name, info in losers.items():
        if name in selected or name in excluded_names:
            continue
        skill = next((item for item in registry if item["name"] == name), None)
        if skill is None:
            continue
        role_label = rules["roles"][info["role"]]["label"]
        reason = (
            f"同一能力「{role_label}」优先选 {info['winner']}：{'；'.join(info['winner_basis'][:3])}。"
            f"本 skill 限制：{snippet(skill.get('scope_note'), 70)}（registry:{name}.scope_note）"
        )
        kb_hit = prefs.get(info["winner"])
        if kb_hit:
            reason += f"；kb:{kb_hit} 规定统一走 {info['winner']}"
        excluded.append({"skill": name, "reason": reason})
        excluded_names.add(name)

    # 依赖闭包：只把登记了调用模板、且非 spec 的依赖加为步骤；spec 依赖进 standards。
    standards: list[str] = []
    runtime_notes: list[str] = []
    warnings: list[str] = []
    by_name = {skill["name"]: skill for skill in registry}
    queue = list(selected)
    visited: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in visited or name not in by_name:
            continue
        visited.add(name)
        depends = by_name[name].get("depends_skills") or []
        for dep in depends:
            if norm(dep) in ("", "无"):
                continue
            dependency = by_name.get(dep)
            if dependency is None:
                warnings.append(f"依赖 {dep} 不在台账（registry:{name}.depends_skills），未加入计划，执行前需确认入口")
                continue
            if dependency.get("status") == "archived":
                warnings.append(f"依赖 {dep} 已归档（registry:{dep}.status），未加入计划")
                continue
            if not platform_ok(dependency, intent.os_name):
                warnings.append(f"依赖 {dep} 的平台 {dependency.get('platform')} 不含 {intent.os_name}")
                continue
            if dep in selected:
                continue
            if rules.get("dependency_steps", {}).get(dep):
                roles = [role for role in rules["roles"] if matched_role_keywords(dependency, role, rules)]
                role = next((item for item in intent.roles if item in roles), roles[0] if roles else "unknown")
                selected[dep] = Pick(
                    dependency,
                    role,
                    score=0,
                    basis=[f"由 {name} 依赖（registry:{name}.depends_skills）"],
                    dependency=True,
                )
                queue.append(dep)
                continue
            if dependency.get("layer") == "spec":
                note = f"{dep}：{snippet(dependency.get('scope_note'), 110)}"
                if note not in standards:
                    standards.append(note)
                continue
            runtime_notes.append(
                f"{dep}：由 {name} 依赖，作为运行时/共享包使用，不单独执行（registry:{name}.depends_skills）"
            )
    return selected, excluded, unfilled, standards, runtime_notes, warnings


# --------------------------------------------------------------------------- #
# 排序与步骤
# --------------------------------------------------------------------------- #

def order_selected(selected: dict[str, Pick], rules: dict) -> list[str]:
    phase_rank = {phase: index for index, phase in enumerate(rules.get("phase_order", []))}
    by_name = {name: pick.skill for name, pick in selected.items()}
    deps = {
        name: [dep for dep in (by_name[name].get("depends_skills") or []) if dep in selected]
        for name in selected
    }

    def sort_key(name: str):
        pick = selected[name]
        phase = role_phase(pick.role, rules, pick.skill)
        return (
            phase_rank.get(phase, 99),
            LAYER_RANK.get(pick.skill.get("layer"), 99),
            name,
        )

    indegree = {name: len(set(dependencies)) for name, dependencies in deps.items()}
    dependents: dict[str, list[str]] = {name: [] for name in selected}
    for name, dependencies in deps.items():
        for dependency in set(dependencies):
            dependents[dependency].append(name)
    ready = [(sort_key(name), name) for name in selected if indegree[name] == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        _, name = heapq.heappop(ready)
        ordered.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (sort_key(dependent), dependent))
    leftovers = [name for name in selected if name not in ordered]
    ordered.extend(sorted(leftovers, key=sort_key))
    return ordered


def resolve_port_roles(intent: Intent, skill_name: str, rules: dict) -> dict[str, str]:
    mapping: dict[str, str] = {}
    wanted = rules.get("port_roles", {}).get(skill_name, [])
    for index, role in enumerate(wanted):
        explicit = intent.explicit_port_roles.get(norm(role))
        if explicit:
            mapping[role] = explicit
        elif index < len(intent.ports):
            mapping[role] = intent.ports[index]
    return mapping


def effective_ports(intent: Intent, skill_name: str, rules: dict) -> tuple[str | None, str]:
    """gate/前置检查里写的端口：请求优先，skill 默认值兜底，都没有则来源未知。

    返回 (端口文本, 来源)，来源为 bench / default / unknown。
    请求给了端口就绝不再混入 skill 默认值，避免执行方去开不存在的口。
    """
    if intent.ports:
        return "/".join(intent.ports), "bench"
    defaults = rules.get("port_defaults", {}).get(skill_name) or {}
    ordered: list[str] = []
    for role in rules.get("port_roles", {}).get(skill_name, []):
        port = clean_text(defaults.get(role))
        if port and port not in ordered:
            ordered.append(port)
    for port in defaults.values():
        port = clean_text(port)
        if port and port not in ordered:
            ordered.append(port)
    if ordered:
        return "/".join(ordered), "default"
    return None, "unknown"


def port_context(intent: Intent, skill_name: str, rules: dict) -> dict[str, str]:
    text, source = effective_ports(intent, skill_name, rules)
    if source == "bench":
        rendered = f"{text}（来源：bench.ports）"
    elif source == "default":
        rendered = f"{text}（来源：default，{skill_name} 的 skill 默认值）"
    else:
        rendered = "来源未知（请求未给 bench.ports，且未登记该 skill 的默认端口）"
    return {"effective_ports": rendered, "port_source": source}


def build_context(
    intent: Intent,
    skill_name: str,
    plan_id: str,
    skills_home: str | None,
    rules: dict,
) -> dict[str, str]:
    home = skills_home or os.environ.get("DISPATCH_SKILLS_HOME") or "$HOME/.codex/skills"
    minutes = int(intent.hours * 60) if intent.hours else 30
    ports_text = ", ".join(intent.ports) if intent.ports else None
    port_map = resolve_port_roles(intent, skill_name, rules)
    context = {
        "skills_home": home,
        "skill_dir": f"{home}/{skill_name}",
        "plan_id": plan_id,
        "result_root": f"./result/{plan_id}",
        "ports": ports_text,
        "playback_key": intent.playback_key,
        "log_port": port_map.get("log"),
        "control_port": port_map.get("control"),
        "flash_port": port_map.get("flash"),
        "minutes": str(minutes),
        "short_rounds": "20",
        "burn_rounds": "20" if intent.stress else "1",
        "text_file": intent.text_file,
        "profile": None,
        "model": intent.model,
        "project": intent.project,
        "firmware": intent.firmware,
        "node": intent.node,
    }
    context.update(port_context(intent, skill_name, rules))
    return context


def fill(text: str, context: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        if value:
            return str(value)
        missing.append(key)
        return f"<未提供:{key}>"

    return PLACEHOLDER_RE.sub(replace, text), missing


def fallback_steps(skill: dict, role: str, rules: dict) -> list[dict]:
    entry = (skill.get("entry") or "").strip()
    if entry.endswith(".py"):
        command = 'python "{skill_dir}/' + entry + '"'
    elif entry.endswith(".ps1"):
        command = 'powershell.exe -NoProfile -File "{skill_dir}/' + entry + '"'
    elif entry.endswith(".bat") or entry.endswith(".cmd"):
        command = '& "{skill_dir}/' + entry + '"'
    else:
        command = '& "{skill_dir}/' + entry + '"'
    return [
        {
            "phase": role_phase(role, rules, skill),
            "label": f"按台账入口执行 {skill['name']}",
            "cmd": command,
            "precheck": ["确认该 skill 的 SKILL.md 调用参数；台账只登记入口，未登记调用模板"],
            "gate": "按该 skill 自身的前检与判定执行；没有证据不得判 PASS",
            "on_fail": "stop",
            "rollback": "按该 skill SKILL.md 的失败处理执行；不删除证据",
            "evidence": "该 skill 约定的结果目录",
            "cmd_verified": False,
        }
    ]


def build_steps(
    ordered: list[str],
    selected: dict[str, Pick],
    intent: Intent,
    rules: dict,
    plan_id: str,
    skills_home: str | None,
) -> tuple[list[dict], list[str], list[str], list[str]]:
    steps: list[dict] = []
    unresolved: list[str] = []
    warnings: list[str] = []
    port_needs: list[str] = []
    for name in ordered:
        pick = selected[name]
        templates = rules.get("steps", {}).get(name)
        if not templates:
            templates = fallback_steps(pick.skill, pick.role, rules)
            warnings.append(
                f"{name} 没有登记调用模板：台账只给入口 {pick.skill.get('entry')}，步骤标记 cmd_verified=false"
            )
        context = build_context(intent, name, plan_id, skills_home, rules)
        port_text, port_source = effective_ports(intent, name, rules)
        uses_effective_ports = any("{effective_ports}" in json.dumps(t, ensure_ascii=False) for t in templates)
        if uses_effective_ports and port_source == "default":
            warnings.append(
                f"{name} 的 gate 用了 skill 默认端口（来源：default，{port_text}）："
                "请求未提供 bench.ports，执行前必须按台架实际端口核对，不得直接照抄"
            )
            port_needs.append(
                f"台架实际端口：请求未提供 bench.ports，{name} 的 gate 回落到 skill 默认端口 {port_text}（来源：default），需核对后才能执行"
            )
        if uses_effective_ports and port_source == "unknown":
            port_needs.append(
                f"台架实际端口：请求未提供 bench.ports，台账也没有登记 {name} 的默认端口，gate 里的端口来源未知，需补齐后重新生成计划"
            )
        for template in templates:
            when = template.get("when")
            if when == "long_run" and not intent.long_run:
                continue
            if when == "short_run" and intent.long_run:
                continue
            command, missing_cmd = fill(template["cmd"], context)
            gate, missing_gate = fill(template.get("gate", ""), context)
            prechecks: list[str] = []
            for item in template.get("precheck", []):
                text, missing = fill(item, context)
                prechecks.append(text)
                for key in missing:
                    if key not in unresolved:
                        unresolved.append(key)
            rollback, missing_rollback = fill(template.get("rollback", ""), context)
            evidence, missing_evidence = fill(template.get("evidence", ""), context)
            missing = set(missing_cmd) | set(missing_gate) | set(missing_rollback) | set(missing_evidence)
            for key in missing:
                if key not in unresolved:
                    unresolved.append(key)
            steps.append(
                {
                    "n": len(steps) + 1,
                    "skill": name,
                    "phase": template["phase"],
                    "label": template["label"],
                    "cmd": command,
                    "precheck": prechecks,
                    "gate": gate,
                    "on_fail": template.get("on_fail", "stop"),
                    "rollback": rollback,
                    "evidence": evidence,
                    "cmd_verified": bool(template.get("cmd_verified", True)),
                    "basis": list(pick.basis)
                    + [f"routing:steps.{name}"]
                    + ([f"registry:{name}.entry={pick.skill.get('entry')}"] if pick.dependency else []),
                }
            )
    for index, step in enumerate(steps, 1):
        step["n"] = index
    return steps, unresolved, warnings, port_needs


# --------------------------------------------------------------------------- #
# 知识库消费
# --------------------------------------------------------------------------- #

def kb_relevance(entry: dict, intent: Intent, rules: dict) -> int:
    score = 0
    scope = entry.get("scope") or {}
    device = norm(scope.get("device") or "")
    project = norm(scope.get("project") or "")
    if device:
        if device in {norm(intent.node or ""), norm(intent.model or "")}:
            score += 5
        elif device in norm(intent.task):
            score += 4
        elif norm(intent.model or "") and device in norm(intent.model):
            score += 3
    if project and project != "通用" and norm(intent.project or ""):
        needle = norm(intent.project)
        if project == needle or project in needle or needle in project:
            score += 3
    haystack = norm(
        " ".join(
            [
                str(entry.get("title") or ""),
                str(entry.get("claim") or ""),
                " ".join(str(tag) for tag in entry.get("tags") or []),
            ]
        )
    )
    hits = 0
    for role in intent.roles:
        for keyword in rules["roles"].get(role, {}).get("keywords", []):
            if norm(keyword) in haystack:
                hits += 1
                if hits >= 4:
                    break
        if hits >= 4:
            break
    score += hits
    task_tokens = {token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", intent.task.lower()) if len(token) >= 2}
    title_tokens = {token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", str(entry.get("title") or "").lower()) if len(token) >= 2}
    if task_tokens & title_tokens:
        score += 1
    return score


def classify_kb(entry: dict) -> str:
    entry_type = entry.get("type")
    if entry_type == "method":
        return "oracle"
    if entry_type == "bug":
        return "trap"
    if entry_type == "run":
        return "reference"
    tags = {norm(tag) for tag in entry.get("tags") or []}
    if tags & {"台架", "执行通道", "硬件", "串口", "声卡"}:
        return "precheck"
    return "fact"


def pick_kb(entries: list[dict], intent: Intent, rules: dict, top_k: int = 5) -> list[dict]:
    scored = []
    for entry in entries:
        score = kb_relevance(entry, intent, rules)
        if score <= 0:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    picked = []
    for score, entry in scored[:top_k]:
        item = dict(entry)
        item["_score"] = score
        item["_use"] = classify_kb(entry)
        picked.append(item)
    return picked


def kb_notes(picked: list[dict]) -> list[dict]:
    notes = []
    for entry in picked:
        notes.append(
            {
                "id": entry["id"],
                "type": entry.get("type"),
                "title": entry.get("title"),
                "claim": entry.get("claim"),
                "use": entry["_use"],
                "confidence": entry.get("confidence"),
                "relevance": entry["_score"],
            }
        )
    return notes


# --------------------------------------------------------------------------- #
# 知识 → 动作：第二阶段消费（trap / knowledge → 恢复步骤与针对性停止条件）
# --------------------------------------------------------------------------- #

KB_SOURCE_KB = "kb"
KB_SOURCE_DEFAULT = "default"


def action_text(entry: dict) -> str:
    """匹配用的正文：标题 + claim + tags。只做 NFKC 与小写，不剥标点。

    剥标点会吃掉 `algo.restart` 里的点，映射表就没法用它做锚点。
    """
    parts = [
        str(entry.get("title") or ""),
        str(entry.get("claim") or ""),
        " ".join(str(tag) for tag in entry.get("tags") or []),
    ]
    return unicodedata.normalize("NFKC", " ".join(part for part in parts if part)).lower()


def spec_matches(entry: dict, spec: dict) -> bool:
    types = spec.get("types")
    if types and entry.get("type") not in types:
        return False
    patterns = spec.get("text_any") or []
    if not patterns:
        return False
    text = action_text(entry)
    return any(re.search(pattern, text) for pattern in patterns)


def kb_scope_linked(entry: dict, intent: Intent) -> bool:
    """条目必须与本请求的设备/项目相关，才允许它改变执行。

    scope 为空或「通用」的条目只作为事实出现在 plan.kb：别的设备的恢复手段不能
    套到本台架上；宁可不生成动作，也不硬塞。
    """
    scope = entry.get("scope") or {}
    device = norm(scope.get("device") or "")
    project = norm(scope.get("project") or "")
    model = norm(intent.model or "")
    node = norm(intent.node or "")
    if device and (device in {node, model} or device in norm(intent.task) or (model and device in model)):
        return True
    if project and project not in {"通用", "general", "common"}:
        needle = norm(intent.project or "")
        if needle and (project == needle or project in needle or needle in project):
            return True
    return False


def kb_action_pool(entries: list[dict], intent: Intent, rules: dict) -> list[dict]:
    """可用来改变执行的条目池：与本请求相关 + 相关度 > 0，按相关度降序。

    不局限在 Top 5：Top 5 是「写进 plan.kb 的篇幅上限」，而一条只被恢复/停止规则
    引用的条目也必须能被找到，否则最值钱的 trap 会被注入上限挤掉。
    """
    scored = []
    for entry in entries:
        score = kb_relevance(entry, intent, rules)
        if score <= 0:
            continue
        if not kb_scope_linked(entry, intent):
            continue
        scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [entry for _, entry in scored]


def extract_recovery_command(entry: dict, spec: dict) -> str | None:
    pattern = spec.get("command_regex")
    if not pattern:
        return None
    match = re.search(pattern, action_text(entry))
    if not match:
        return None
    return clean_text(match.group(1))


def primary_port(intent: Intent, skill_name: str, port_role: str, rules: dict) -> tuple[str | None, str]:
    """恢复步骤要写具体端口：请求优先，skill 默认值兜底，都没有就不生成动作。"""
    explicit = resolve_port_roles(intent, skill_name, rules).get(port_role)
    if explicit:
        return explicit, "bench"
    defaults = rules.get("port_defaults", {}).get(skill_name) or {}
    fallback = clean_text(defaults.get(port_role))
    if fallback:
        return fallback, "default"
    return None, "unknown"


def build_recovery(
    intent: Intent,
    pool: list[dict],
    selected: dict[str, Pick],
    rules: dict,
    plan_id: str,
    skills_home: str | None,
) -> tuple[list[dict], list[str], list[str], list[str]]:
    """把条目里的恢复手段映射成 recovery 步骤。

    返回 (recovery, warnings, needs_confirmation, commands)。
    映射不到具体命令、端口或已选中 skill 的，就不生成步骤——空动作比没动作更危险。
    """
    specs = (rules.get("kb_actions") or {}).get("recovery") or []
    recovery: list[dict] = []
    warnings: list[str] = []
    needs: list[str] = []
    commands: list[str] = []
    if not specs:
        return recovery, warnings, needs, commands
    matched_specs = [
        (spec, [entry for entry in pool if spec_matches(entry, spec.get("match") or {})]) for spec in specs
    ]
    matched_specs = [(spec, entries) for spec, entries in matched_specs if entries]
    if not matched_specs:
        return recovery, warnings, needs, commands
    if not intent.reset_allowed:
        warnings.append(
            "reset_allowed=false：知识库里有恢复手段，但本次禁止任何恢复/复位动作，recovery 为空（故障即停）"
        )
        return recovery, warnings, needs, commands

    seen_commands: set[str] = set()
    for spec, entries in matched_specs:
        skill_name = spec.get("skill")
        if skill_name not in selected:
            warnings.append(
                f"知识条目给出的恢复手段（{spec.get('id')}）要由 {skill_name} 执行，但本轮没有选中它：不生成恢复步骤"
            )
            continue
        port_role = spec.get("port_role")
        port, port_source = primary_port(intent, skill_name, port_role, rules)
        if not port:
            warnings.append(
                f"恢复手段（{spec.get('id')}）缺少 {port_role} 口：请求未给 bench.ports，{skill_name} 也没有登记默认端口"
            )
            needs.append(
                f"恢复步骤缺少 {port_role} 口：请求未提供 bench.ports 且 {skill_name} 未登记默认端口，本轮不生成恢复动作"
            )
            continue
        groups: dict[str, tuple[str, list[dict]]] = {}
        for entry in entries:
            command = extract_recovery_command(entry, spec)
            if not command:
                continue
            key = norm(command)
            if key in seen_commands:
                continue
            groups.setdefault(key, (command, []))[1].append(entry)
        for command, group_entries in groups.values():
            key = norm(command)
            if key in seen_commands:
                continue
            seen_commands.add(key)
            primary = group_entries[0]
            basis = f"kb:{primary['id']}"
            context = build_context(intent, skill_name, plan_id, skills_home, rules)
            context.update(
                {
                    "port": port,
                    "baud": str(spec.get("baud") or 115200),
                    "command": command,
                    "port_source": "来源：bench.ports" if port_source == "bench" else "来源：skill 默认值，执行前必须核对",
                }
            )
            item = {
                "n": f"R{len(recovery) + 1}",
                "action": spec.get("id"),
                "skill": skill_name,
                "label": fill(spec.get("label", ""), context)[0],
                "why": f"{primary.get('title')}：{snippet(primary.get('claim'))}（{basis}）",
                "cmd": fill(spec.get("cmd", ""), context)[0],
                "port": port,
                "port_source": port_source,
                "precheck": [fill(text, context)[0] for text in spec.get("precheck") or []],
                "gate": fill(spec.get("gate", ""), context)[0],
                "after_fail": fill(spec.get("after_fail", ""), context)[0],
                "verify": fill(spec.get("verify", ""), context)[0],
                "record": spec.get("record"),
                "basis": basis,
                "supporting": [f"kb:{entry['id']}" for entry in group_entries[1:]],
                "cmd_verified": False,
            }
            recovery.append(item)
            commands.append(command)
            if port_source == "default":
                warnings.append(
                    f"恢复步骤 R{item['n'][1:]} 使用 {skill_name} 的默认端口 {port}（来源：default）：执行前必须按台架实际端口核对"
                )
                needs.append(
                    f"恢复步骤 R{item['n'][1:]} 的端口来自 skill 默认值（{port}），需核对台架实际 {port_role} 口后才能执行"
                )
            warnings.append(
                f"恢复步骤 R{item['n'][1:]} 的发送方式未在真机验证过（cmd_verified=false）：执行前按 {skill_name} 的串口约定确认"
            )
    return recovery, warnings, needs, commands


def build_stop_rules(
    pool: list[dict],
    rules: dict,
    recovery_commands: list[str],
) -> list[dict]:
    """把「崩溃规律」映射成针对性停止条件。

    - source=kb：规则来自条目自己说出的规律（轮次聚集 / 同一签名 / 恢复手段失效）；
    - source=default：routing 默认检测器，只因条目证明该设备有崩溃史才装上，
      basis 指向激活它的条目——所以没有知识库时 stop_rules 必然为空。
    """
    config = (rules.get("kb_actions") or {}).get("stop_rules") or {}
    found: dict[str, dict] = {}

    def add(rule_text: str, entry: dict, source: str, why: str, spec_id: str | None) -> None:
        current = found.get(rule_text)
        if current is not None:
            if current["source"] == KB_SOURCE_KB or source != KB_SOURCE_KB:
                return
            # 条目自己说出的规律比默认检测器更有依据：原地升级出处
            current.update({"basis": f"kb:{entry['id']}", "source": source, "why": why, "id": spec_id})
            return
        found[rule_text] = {
            "id": spec_id,
            "rule": rule_text,
            "basis": f"kb:{entry['id']}",
            "source": source,
            "why": why,
        }

    for spec in config.get("kb") or []:
        for entry in pool:
            if not spec_matches(entry, spec):
                continue
            rule_text = spec.get("rule") or ""
            if not rule_text:
                continue
            if spec.get("needs_recovery") and not recovery_commands:
                continue
            if "{command}" in rule_text:
                if not recovery_commands:
                    continue
                rule_text = rule_text.replace("{command}", recovery_commands[0])
            add(
                rule_text,
                entry,
                KB_SOURCE_KB,
                f"kb:{entry['id']}：{snippet(entry.get('claim'))}",
                spec.get("id"),
            )

    crash_history = config.get("crash_history") or {}
    crash_entries = [entry for entry in pool if spec_matches(entry, crash_history)]
    if crash_entries:
        primary = crash_entries[0]
        for spec in config.get("default") or []:
            rule_text = spec.get("rule") or ""
            if not rule_text:
                continue
            add(
                rule_text,
                primary,
                KB_SOURCE_DEFAULT,
                f"kb:{primary['id']} 记录该设备有崩溃史，安装默认止损检测器",
                spec.get("id"),
            )
    return list(found.values())


# --------------------------------------------------------------------------- #
# 计划装配
# --------------------------------------------------------------------------- #

def plan_id_for(intent: Intent, plan_date: str) -> str:
    payload = json.dumps(
        {
            "task": intent.task,
            "device": {"model": intent.model, "project": intent.project, "firmware": intent.firmware},
            "bench": {
                "os": intent.os_name,
                "node": intent.node,
                "ports": intent.ports,
                "port_roles": intent.explicit_port_roles,
                "playback_key": intent.playback_key,
            },
            "constraints": {
                "hours": intent.hours,
                "overnight": intent.overnight,
                "reset_allowed": intent.reset_allowed,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:4]
    return f"p-{plan_date}-{digest}"


def lower_confidence(current: str, candidate: str) -> str:
    return current if CONFIDENCE_RANK[current] >= CONFIDENCE_RANK[candidate] else candidate


def preconditions_skill(ordered: list[str], rules: dict) -> str:
    """共享前置检查里的端口要按“真正使用端口的 skill”解析，而不是固定第一个。

    否则请求没给端口时，voice_test 的前置检查会拿 usb-tree 去查默认端口，误报“来源未知”。
    """
    for name in ordered:
        templates = rules.get("steps", {}).get(name) or []
        if any("{effective_ports}" in json.dumps(t, ensure_ascii=False) for t in templates):
            return name
    return ordered[0] if ordered else "usb-tree"


def build_reset_policy(intent: Intent, ordered: list[str], rules: dict) -> dict:
    """reset_allowed 的口径：调度层不执行复位，也不排独立复位步骤。

    - 复位是恢复动作，属于被选中 skill 的职责；调度层只声明允许/禁止与证据要求。
    - 复位命令必须先在台架实测有效；未验证的命令（如 tcl-test-sub 的 uut-switch1）只登记、不自动执行。
    """
    channels: list[dict] = []
    if intent.reset_allowed:
        for name in ordered:
            entry = (rules.get("reset_channels") or {}).get(name)
            if not entry:
                continue
            channels.append(
                {
                    "skill": name,
                    "command": entry.get("command"),
                    "verified": bool(entry.get("verified", False)),
                    "log": entry.get("log"),
                    "note": entry.get("note"),
                }
            )
        decision = "允许复位，但不单独排复位步骤"
        reason = (
            "constraints.reset_allowed=true：允许在 sticky failure 或明确恢复流程中复位；"
            "复位属于被选中 skill 的恢复动作，调度层不执行，也不生成独立复位步骤"
        )
        requirement = "复位命令必须先在本台架实测有效并留下证据；未验证的命令不得当作自动恢复手段"
        if not channels:
            requirement += "；当前选中的 skill 未登记复位通道，执行前须先确认人工复位方式"
    else:
        decision = "禁止复位"
        reason = "constraints.reset_allowed=false：故障即停，不执行任何复位/恢复动作"
        requirement = "保持现场并保留证据；不得用复位掩盖失败"
    return {
        "allowed": intent.reset_allowed,
        "decision": decision,
        "reason": reason,
        "requirement": requirement,
        "channels": channels,
    }


def build_plan(
    request: dict,
    registry: list[dict],
    kb_entries: list[dict],
    rules: dict,
    plan_date: str,
    registry_rev: str,
    skills_home: str | None = None,
    top_kb: int = 5,
    kb_available: bool = True,
) -> dict:
    intent = parse_request(request, rules)
    needs = list(intent.needs_confirmation)
    warnings: list[str] = []
    confidence = "high"

    if intent.model is None:
        confidence = "low"
    if intent.os_name is None:
        confidence = lower_confidence(confidence, "medium")
    if kb_entries:
        picked = pick_kb(kb_entries, intent, rules, top_kb)
    else:
        picked = []
        if not kb_available:
            warnings.append("知识库不可用：本轮没有注入已知坑/前置检查（--allow-missing-kb 显式降级）")
            confidence = lower_confidence(confidence, "medium")

    selected, excluded, unfilled, standards, runtime_notes, sel_warnings = select_skills(
        registry, intent, rules, kb_entries
    )
    warnings.extend(sel_warnings)

    for role in unfilled:
        label = rules["roles"].get(role, {}).get("label", role)
        needs.append(f"能力「{label}」在台账中找不到可用 skill：本轮不硬选，需确认替代入口或放宽设备/项目约束")
        confidence = "low"

    for pick in selected.values():
        if pick.skill.get("status") == "legacy":
            needs.append(f"{pick.skill['name']} 为 legacy（registry:{pick.skill['name']}.status），需确认仍适用于 {intent.model or '当前设备'}")
            confidence = lower_confidence(confidence, "medium")

    plan_id = plan_id_for(intent, plan_date)
    ordered = order_selected(selected, rules)
    steps, unresolved, step_warnings, port_needs = build_steps(ordered, selected, intent, rules, plan_id, skills_home)
    warnings.extend(step_warnings)
    for key in unresolved:
        label = PLACEHOLDER_LABELS.get(key, key)
        needs.append(f"缺少输入：{label}；计划中对应位置以 <未提供:{key}> 标记，补齐后才能执行")
        confidence = lower_confidence(confidence, "medium")
    for note in port_needs:
        if note not in needs:
            needs.append(note)
    if port_needs:
        confidence = lower_confidence(confidence, "medium")

    reset_policy = build_reset_policy(intent, ordered, rules)
    for channel in reset_policy["channels"]:
        if channel["verified"]:
            continue
        note = (
            f"复位通道 {channel['command']}（{channel['skill']}）未验证：reset_allowed=true 只表示允许复位，"
            f"执行前须先在台架实测有效并写 {channel['log']}"
        )
        if note not in needs:
            needs.append(note)
        confidence = lower_confidence(confidence, "medium")

    if not steps:
        confidence = "low"
        needs.append("本轮没有任何可执行步骤：需确认任务意图或补充设备信息")

    # 第二阶段消费：把 trap/knowledge 里的恢复手段与崩溃规律变成可执行动作。
    action_pool = kb_action_pool(kb_entries, intent, rules)
    recovery, recovery_warnings, recovery_needs, recovery_commands = build_recovery(
        intent, action_pool, selected, rules, plan_id, skills_home
    )
    warnings.extend(recovery_warnings)
    for note in recovery_needs:
        if note not in needs:
            needs.append(note)
        confidence = lower_confidence(confidence, "medium")
    stop_rules = build_stop_rules(action_pool, rules, recovery_commands)

    preconditions: list[str] = []
    context = build_context(intent, preconditions_skill(ordered, rules), plan_id, skills_home, rules)
    for role in intent.roles:
        for template in rules.get("preconditions", {}).get(role, []):
            text, _ = fill(template, context)
            if text not in preconditions:
                preconditions.append(text)
    for template in rules.get("preconditions", {}).get("input", []):
        text, _ = fill(template, context)
        if text not in preconditions:
            preconditions.append(text)
    if intent.long_run:
        for template in rules.get("long_run_preconditions", []):
            text, _ = fill(template, context)
            if text not in preconditions:
                preconditions.append(text)
    for note in kb_notes(picked):
        if note["use"] == "precheck":
            line = f"{note['title']}：{note['claim']}（kb:{note['id']}）"
            if line not in preconditions:
                preconditions.append(line)

    stop_conditions = list(rules.get("stop_conditions", []))
    if intent.long_run:
        for item in rules.get("long_run_stop_conditions", []):
            if item not in stop_conditions:
                stop_conditions.append(item)
    patrol = list(rules.get("patrol", [])) if intent.long_run else []

    for signal, advisory in rules.get("advisory_signals", {}).items():
        if any(norm(keyword) in norm(intent.task) for keyword in advisory.get("keywords", [])):
            line = f"{advisory['skill']}：{advisory['note']}"
            if line not in standards:
                standards.append(line)

    notes = kb_notes(picked)
    # 步骤/排除理由里引用到的 kb 条目（例如「统一走 lstts」）必须出现在 kb 与 sources 中。
    referenced = set()
    for step in steps:
        referenced.update(re.findall(r"kb:([A-Za-z0-9_.-]+)", " ".join(step.get("basis") or [])))
    for item in excluded:
        referenced.update(re.findall(r"kb:([A-Za-z0-9_.-]+)", item.get("reason") or ""))
    for block in (recovery, stop_rules):
        referenced.update(re.findall(r"kb:([A-Za-z0-9_.-]+)", json.dumps(block, ensure_ascii=False)))
    known_ids = {note["id"] for note in notes}
    for entry in kb_entries:
        if entry["id"] in referenced and entry["id"] not in known_ids:
            extra = dict(entry)
            extra["_score"] = 0
            extra["_use"] = classify_kb(entry)
            notes.append(
                {
                    "id": extra["id"],
                    "type": extra.get("type"),
                    "title": extra.get("title"),
                    "claim": extra.get("claim"),
                    "use": extra["_use"],
                    "confidence": extra.get("confidence"),
                    "relevance": 0,
                }
            )
            known_ids.add(entry["id"])

    sources = [f"registry/skills.json@{registry_rev}", f"routing:{rules.get('version', 'unknown')}"]
    sources.extend(f"kb:{note['id']}" for note in notes)

    plan = {
        "schema": SCHEMA_PLAN,
        "plan_id": plan_id,
        "task": intent.task,
        "confidence": confidence,
        "needs_confirmation": needs,
        "request": {
            "device": {"model": intent.model, "project": intent.project, "firmware": intent.firmware},
            "bench": {
                "node": intent.node,
                "os": intent.os_name,
                "ports": intent.ports,
                "port_roles": intent.explicit_port_roles,
                "playback_key": intent.playback_key,
            },
            "constraints": {
                "hours": intent.hours,
                "overnight": intent.overnight,
                "reset_allowed": intent.reset_allowed,
            },
            "signals": sorted(intent.signals),
            "long_run": intent.long_run,
        },
        "steps": steps,
        "reset_policy": reset_policy,
        "standards": standards,
        "runtime_notes": runtime_notes,
        "excluded": excluded,
        "preconditions": preconditions,
        "stop_conditions": stop_conditions,
        "stop_rules": stop_rules,
        "recovery": recovery,
        "patrol": patrol,
        "kb": notes,
        "sources": sources,
        "warnings": warnings,
        "limits": [
            "本计划只做选型与排序：不执行设备动作、不联网、不写台账与知识库",
            "cmd 字段按 skill 安装根目录 $HOME/.codex/skills/<skill> 书写（Windows PowerShell 5.1 与 Linux bash 通用）；实际路径不同时用 --skills-home 重新生成",
            "台账只登记入口，未登记调用模板的 skill 会标记 cmd_verified=false，执行前必须按其 SKILL.md 确认参数",
            "代码测试通过不等于真机验证通过：执行方必须自己跑 preflight 并留下证据",
            "recovery 步骤同样由被选中 skill 执行：调度层只给命令、出处与失败退出点，不代发串口",
        ],
    }
    return plan


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def validate_plan(plan: dict, registry: list[dict], rules: dict) -> list[str]:
    problems: list[str] = []
    by_name = {skill["name"]: skill for skill in registry}
    if plan.get("schema") != SCHEMA_PLAN:
        problems.append(f"schema 必须是 {SCHEMA_PLAN}")
    if not re.match(r"^p-\d{8}-[0-9a-f]{4}$", str(plan.get("plan_id") or "")):
        problems.append("plan_id 必须匹配 p-YYYYMMDD-<4hex>")
    if plan.get("confidence") not in CONFIDENCES:
        problems.append("confidence 必须是 high/medium/low")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        problems.append("steps 必须是数组")
        steps = []
    if not steps and plan.get("confidence") != "low":
        problems.append("没有步骤时必须给 confidence: low")
    if plan.get("confidence") == "low" and not plan.get("needs_confirmation"):
        problems.append("confidence: low 时必须列出 needs_confirmation")
    if not plan.get("sources"):
        problems.append("sources 不能为空")
    reset = plan.get("reset_policy")
    if reset is not None:
        if not isinstance(reset, dict) or not isinstance(reset.get("allowed"), bool):
            problems.append("reset_policy 必须是对象且含 allowed:boolean")
        else:
            for channel in reset.get("channels") or []:
                if channel.get("skill") not in by_name:
                    problems.append(f"reset_policy 里的 skill {channel.get('skill')} 不在台账中")
    for index, step in enumerate(steps, 1):
        if step.get("n") != index:
            problems.append(f"第 {index} 步 n 不连续")
        skill_name = step.get("skill")
        if skill_name not in by_name:
            problems.append(f"步骤 {index} 的 skill {skill_name} 不在台账中")
            continue
        skill = by_name[skill_name]
        if skill.get("status") == "archived":
            problems.append(f"步骤 {index} 选了 archived skill {skill_name}")
        if (skill.get("entry") or "").endswith(".md"):
            problems.append(f"步骤 {index} 的 {skill_name} 只有规范文档（entry 是 .md），不能作为执行步骤")
        if step.get("phase") not in rules.get("phase_order", []):
            problems.append(f"步骤 {index} 的 phase {step.get('phase')} 不在阶段序列")
        for key in ("cmd", "gate", "label", "rollback"):
            if not str(step.get(key) or "").strip():
                problems.append(f"步骤 {index} 缺 {key}")
        if step.get("on_fail") not in ON_FAIL_VALUES:
            problems.append(f"步骤 {index} 的 on_fail {step.get('on_fail')} 不合法")
        if "<未提供:" in json.dumps(step, ensure_ascii=False) and plan.get("confidence") == "high":
            problems.append(f"步骤 {index} 有未填占位符，但 confidence 是 high")
    for item in plan.get("excluded") or []:
        if not str(item.get("reason") or "").strip():
            problems.append(f"excluded 里的 {item.get('skill')} 没有理由")
        if item.get("skill") not in by_name:
            problems.append(f"excluded 里的 {item.get('skill')} 不在台账中")
    sources = set(plan.get("sources") or [])
    for index, item in enumerate(plan.get("recovery") or [], 1):
        if str(item.get("n") or "") != f"R{index}":
            problems.append(f"recovery 第 {index} 条的 n 必须是 R{index}")
        skill_name = item.get("skill")
        if skill_name not in by_name:
            problems.append(f"recovery 第 {index} 条的 skill {skill_name} 不在台账中")
        else:
            skill = by_name[skill_name]
            if skill.get("status") == "archived":
                problems.append(f"recovery 第 {index} 条选了 archived skill {skill_name}")
            if (skill.get("entry") or "").endswith(".md"):
                problems.append(f"recovery 第 {index} 条的 {skill_name} 只有规范文档（entry 是 .md），不能执行恢复动作")
        for key in ("cmd", "why", "gate", "after_fail", "record"):
            if not str(item.get(key) or "").strip():
                problems.append(f"recovery 第 {index} 条缺 {key}")
        basis = str(item.get("basis") or "")
        if not basis.startswith("kb:"):
            problems.append(f"recovery 第 {index} 条必须以 kb: 条目为出处（basis 现在是 {basis!r}）")
        elif basis not in sources:
            problems.append(f"recovery 第 {index} 条的出处 {basis} 不在 sources 中")
        for extra in item.get("supporting") or []:
            if not str(extra).startswith("kb:") or extra not in sources:
                problems.append(f"recovery 第 {index} 条的 supporting 出处 {extra} 不合法或不在 sources 中")
        if item.get("port_source") == "default" and not any("默认" in str(note) for note in plan.get("needs_confirmation") or []):
            problems.append(f"recovery 第 {index} 条用了 skill 默认端口，但 needs_confirmation 里没有要求核对")
    for index, item in enumerate(plan.get("stop_rules") or [], 1):
        if not str(item.get("rule") or "").strip():
            problems.append(f"stop_rules 第 {index} 条缺 rule")
        if item.get("source") not in {KB_SOURCE_KB, KB_SOURCE_DEFAULT}:
            problems.append(f"stop_rules 第 {index} 条的 source {item.get('source')!r} 必须是 kb 或 default")
        basis = str(item.get("basis") or "")
        if not basis.startswith("kb:"):
            problems.append(f"stop_rules 第 {index} 条必须以 kb: 条目为出处（basis 现在是 {basis!r}）")
        elif basis not in sources:
            problems.append(f"stop_rules 第 {index} 条的出处 {basis} 不在 sources 中")
    return problems


def check_rules(rules: dict, registry: list[dict]) -> list[str]:
    problems: list[str] = []
    by_name = {skill["name"]: skill for skill in registry}
    phase_order = rules.get("phase_order") or []
    if not phase_order:
        problems.append("phase_order 不能为空")
    for role, body in (rules.get("roles") or {}).items():
        if body.get("phase") not in phase_order:
            problems.append(f"role {role} 的 phase 不在 phase_order")
        if not body.get("keywords"):
            problems.append(f"role {role} 没有关键词")
    for signal, keywords in (rules.get("task_signals") or {}).items():
        if signal not in (rules.get("roles") or {}):
            problems.append(f"task_signals 里的 {signal} 不是已定义角色")
        if not keywords:
            problems.append(f"task_signals.{signal} 没有关键词")
    for signal, implied in (rules.get("signal_implies") or {}).items():
        if signal not in (rules.get("roles") or {}):
            problems.append(f"signal_implies 的源 {signal} 不是已定义角色")
        for target in implied:
            if target not in (rules.get("roles") or {}):
                problems.append(f"signal_implies.{signal} 指向未定义角色 {target}")
    for name, templates in (rules.get("steps") or {}).items():
        if name not in by_name:
            problems.append(f"steps.{name} 不在台账中")
        for template in templates:
            if template.get("phase") not in phase_order:
                problems.append(f"steps.{name} 的 phase {template.get('phase')} 不在 phase_order")
            for key in ("label", "cmd", "gate"):
                if not str(template.get(key) or "").strip():
                    problems.append(f"steps.{name} 缺 {key}")
            if template.get("on_fail", "stop") not in ON_FAIL_VALUES:
                problems.append(f"steps.{name} 的 on_fail 不合法")
            if template.get("when") not in (None, "long_run", "short_run"):
                problems.append(f"steps.{name} 的 when 不合法")
    for name in (rules.get("supersedes") or {}):
        if name not in by_name:
            problems.append(f"supersedes 的 {name} 不在台账中")
    for name, target in (rules.get("supersedes") or {}).items():
        if target not in by_name:
            problems.append(f"supersedes.{name} 的目标 {target} 不在台账中")
        if target == name:
            problems.append(f"supersedes.{name} 指向自己")
    for name in (rules.get("dependency_steps") or {}):
        if name not in by_name:
            problems.append(f"dependency_steps 的 {name} 不在台账中")
    for name, roles in (rules.get("port_roles") or {}).items():
        if name not in by_name:
            problems.append(f"port_roles 的 {name} 不在台账中")
        if not roles:
            problems.append(f"port_roles.{name} 为空")
    for name, defaults in (rules.get("port_defaults") or {}).items():
        if name not in by_name:
            problems.append(f"port_defaults 的 {name} 不在台账中")
        if not defaults:
            problems.append(f"port_defaults.{name} 为空")
        roles = rules.get("port_roles", {}).get(name) or []
        for role, port in defaults.items():
            if not str(port or "").strip():
                problems.append(f"port_defaults.{name}.{role} 为空")
            if role not in roles:
                problems.append(f"port_defaults.{name}.{role} 不在 port_roles.{name} 里")
    for name, entry in (rules.get("reset_channels") or {}).items():
        if name not in by_name:
            problems.append(f"reset_channels 的 {name} 不在台账中")
        if not str(entry.get("command") or "").strip():
            problems.append(f"reset_channels.{name} 缺 command")
        if not isinstance(entry.get("verified"), bool):
            problems.append(f"reset_channels.{name}.verified 必须是布尔值")
    for signal, advisory in (rules.get("advisory_signals") or {}).items():
        if advisory.get("skill") not in by_name:
            problems.append(f"advisory_signals.{signal} 的 skill 不在台账中")
    kb_actions = rules.get("kb_actions") or {}
    recovery_ids = set()
    for spec in kb_actions.get("recovery") or []:
        spec_id = spec.get("id")
        if not spec_id:
            problems.append("kb_actions.recovery 有一条缺 id")
            spec_id = "?"
        elif spec_id in recovery_ids:
            problems.append(f"kb_actions.recovery 的 id {spec_id} 重复")
        recovery_ids.add(spec_id)
        skill_name = spec.get("skill")
        if skill_name not in by_name:
            problems.append(f"kb_actions.recovery.{spec_id} 的 skill {skill_name} 不在台账中")
        elif spec.get("port_role") not in (rules.get("port_roles", {}).get(skill_name) or []):
            problems.append(
                f"kb_actions.recovery.{spec_id} 的 port_role {spec.get('port_role')} 不在 port_roles.{skill_name} 里"
            )
        for key in ("label", "cmd", "gate", "after_fail", "verify", "record"):
            if not str(spec.get(key) or "").strip():
                problems.append(f"kb_actions.recovery.{spec_id} 缺 {key}")
        try:
            re.compile(spec.get("command_regex") or "")
        except re.error as exc:
            problems.append(f"kb_actions.recovery.{spec_id} 的 command_regex 不合法: {exc}")
        match_spec = spec.get("match") or {}
        if not match_spec.get("text_any"):
            problems.append(f"kb_actions.recovery.{spec_id} 的 match 缺 text_any（没有匹配条件就永远不生效）")
        for pattern in match_spec.get("text_any") or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"kb_actions.recovery.{spec_id} 的 match 匹配式 {pattern} 不合法: {exc}")
    stop_cfg = kb_actions.get("stop_rules") or {}
    crash_spec = stop_cfg.get("crash_history") or {}
    if not crash_spec.get("text_any"):
        problems.append("kb_actions.stop_rules.crash_history 缺 text_any（默认检测器就没有激活条件）")
    for kind in ("kb", "default"):
        seen: set[str] = set()
        for spec in stop_cfg.get(kind) or []:
            spec_id = spec.get("id")
            if not spec_id:
                problems.append(f"kb_actions.stop_rules.{kind} 有一条缺 id")
            elif spec_id in seen:
                problems.append(f"kb_actions.stop_rules.{kind} 的 id {spec_id} 重复")
            seen.add(spec_id)
            if not str(spec.get("rule") or "").strip():
                problems.append(f"kb_actions.stop_rules.{kind}.{spec_id} 缺 rule")
            if kind == "kb":
                patterns = spec.get("text_any") or []
                if not patterns:
                    problems.append(f"kb_actions.stop_rules.kb.{spec_id} 缺 text_any（没有匹配条件就永远不生效）")
                for pattern in patterns:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        problems.append(f"kb_actions.stop_rules.kb.{spec_id} 的匹配式 {pattern} 不合法: {exc}")
    for pattern in crash_spec.get("text_any") or []:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"kb_actions.stop_rules.crash_history 的匹配式 {pattern} 不合法: {exc}")
    for role in rules.get("roles") or {}:
        if not any(matched_role_keywords(skill, role, rules) for skill in registry):
            problems.append(f"角色 {role} 在台账里没有任何 skill 命中，规则不可用")
    return problems


# --------------------------------------------------------------------------- #
# 渲染与 CLI
# --------------------------------------------------------------------------- #

def render_text(plan: dict) -> str:
    lines = [
        f"计划 {plan['plan_id']}｜置信度 {plan['confidence']}｜{plan['task']}",
        "",
        f"步骤（{len(plan['steps'])} 步）：",
    ]
    for step in plan["steps"]:
        lines.append(f"  {step['n']}. [{step['phase']}] {step['skill']} · {step['label']}")
        lines.append(f"     cmd: {step['cmd']}")
        lines.append(f"     gate: {step['gate']}")
        if not step.get("cmd_verified", True):
            lines.append("     注意: cmd_verified=false，执行前需按 SKILL.md 确认参数")
    if plan.get("excluded"):
        lines.append("")
        lines.append("排除（可复核）：")
        for item in plan["excluded"]:
            lines.append(f"  - {item['skill']}: {item['reason']}")
    reset = plan.get("reset_policy")
    if reset:
        lines.append("")
        lines.append(f"复位口径：{reset.get('decision')}（reset_allowed={str(reset.get('allowed')).lower()}）")
        lines.append(f"  - {reset.get('reason')}")
        lines.append(f"  - 要求：{reset.get('requirement')}")
        for channel in reset.get("channels") or []:
            verified = "已验证" if channel.get("verified") else "未验证"
            lines.append(
                f"  - 通道 {channel.get('skill')}: {channel.get('command')}（{verified}，日志 {channel.get('log')}）"
            )
    if plan.get("needs_confirmation"):
        lines.append("")
        lines.append("需要人确认：")
        for item in plan["needs_confirmation"]:
            lines.append(f"  - {item}")
    if plan.get("recovery"):
        lines.append("")
        lines.append("恢复预案（来自知识库）：")
        for item in plan["recovery"]:
            lines.append(f"  {item['n']}. {item['skill']} · {item['label']}（{item['basis']}）")
            lines.append(f"     cmd: {item['cmd']}")
            lines.append(f"     gate: {item['gate']}")
            lines.append(f"     失败后: {item['after_fail']}")
            lines.append(f"     记账: {item['record']}")
    if plan.get("stop_rules"):
        lines.append("")
        lines.append("针对性停止条件（来自知识库）：")
        for item in plan["stop_rules"]:
            lines.append(f"  - [{item['source']}] {item['rule']}（{item['basis']}）")
    if plan.get("warnings"):
        lines.append("")
        lines.append("告警：")
        for item in plan["warnings"]:
            lines.append(f"  - {item}")
    lines.append("")
    lines.append("边界：本计划不执行任何设备动作；执行与取证由被选中的 skill 完成。")
    return "\n".join(lines)


def registry_rev_for(path: Path, override: str | None) -> str:
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    data = path.read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()[:12]


def read_request(path: Path) -> dict:
    data = read_json(path)
    if isinstance(data, dict) and "request" in data and isinstance(data["request"], dict):
        data = data["request"]
    return data


def command_plan(args) -> int:
    registry_path = resolve_registry_path(args.registry)
    registry = load_registry(registry_path)
    kb_path = resolve_kb_path(args.kb)
    kb_available = kb_path is not None
    if kb_path is None and not args.allow_missing_kb:
        raise DispatchError(
            "找不到知识库（ark-kb 的 entries/）；用 --kb 指定本地快照，或显式 --allow-missing-kb 降级",
            EXIT_ENV,
        )
    kb_entries = load_kb(kb_path) if kb_path is not None else []
    rules = load_rules(Path(args.routing).expanduser() if args.routing else Path(__file__).resolve().parent / "routing.json")
    rule_problems = check_rules(rules, registry)
    if rule_problems:
        raise DispatchError("选型规则与台账不一致：" + "；".join(rule_problems), EXIT_ENV)
    request = read_request(Path(args.request).expanduser())
    plan_date = args.date or datetime.datetime.now().strftime("%Y%m%d")
    plan = build_plan(
        request,
        registry,
        kb_entries,
        rules,
        plan_date,
        registry_rev_for(registry_path, args.registry_rev),
        skills_home=args.skills_home,
        top_kb=args.top_kb,
        kb_available=kb_available,
    )
    problems = validate_plan(plan, registry, rules)
    if problems:
        raise DispatchError("计划自检失败：" + "；".join(problems), EXIT_PLAN)
    if args.require_confidence:
        allowed = {"high": 0, "medium": 1}
        if CONFIDENCE_RANK[plan["confidence"]] > allowed[args.require_confidence]:
            raise DispatchError(
                f"计划置信度 {plan['confidence']} 低于要求的 {args.require_confidence}",
                EXIT_PLAN,
            )
    text = render_text(plan) if args.format == "text" else json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            (text if args.format == "text" else json.dumps(plan, ensure_ascii=False, indent=2)) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {out_path}", file=sys.stderr)
    else:
        print(text)
    return EXIT_OK


def command_check(args) -> int:
    plan = read_json(Path(args.plan).expanduser())
    registry_path = resolve_registry_path(args.registry)
    registry = load_registry(registry_path)
    rules = load_rules(Path(args.routing).expanduser() if args.routing else Path(__file__).resolve().parent / "routing.json")
    problems = validate_plan(plan, registry, rules)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return EXIT_PLAN
    print(f"OK {plan.get('plan_id')}：{len(plan.get('steps') or [])} 步，全部 skill 在台账内且非 archived")
    return EXIT_OK


def command_rules(args) -> int:
    registry_path = resolve_registry_path(args.registry)
    registry = load_registry(registry_path)
    rules = load_rules(Path(args.routing).expanduser() if args.routing else Path(__file__).resolve().parent / "routing.json")
    problems = check_rules(rules, registry)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return EXIT_ENV
    print(f"OK {rules.get('version')}：{len(rules.get('roles', {}))} 个角色，{len(rules.get('steps', {}))} 个 skill 登记了调用模板")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dispatch.py",
        description="方舟调度：给定任务/设备/台架条件，产出可执行的 plan.v1（只决策，不执行）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="生成计划")
    plan_parser.add_argument("--request", required=True, help="任务请求 JSON（dispatch.request.v1）")
    plan_parser.add_argument("--registry", help="要读取的 registry/skills.json 路径")
    plan_parser.add_argument("--kb", help="ark-kb 检出目录或 entries 目录")
    plan_parser.add_argument("--routing", help="routing.json 路径（默认同目录）")
    plan_parser.add_argument("--out", help="把计划写入文件")
    plan_parser.add_argument("--format", choices=["json", "text"], default="json")
    plan_parser.add_argument("--date", help="plan_id 使用的日期（YYYYMMDD）")
    plan_parser.add_argument("--registry-rev", help="台账版本标识（默认 git HEAD 或文件 sha256）")
    plan_parser.add_argument("--skills-home", help="执行节点的 skill 安装根目录（默认 $HOME/.codex/skills）")
    plan_parser.add_argument("--top-kb", type=int, default=5, help="知识库注入条数上限（默认 5）")
    plan_parser.add_argument("--require-confidence", choices=["high", "medium"], help="低于该置信度时以退出码 4 失败")
    plan_parser.add_argument("--allow-missing-kb", action="store_true", help="知识库缺失时降级继续（默认失败关闭）")
    plan_parser.set_defaults(func=command_plan)

    check_parser = sub.add_parser("check", help="校验已有计划：skill 是否都在台账、是否非 archived、字段是否齐全")
    check_parser.add_argument("--plan", required=True)
    check_parser.add_argument("--registry")
    check_parser.add_argument("--routing")
    check_parser.set_defaults(func=command_check)

    rules_parser = sub.add_parser("rules", help="校验 routing.json 与台账是否一致")
    rules_parser.add_argument("--registry")
    rules_parser.add_argument("--routing")
    rules_parser.set_defaults(func=command_rules)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DispatchError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
