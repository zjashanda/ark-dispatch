---
name: ark-dispatch
description: 方舟调度：给定任务、设备与现场条件，查能力台账和知识库，输出一份可执行的 plan.v1（选哪些 skill、什么顺序、每步入口/门禁/失败退出点/回滚），本身不执行任何设备动作。Use when 需要为设备测试任务自动选型排计划、判断某个 skill 适用于哪台设备、或在多 skill 之间决定先后与停止条件。
---

# 方舟调度（ark-dispatch）

**只做决策，不做执行。** 输入一句话任务与设备/台架条件，输出 `plan.v1`；真正的串口、声卡、烧录动作由被选中的 skill 自己完成。

这是「整合」的落点：不是把所有功能塞进一个大 skill，而是让薄薄一层知道**该找谁、按什么顺序、哪一步停**。

## 何时用

- 拿到一个设备测试任务，需要在 30+ 个 skill 里决定用哪几个、先后顺序。
- 要复核"为什么这次没选某个 skill"——排除理由必须能指回台账字段或知识条目。
- 长跑/通宵前要一份带停止条件与巡检项的计划。

## 何时不用

- 要真的驱动设备、收日志、出报告 → 直接调用计划里选中的 skill。
- 只是查某个 skill 的用法 → 读它自己的 `SKILL.md`。
- 台账或知识库都没有本地快照 → 先检出 `ark-kb`，本 skill 不联网抓取。

## 输入（dispatch.request.v1）

```json
{
  "task": "验证 TCL 离线语音唤醒与命令词",
  "device": {"model": "CSK7+HI3863", "project": "tcl"},
  "bench": {
    "node": "win009",
    "os": "windows",
    "ports": ["COM3", "COM4", "COM6"],
    "playback_key": "VID_8765&PID_5678:9_387C0FC7_0_0000"
  },
  "constraints": {"hours": 2, "overnight": false, "reset_allowed": false}
}
```

字段与可选扩展见 [references/plan-schema.md](references/plan-schema.md)。

## 怎么跑

```bash
# 1. 生成计划（默认读同级 ../ark-kb 的台账与知识库）
python scripts/dispatch.py plan --request examples/tcl-offline.json --out plan.json

# 2. 人读版
python scripts/dispatch.py plan --request examples/tcl-offline.json --format text

# 3. 复核对拍：计划里的 skill 是否都在台账、是否非 archived
python scripts/dispatch.py check --plan plan.json

# 4. 校验选型规则与台账是否仍然一致
python scripts/dispatch.py rules
```

本地没有同级 `ark-kb` 时显式指定快照：

```bash
python scripts/dispatch.py plan \
  --request examples/tcl-offline.json \
  --registry /path/to/ark-kb/registry/skills.json \
  --kb /path/to/ark-kb \
  --registry-rev "$(git -C /path/to/ark-kb rev-parse --short HEAD)"
```

退出码：`0` 产出计划；`2` 请求不合法；`3` 台账/知识库缺失、规则与台账不一致（失败关闭）；`4` 计划自检不通过或置信度低于 `--require-confidence`。

## 决策流程（确定性，不靠模型猜）

1. **抽信号**：任务文本命中哪些能力信号（唤醒/命令词、烧录、合成、播放、录音、端口…），并补上隐含依赖（语音验证 → 播报 + 端口）。
2. **过台账门禁**：`status != archived`、`platform` 含台架系统、项目/型号匹配；`reuse: low` 的设备专用 skill 必须命中项目或型号，否则带理由剔除。
3. **按角色竞标**：同一能力只选最贴合的一个——型号命中 > 项目命中 > 设备专用 > 通用实现；`supersedes` 明示被覆盖的旧实现。
4. **查知识库**：按 device/project/能力取 Top 5，条目分成 `precheck` / `oracle` / `trap` / `fact`，前置检查写进计划；"统一走 lstts" 这类规范会直接影响选型并在理由里标注条目 id。
5. **排序出计划**：依赖关系拓扑排序，同层按 `设备接入 → 动作 → 素材 → 执行 → 证据`；每一步写清入口命令、前置校验、门禁、失败退出点、回滚方式与证据位置。

## 硬边界

- **不执行任何设备动作**：只读本地台账与知识库，输出 JSON/文本，不碰串口、声卡、固件。
- 计划里每个 skill 必须能在台账查到且不是 `archived`；查不到不选。`check` 子命令与单测都会拦。
- 计划里的端口必须可溯源：请求 `bench.ports` 优先，其次 skill 默认值；回落默认值或来源未知时，计划降 `medium` 并要求核对，不得让执行方去开不存在的串口。
- 调度层不执行复位：`reset_allowed=true` 只表示允许被选中 skill 在恢复流程里复位；未验证的复位命令只登记，不自动执行。
- 选择理由必须可复核：命中哪个台账字段、哪条知识条目，都写在 `basis` / `excluded[].reason` / `sources` 里。
- 没有把握就 `confidence: low` 并列出 `needs_confirmation`，不硬选；设备型号未知时尤其如此。
- 知识库只读：不回写命中计数、不改条目、不把执行结果写进台账。
- `cmd` 按 skill 安装根目录 `$HOME/.codex/skills/<skill>` 书写；未登记调用模板的 skill 会标 `cmd_verified: false`，执行前必须按其 `SKILL.md` 确认参数。

## 目录

```
ark-dispatch/
  SKILL.md
  scripts/dispatch.py        # plan / check / rules
  scripts/routing.json       # 能力角色、竞标与步骤模板（机器可读）
  references/
    plan-schema.md           # 请求与计划的字段契约、退出码、置信度规则
    routing-rules.md         # 选型规则的人读解释与扩展方式
  tests/
    test_dispatch.py         # 5 组 golden case + 失败关闭 + 规则完整性
    fixtures/                # 台账与知识库的固定快照（离线）
  examples/                  # 5 组可直接跑的任务请求
```

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：TCL 离线唤醒、CSK6012 烧录、新语料合成、8 小时通宵、设备型号未知；另含计划篡改拦截、台账/知识库缺失失败关闭与规则完整性。全部离线，不访问真实设备与网络。

**代码测试通过 ≠ 真机验证通过**：本 skill 的测试只证明"选型与计划格式正确"，真机结论由执行方按计划跑完并留下证据后另行判定。
