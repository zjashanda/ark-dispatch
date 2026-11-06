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
| `constraints.reset_allowed` | boolean | 是否允许复位；只影响前置检查与回滚描述，计划本身不执行复位 |

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
  "preconditions": ["..."],
  "stop_conditions": ["..."],
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
| `sources` | 至少含 `registry/skills.json@<rev>` 与 `routing:<version>` |

`check` 子命令和 `tests/test_dispatch.py` 的 `ValidationGuards` 按上表拦截被篡改的计划。

## 3. 置信度规则

| 情况 | 结果 |
|---|---|
| 设备型号未知 | `low` + 要求提供型号 |
| 任务需要的能力在台账里没有可用 skill | `low` + 列出缺口 |
| 计划没有任何可执行步骤 | `low` |
| 选中的 skill 是 `legacy` | 最多 `medium` + 要求确认仍适用 |
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
