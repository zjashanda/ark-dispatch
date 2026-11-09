# 请求与计划契约（v1）

## 1. 请求 `dispatch.request.v1`

顶层对象。除 `task` 外都可缺省，缺省项会进入 `needs_confirmation` 或拉低置信度，不会被静默猜测。

| 字段 | 类型 | 说明 |
|---|---|---|
| `task` | string | **必填**。一句话任务，能力信号从这里抽取 |
| `device.model` | string | 设备型号，例如 `CSK7+HI3863`、`CSK6012`；`未知`/空视为未知 |
| `device.project` | string | 项目名，例如 `tcl`、`midea-smoke`；按分隔符切词匹配台账 `projects` |
| `device.firmware` | string | 固件路径或版本，透传给烧录类步骤 |
| `bench.node` | string | 执行节点名，与知识条目 `scope.device` 对照，例如 `win009` |
| `bench.os` | string | `windows` / `linux` / `macos`；做 `platform` 门禁 |
| `bench.ports` | string[] | 台架端口；按序或按 `bench.port_roles` 映射到 skill 的端口角色 |
| `bench.port_roles` | object | 显式端口映射，例如 `{"log":"COM23","control":"COM27","flash":"COM29"}` |
| `bench.playback_key` | string | 播报声卡 Key（`VID_xxxx&PID_xxxx:...`） |
| `bench.skills_home` | string | 预留：执行节点 skill 安装根目录；实际用 CLI `--skills-home` 生效 |
| `bench.text_file` | string | 待合成文本文件（melotts 等离线合成） |
| `constraints.hours` | number | 计划时长；≥6 或 `overnight` 触发长跑模式 |
| `constraints.overnight` | boolean | 通宵；触发停止条件与巡检项 |
| `constraints.reset_allowed` | boolean | 是否允许复位；只影响复位口径与证据要求，计划本身不执行复位，也不单独排复位步骤 |

未知字段会被忽略，不做猜测性解释。

## 2. 计划 `plan.v1`

```json
{
  "schema": "plan.v1",
  "plan_id": "p-20261107-892a",
  "task": "验证 TCL 离线语音唤醒与命令词",
  "confidence": "medium",
  "needs_confirmation": ["..."],
  "request": {"...": "回显请求（归一化后）"},
  "steps": [
    {
      "n": 1,
      "skill": "usb-tree",
      "phase": "access",
      "label": "枚举 USB 拓扑与声卡端点",
      "cmd": "$HOME/.codex/skills/usb-tree/scripts/usbTree-cli.exe --all ...",
      "precheck": ["..."],
      "gate": "端口 COM3, COM4, COM6 全部出现在板卡树中……",
      "on_fail": "stop",
      "rollback": "只读枚举，无副作用；……",
      "evidence": "preflight/usb-tree.txt",
      "cmd_verified": true,
      "basis": ["registry:usb-tree.capability 命中「拓扑」", "routing:steps.usb-tree"]
    }
  ],
  "standards": ["trails：……", "skill-creator：……"],
  "runtime_notes": ["lstest：由 cskflashtest 依赖，作为运行时/共享包使用，不单独执行"],
  "excluded": [{"skill": "mideaSmoke", "reason": "projects=['CSK6012-midea-smoke'] 与任务项目 tcl 不匹配……"}],
  "reset_policy": {
    "allowed": true,
    "decision": "允许复位，但不单独排复位步骤",
    "reason": "复位属于被选中 skill 的恢复动作，调度层不执行，也不生成独立复位步骤",
    "requirement": "复位命令必须先在本台架实测有效并留下证据；未验证的命令不得当作自动恢复手段",
    "channels": [{"skill": "tcl-test-sub", "command": "uut-switch1", "verified": false, "log": "reset_log.jsonl", "note": "…"}]
  },
  "preconditions": ["..."],
  "stop_conditions": ["..."],
  "stop_rules": [
    {"id": "crash-round-cluster", "rule": "连续 3 段都在同一轮次崩溃 → 判设备不稳定并停止",
     "basis": "kb:kb-run-3228baa6c3f6", "source": "default",
     "why": "kb:kb-run-3228baa6c3f6 记录该设备有崩溃史，安装默认止损检测器"}
  ],
  "recovery": [
    {"n": "R1", "action": "algo-restart", "skill": "tcl-test-sub",
     "label": "崩溃恢复：向 COM3 发送 algo.restart 1 复位算法侧",
     "why": "CSK 重启后卡在 boot app client 创建…（kb:kb-bug-241022256518）",
     "cmd": "python -c \"import serial;s=serial.Serial('COM3',921600,timeout=1);s.write(b'algo.restart 1\\r\\n');s.close()\"",
     "port": "COM3", "port_source": "bench",
     "precheck": ["先写 STOP 并确认 runner 已退出…"],
     "gate": "命令发出后 10 秒内 COM3 重新产生字节",
     "after_fail": "连续 3 次 300 秒内快速崩溃 → 停止…；10 秒仍无字节 → 停止并要求人工物理断电",
     "verify": "串口重新开口不等于功能恢复：再跑 1 轮 smoke 通过才算恢复",
     "record": "reset_log.jsonl",
     "basis": "kb:kb-bug-241022256518", "supporting": ["kb:kb-method-5a97db5bfc22"],
     "cmd_verified": false}
  ],
  "patrol": ["..."],
  "kb": [{"id": "kb-...", "type": "knowledge", "title": "...", "claim": "...", "use": "precheck", "relevance": 8}],
  "sources": ["registry/skills.json@4af4f6d", "routing:routing.v1", "kb:kb-..."],
  "warnings": ["..."],
  "limits": ["本计划只做选型与排序：不执行设备动作、不联网、不写台账与知识库"]
}
```

