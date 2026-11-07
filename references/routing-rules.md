# 选型规则（人读版）

机器规则在 `scripts/routing.json`，本文件解释它为什么这样写、怎么改。两者不一致时以代码与测试为准——`dispatch.py rules` 会校验规则引用的每个 skill 都在台账里。

## 1. 为什么不是关键词相似度打分

任务是自然语言，"验证 TCL 唤醒"和"给 TCL 生成语料"都能命中 `TCL`，但需要的 skill 完全不同。纯文本相似度会把设备名当能力，选出一堆跑不动的实现。

所以 v1 用四层确定性结构：

```
任务文本 → 能力信号（signal）→ 能力角色（role）→ 台账门禁 → 角色竞标 → 依赖闭包 → 排序
```

每一步都能在计划里指出依据：命中的关键词、台账字段、知识条目 id。

## 2. 任务信号（`task_signals`）

| 信号 | 关键词（节选） | 说明 |
|---|---|---|
| `port_map` | 串口、端口、COM 口、USB、设备树、拓扑 | 需要先确认端口/声卡归属 |
| `playback` | 播放、播报、放音、playback | 需要向设备放音 |
| `synth` | 语音合成、合成、语料、TTS、音频生成 | 需要新语料或重新合成 |
| `voice_test` | 唤醒、命令词、命令识别、语音识别、语音验证、离线语音、ASR、交互 | 需要跑语音验证/压测 |
| `burn` | 烧录、烧写、刷机、flash、burn | 需要写固件 |
| `record` | 录音、回采、record | 需要主机侧录音取证 |
| `evidence` | 蜂鸣、取证、证据定位、音频分析 | 需要对音频/日志做定位分析 |

隐含依赖（`signal_implies`）：

- `voice_test` → `playback` + `port_map`：验证语音必须能放音、能读串口。
- `burn` → `port_map`：烧录前必须确认日志口/控制口/烧录口。

`stress_keywords`（压测、长稳、通宵、整夜…）或 `constraints.hours ≥ 6` 或 `overnight: true` 触发长跑模式：加截止时长、停止条件与巡检项。

**扩展方式**：只加新词到对应信号，不要写同义词正则到代码里。新增信号要同时加 `roles` 与 `task_signals`，否则 `rules` 校验会失败。

## 3. 能力角色（`roles`）

每个 skill 用 **名称 + capability** 去匹配角色关键词，得到"它能承担哪些角色"。刻意不看 `hardware` 字段：串口、声卡是很多 skill 的共用硬件，用它匹配会让每个测试 skill 都自称能做设备接入。

| 角色 | 阶段 | 关键词（台账 capability 里的说法） | 典型承担者 |
|---|---|---|---|
| `port_map` | `access` | 拓扑、设备树、整理成树、端口枚举 | `usb-tree` |
| `playback` | `action` | 播报、播放、声卡端点、audio-list | `listenai-play` |
| `synth` | `material` | 语音合成、TTS、Melo、IndexTTS、语料 | `lstts`、`melotts` |
| `voice_test` | `execute` | 唤醒、命令识别、命令词、语音验证、离线语音、ASR、交互探测 | `tcl-test-sub`、`arcs-aopu-test`、`Trisolaris` |
| `burn` | `execute` | 烧录、烧写 | `mideaSmoke`、`cskflashtest`、`burn_bundle` |
| `record` | `evidence` | 录音、PCM | `omni-pickup`、`power-on-record-check` |
| `evidence` | `evidence` | 蜂鸣、定位、证据 | `beep-locator` |

阶段顺序即计划顺序：`access → action → material → execute → evidence`。

**v1 不设 `report` 角色**：台账写明 `generate-html-report` 只是写作规范、没有生成脚本；任务里出现"报告"时把它放进 `standards`，而不是伪造一条执行命令。

## 4. 台账门禁（顺序固定）

1. `status == archived` → 直接排除，理由写 `registry:<name>.status` 与 `scope_note`。
2. `platform` 不含台架 `os` → 排除。
3. 项目/型号不匹配：
   - `projects` 含"通用" → 放行；
   - 否则要求 `projects` 与请求项目按分隔符切词有包含关系，或型号出现在 `projects/scope_note/capability`；
   - `reuse: low` 还额外要求型号或项目命中（设备专用实现不允许跨设备借用）。
4. 任务信号与它的角色无交集 → 不参与竞标；若 `reuse: low`，仍写进 `excluded`，说明"这次为什么没用它"。
5. 台账里查不到 → 永不入选（`check` 与单测都会拦）。

项目匹配按 `-`/`_`/空格切词比较，避免 `tcl` 命中 `httclothes` 这类子串误判——这是 v1 实测踩到的第一个坑。

## 5. 角色竞标（同一能力只选一个）

对每个角色，候选按分数降序，取最高且尚未选中的 skill：