### 字段约束

| 字段 | 约束 |
|---|---|
| `plan_id` | `p-YYYYMMDD-<4hex>`；4 位十六进制来自归一化请求的 SHA-256，同一请求稳定可复现 |
| `confidence` | `high` / `medium` / `low`；`low` 时必须非空 `needs_confirmation` |
| `steps[].phase` | 必须属于 `access / action / material / execute / evidence` |
| `steps[].skill` | 必须在 `registry/skills.json` 内且 `status != archived`；`entry` 为 `.md` 的规范类 skill 不得作为执行步骤 |
| `steps[].on_fail` | `stop` / `retry` / `skip` |
| `steps[].cmd` | 非空；使用 `$HOME/.codex/skills/<skill>` 作为安装根目录占位；未登记模板时为 `cmd_verified: false` |
| `steps[].basis` | 每条至少一个 `registry:` / `kb:` / `routing:` 引用，便于复核 |
| `excluded[].reason` | 非空；必须能指出台账字段或知识条目 |
| `steps[].n` | 从 1 连续编号 |
| `reset_policy` | 对象：`allowed` 布尔 + `decision` / `reason` / `requirement` + `channels`；`channels[].verified=false` 时必须进 `needs_confirmation` 并降置信度；`allowed=false` 时 `channels` 为空 |
| `stop_rules[]` | 每条 `rule` 非空、`source` ∈ `kb` / `default`、`basis` 为 `kb:<id>` 且该 id 必须在 `sources` 里；没有知识库时整个数组为空 |
| `recovery[]` | `n` 从 `R1` 连续编号；`skill` 必须在台账内且非 archived、`entry` 不得是 `.md`；`cmd` / `why` / `gate` / `after_fail` / `record` 非空；`basis` 为 `kb:<id>` 且在 `sources` 里；`port_source=default` 时必须进 `needs_confirmation`；`cmd_verified=false` 表示发送方式未在真机验证 |
| `sources` | 至少含 `registry/skills.json@<rev>` 与 `routing:<version>` |

`check` 子命令和 `tests/test_dispatch.py` 的 `ValidationGuards` / `KbActionGuards` 按上表拦截被篡改的计划。

## 3. 置信度规则

| 情况 | 结果 |
|---|---|
| 设备型号未知 | `low` + 要求提供型号 |
| 任务需要的能力在台账里没有可用 skill | `low` + 列出缺口 |
| 计划没有任何可执行步骤 | `low` |
| 选中的 skill 是 `legacy` | 最多 `medium` + 要求确认仍适用 |
| gate 端口回落到 skill 默认值，或端口来源未知 | 最多 `medium` + 要求核对台架实际端口 |
| 允许复位但复位通道 `verified=false` | 最多 `medium` + 要求先实测复位 |
| 有未填占位符（缺端口/Key/时长等） | 最多 `medium` + 逐项列出缺失输入 |
| 台架系统未知 | 最多 `medium` |
| 知识库缺失但显式 `--allow-missing-kb` | 最多 `medium` |
| 其余 | `high` |

## 4. 退出码

| 码 | 含义 |
|---|---|
| `0` | 计划产出且自检通过 |
| `2` | 请求不合法（缺 `task`、字段类型错） |
| `3` | 台账/知识库缺失、JSON 不合法、routing 与台账不一致（失败关闭） |
| `4` | 计划自检不通过，或 `--require-confidence` 未达标 |

## 5. cmd 约定

- 安装根目录固定写 `$HOME/.codex/skills/<skill>`：PowerShell 5.1 与 bash 都会展开 `$HOME`；路径含空格时模板已加双引号。
- 可执行文件（`.exe`/`.bat`）按 PowerShell 语义用 `& "路径"` 调用；Linux 下对应的 bash 入口另行使用，不把 `&` 当后台符号。
- 需要其它根目录时用 `--skills-home 'C:\Users\x\.codex\skills'` 重新生成，不要手改计划里的路径。
- `cmd` 只描述**入口与固定输入**；轮次/时长等来自请求，语义由 skill 自己的 `SKILL.md` 定义。
- 台账只登记入口，未登记调用模板的 skill 用 `cmd_verified: false` 标记，执行方必须先按该 skill 的 `SKILL.md` 补参数。

## 6. 端口与复位口径

### 端口：以请求为准，默认值必须标来源

`gate` / `precheck` 里写端口时用 `{effective_ports}`，渲染优先级固定：

1. 请求 `bench.ports`（含 `bench.port_roles`）→ 文案写 `（来源：bench.ports）`；
2. 请求没给 → `routing.port_defaults.<skill>` 的 skill 默认值 → 文案写 `（来源：default，<skill> 的 skill 默认值）`，同时进 `warnings` 与 `needs_confirmation`，置信度最多 `medium`；
3. 两者都没有 → `来源未知（请求未给 bench.ports，且未登记该 skill 的默认端口）`，进 `needs_confirmation`，置信度最多 `medium`。

请求给了端口就**不再混入** skill 默认值：gate 里出现默认端口会把执行方引到不存在的串口。回归用例见 `tests/test_dispatch.py::GatePorts`。

### 复位：允许 ≠ 自动恢复

`constraints.reset_allowed` 只表达"允许在恢复流程中复位"，不产生独立的复位步骤：

- `false`：故障即停，`reset_policy.channels` 为空，禁止任何复位/恢复动作；
- `true`：列出被选中 skill 登记的复位通道；`verified=false` 的通道（目前 `tcl-test-sub` 的 `uut-switch1`）只登记、不自动执行，并进 `needs_confirmation`、置信度最多 `medium`；
- 复位永远由被选中 skill 执行并写自己的 `reset_log`，调度层不碰设备。

"允许复位"不是"计划会帮你复位"；执行方必须先证明通道有效，再把它当作恢复手段。

## 7. 知识的第二阶段消费：`trap` → 恢复步骤 + 针对性停止条件

v1 只把 `precheck` 类条目写进 `preconditions`；`trap` / `knowledge` 只挂在 `kb` 字段里，等于"知识被存下来但没改变执行"。§7 定义第二阶段：命中映射表的条目会改变计划的动作。

映射表在 `routing.json.kb_actions`（机器可读），不在代码里写死条目名：

```json
"kb_actions": {
  "recovery": [{"id": "algo-restart", "skill": "tcl-test-sub", "match": {"types": ["bug"], "text_any": ["algo\\.restart"]},
                "command_regex": "(algo\\.restart\\s+\\d+)", "port_role": "log", "baud": 921600, "...": "模板"}],
  "stop_rules": {
    "crash_history": {"types": ["bug", "run"], "text_any": ["崩溃", "crash", "重启"]},
    "kb":      [{"id": "crash-round-cluster", "text_any": ["round\\s*\\d+"], "rule": "连续 3 段都在同一轮次崩溃 → 判设备不稳定并停止"}],
    "default": [{"id": "crash-round-cluster", "rule": "连续 3 段都在同一轮次崩溃 → 判设备不稳定并停止"}]
  }
}
```

### 7.1 生成规则

| 输出 | 生成条件 | 出处 |
|---|---|---|
| `recovery[]` | 条目命中 `match`，且模板里的 skill 本轮**已被选中**、端口能解析 | 条目 id → `basis: kb:<id>`；同命令的其他条目进 `supporting` |
| `stop_rules[]`（`source: kb`） | 条目自己说出规律：固定轮次、同一崩溃签名、恢复手段失效 | 该条目 id |
| `stop_rules[]`（`source: default`） | 条目命中 `crash_history`（证明该设备有崩溃史），装上 `default` 里的检测器 | **激活它的条目** id |

- `source` 的含义是**规则文本的来源**：`kb` = 条目自己说出的规律；`default` = routing 默认检测器，只因条目证明"这台设备会崩"才装上。两者都带 `basis: kb:<id>`，没有知识库时两张表都为空。
- 条目池 = 与本请求 **设备或项目相关**（`scope.device` / `scope.project`）且相关度 > 0 的条目，**不受 Top 5 注入上限限制**——最值钱的 trap 不能被摘要上限挤掉。被引用的条目会自动补进 `plan.kb` 与 `sources`。
- 去重按规范化后的命令 / 规则文本：两条条目说同一句，只生成一个步骤，其余进 `supporting` 或保留 `kb` 出处更高的那一条。

### 7.2 宁可不生成，也不生成空动作

以下情况**不生成** `recovery`，只留 `warnings` / `needs_confirmation`：

- 条目里没有可执行命令（`command_regex` 不命中）；
- 执行该动作的 skill 本轮没被选中；
- 端口解析不出来（请求没给 `bench.ports`，skill 也没登记默认口）；
- `constraints.reset_allowed=false`：知识里的恢复手段一律不进计划，故障即停。

`recovery[]` 只是**预案**：调度层不代发串口、不点声卡。执行方按 `precheck` → `cmd` → `gate` → `verify` 自行完成，并把结果写进 `record`（默认 `reset_log.jsonl`）；`cmd_verified=false` 表示发送方式尚未在真机验证，执行前必须按该 skill 的串口约定核对。

### 7.3 与 `stop_conditions` 的关系

`stop_conditions` 是**通用**规则（STOP 文件、串口 180s 无增长、连续崩溃 ≥3 次/30min、磁盘满…），`stop_rules` 是**针对本设备历史**的规则（同轮次连崩、同一签名预算、恢复失效）。两者并存，互不覆盖：`stop_conditions` 的条目和顺序不因知识库变化而改变（回归用例 `KbTrapBecomesActions::test_legacy_fields_are_untouched`）。