| 加分项 | 分值 | 依据 |
|---|---|---|
| 型号命中 | +40 | 设备专用实现优先 |
| 项目命中 | +30 | 同项目资产优先 |
| `reuse: low` | +6 | 设备专用 > 通用 |
| `reuse: medium` | +3 | — |
| 角色关键词命中数 | +2/个 | 能力描述越贴切越优先 |
| 知识库规范指定 | +12 | 例如 `kb:…` 规定"运行时合成统一走 lstts" |
| `status: legacy` | −4 | 仍可用，但置信度降为 medium 并要求确认 |

落选者写入 `excluded`，理由包含：角色名、获胜者及加分依据、本 skill 的 `scope_note` 限制。`supersedes` 明示的旧实现（如 `listenai-laid-installer` → `listenai-play`）优先给出"能力被覆盖"的理由。

这套规则让 golden case 稳定复现：

- TCL 唤醒 → `usb-tree`（端口）→ `listenai-play`（播报）→ `tcl-test-sub`（执行）；`mideaSmoke` 因只适用 CSK6012 被排除。
- CSK6012 烧录 → `mideaSmoke` 型号命中，通用 `cskflashtest`/`burn_bundle` 让位；`tcl-test-sub` 因项目 TCL-ac 排除。
- 新语料 → `lstts` 命中合成角色，并引用知识条目"统一走 lstts"。
- 通宵 → 长跑命令自动带 `--duration-minutes`，计划带停止条件与巡检项。
- 型号未知 → 设备专用 skill 全部排除，`confidence: low` 并要求确认。

## 6. 依赖闭包与排序

- `depends_skills` 里**登记了调用模板**的依赖才加为步骤（如 `listenai-play`、`lstts`）。
- `spec` 层依赖（`trails`、`skill-creator`）放进 `standards`：它们是规范约束，不是可执行步骤。
- 运行时/共享包依赖（`lstest`、`burn_bundle`）放进 `runtime_notes`，避免把"框架"误当一次执行。
- 台账里查不到、已归档或平台不符的依赖 → `warnings`，不悄悄丢掉。
- 排序：先在已选 skill 之间按 `depends_skills` 拓扑排序；同层按阶段、台账 `layer`、名称打破平局。

## 7. 知识库只读消费

- 按 `scope.device`（对照 `bench.node`/`device.model`）、`scope.project`、角色关键词与标题词重合度打分，取 Top 5。
- 默认跳过 `stale` / `superseded`；只读，不回写 `hits`。
- 条目分类：`method → oracle`（判定口径）、`bug → trap`、`run → reference`、带台架/执行通道/硬件标签的 knowledge → `precheck`。
- `precheck` 条目会以前置检查形式写进 `preconditions`；所有条目保留在 `kb` 字段（含 claim 与 id）。
- "统一走 <skill>" 这类规范会参与竞标加分，并在获胜/落选理由里引用条目 id。

## 8. 计划里的步骤模板（`steps`）

- 每个模板固定：`phase / label / cmd / precheck / gate / on_fail / rollback / evidence`。
- `when: long_run` / `when: short_run` 控制长短跑二选一，避免通宵任务里混进 20 轮短跑。
- 命令参数来自对应 skill 的 `SKILL.md`（2026-11-07 采集），不自行发明开关；台账只给入口、没登记模板的 skill 会标 `cmd_verified: false`。
- 占位符缺值时不猜：写成 `<未提供:key>`，同时把置信度压到最多 `medium` 并列入 `needs_confirmation`。
- `gate` / `precheck` 的端口用 `{effective_ports}`：请求 `bench.ports` → `routing.port_defaults` → “来源未知”，渲染结果带来源。**请求给了端口就绝不回落默认值**（NOAH-20 的回归点：gate 曾写死 `COM20/COM22`，而台架是 `COM3/COM4/COM6`）。
- `constraints.reset_allowed` 不生成独立复位步骤，只在 `reset_policy` 里声明允许/禁止与证据要求；`verified=false` 的复位通道（如 `uut-switch1`）只登记、不自动执行。口径见 [plan-schema.md](plan-schema.md) §6。

新增模板的步骤：

1. 先读该 skill 的 `SKILL.md`，抄真实入口与参数。
2. 在 `routing.json` 的 `steps` 里加模板，`cmd` 用 `{skill_dir}` 锚定安装根目录。
3. `python scripts/dispatch.py rules` 确认 skill 在台账里、阶段合法。
4. 补一条 golden case 或至少一条计划断言，再跑 `python -m unittest discover -s tests -v`。

## 9. 已知限制（v1 明确不做）

- 不做真机执行，不判断"设备到底通没通"；那属于执行方与 oracle。
- 不做自然语言理解兜底：信号词没命中就不选，宁缺勿猜。
- 不做跨任务记忆：每次计划只读当前台账与知识库快照。
- 不自动生成报告：`generate-html-report` 只是规范，进 `standards`。
- `cmd` 模板只覆盖本版本实际核对过的 skill；其余靠台账入口 + `cmd_verified: false` 提示。
