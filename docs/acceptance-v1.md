# ark-dispatch v1 验收记录

日期：2026-11-07
台账/知识库快照：`ark-kb` main `4af4f6d`（`tests/fixtures/` 固定副本）
测试命令：`python -m unittest discover -s tests -v` → **40 条全绿**

## 交付范围

| 项 | 结果 |
|---|---|
| 只决策不执行的调度 skill | `scripts/dispatch.py`（`plan` / `check` / `rules`），无网络、无设备访问 |
| 输入契约 | `dispatch.request.v1`（见 `references/plan-schema.md`） |
| 输出契约 | `plan.v1`：steps / excluded / preconditions / stop_conditions / patrol / kb / sources / confidence |
| 选型规则 | `scripts/routing.json` + `references/routing-rules.md`，7 个能力角色、8 个 skill 登记调用模板 |
| golden case | 5 组，见下表 |
| 离线 | 台账与知识库只读本地快照；知识库不回写命中计数 |

## 5 组 golden case（`--date 20261107`，同一快照）

| 请求 | plan_id | 置信度 | 选型（去重后） | 步骤数 | 排除条目 | 关键排除理由 |
|---|---|---|---|---|---|---|
| `tcl-offline.json` | `p-20261107-892a` | medium | `usb-tree` → `listenai-play` → `tcl-test-sub` | 7 | 13 | `mideaSmoke`：projects 只含 CSK6012-midea-smoke，型号 CSK7+HI3863 不命中 |
| `csk6012-burn.json` | `p-20261107-e2a3` | medium | `usb-tree` → `mideaSmoke` | 4 | 13 | `tcl-test-sub`：项目 TCL-ac 不匹配；`cskflashtest`/`burn_bundle`：同角色让位于型号命中的 mideaSmoke |
| `tts-corpus.json` | `p-20261107-6488` | medium | `usb-tree` → `listenai-play` → `lstts` → `tcl-test-sub` | 9 | 13 | `melotts` 平台不符；`lstts` 的 basis 引用 `kb:kb-knowledge-d81c3d1877cc`（统一走 lstts） |
| `tcl-overnight.json` | `p-20261107-adf3` | medium | `usb-tree` → `listenai-play` → `tcl-test-sub` | 7 | 13 | 长跑命令带 `--duration-minutes 480`；10 条停止条件、5 条巡检项 |
| `unknown-device.json` | `p-20261107-4006` | low | `usb-tree` → `listenai-play` | 3 | 14 | 设备专用 skill 全部不选；要求确认型号与执行 skill |

**置信度说明**：`tcl-test-sub` 与 `mideaSmoke` 在台账里是 `status: legacy`，因此相关计划为 `medium` 并在 `needs_confirmation` 里要求确认仍适用——这是有意的，不用"能跑"冒充"已验证"。

## 断言覆盖（`tests/test_dispatch.py`）

- 5 组 golden case：选型、顺序、阶段、排除理由、端口解析、长跑截止时长、停止条件/巡检项、低置信度不猜。
- 计划契约：`plan.v1` 必有字段、`plan_id` 稳定（同请求同 id）、sources 引用台账与 kb、kb 注入 ≤5、limits 声明不执行。
- 校验器拦截：archived skill 作步骤、台账外 skill、低置信度缺 `needs_confirmation`、排除条目无理由、只有 `.md` 的规范 skill 作执行步骤。
- 失败关闭：台账缺失 → 3；知识库缺失 → 3（显式 `--allow-missing-kb` 才降级且置信度 ≤ medium）；请求缺 task → 2；`--require-confidence high` 遇 low → 4。
- 规则完整性：routing 引用的 skill 都在台账、每个角色都有命中、每条模板的 cmd 锚定 `{skill_dir}`。
- 无网络/设备依赖：源码不得出现 requests/urllib/socket/serial/http 等引用。

## 未做与后续

- **未做真机验证**：本 skill 只产出计划。真机结论仍由 NOAH-9 等执行任务按计划跑完并留证。
- **ark-dispatch 自身尚未登记进 `ark-kb/registry/skills.json`**：它是新增的第 36 个 skill，需要单独一批台账更新（含 `layer: orchestration`、依赖 `ark-kb`），避免和已验收的 35 条混改。
- **调用模板只覆盖本版核对过的 skill**：`usb-tree` / `listenai-play` / `tcl-test-sub` / `lstts` / `mideaSmoke` / `cskflashtest` / `lstest` / `melotts`。其余 skill 走台账入口并标 `cmd_verified: false`。
- **v1 不选报告 skill**：`generate-html-report` 是写作规范、没有生成脚本，任务出现"报告"时进 `standards`。

## NOAH-20 修订（2026-11-07）

- **缺陷**：第 5 步硬件 preflight 的 gate 写死 `tcl-test-sub` 默认端口 `COM20/COM22`，而请求与真实台架是 `COM3/COM4/COM6`；执行方会去开不存在的口。
- **修法**：gate/precheck 端口改用 `{effective_ports}` 渲染：请求 `bench.ports` 优先 → `routing.port_defaults` 兜底 → 显式“来源未知”；回落默认值或来源未知都会降 `medium` 并进 `needs_confirmation`。请求给了端口时 gate 不得出现 skill 默认端口。
- **复位口径**：新增 `plan.v1.reset_policy`（见 `references/plan-schema.md` §6）。`reset_allowed=true` 不生成独立复位步骤；`uut-switch1` 标 `verified: false`，只登记并降 `medium`。
- **台账**：`tcl-test-sub` 按现场证据（win009 台架 TCL 项目仍在交付）由 `legacy` 改回 `active`，变更在 `ark-kb` 独立提交。
- **测试**：46 条全绿（新增 `GatePorts` 4 条、`ResetPolicy` 2 条）；担保对象是“计划格式与选型”，不是真机结果。

## NOAH-27 修订（2026-11-08）：知识的第二阶段消费

**动机**：NOAH-22 实测证明"回执入库 → 检索命中 → 进计划"已通，但只有 `precheck` 类改变了计划（+1 条前置检查）；`trap` / `knowledge` 只挂在 `kb` 字段，没有变成恢复步骤或针对性停止条件——知识被存下来，但没改变执行。

### 改了什么

| 项 | 内容 |
|---|---|
| `plan.v1` 扩展 | 新增 `recovery[]` 与 `stop_rules[]`；`stop_conditions` / `preconditions` / `steps` 等旧字段**逐字不变**（`KbTrapBecomesActions::test_legacy_fields_are_untouched`） |
| 映射表 | `routing.json.kb_actions`：`recovery[]`（命令 + 执行 skill + 端口角色 + 波特率 + precheck/gate/after_fail/verify/record）、`stop_rules.{crash_history,kb,default}` |
| 条条目池 | 与本请求设备/项目相关且相关度 > 0 的条目，**不受 Top 5 注入上限限制**；被引用的条目自动补进 `kb` 与 `sources` |
| 出处 | 每条 `recovery` / `stop_rules` 都带 `basis: kb:<id>`，且该 id 必须在 `sources` 里（校验器强制） |
| 校验 | `dispatch.py check` 与 `check_rules` 拦截：非 `kb:` 出处、出处不在 `sources`、`R` 编号断档、archived / `.md` skill 做恢复动作、`source` 非法、端口默认值未要求确认、映射表正则或端口角色写错 |

### `source` 的口径（避免"硬塞"）

- `source: kb`：规则文本来自条目自己说出的规律（固定轮次 / 同一崩溃签名 / 恢复手段失效）。
- `source: default`：routing 默认检测器，只因条目证明"这台设备会崩"才装上，`basis` 指向激活它的条目。
- 因此**没有知识库时 `recovery` 与 `stop_rules` 必然为空**；映射不到动作的条目只留在 `kb`，不生成空动作。

### 真实知识库回归（`examples/tcl-overnight.json`，ark-kb `f925458`）

| 输出 | 无 kb | 有 kb |
|---|---|---|
| `recovery` | `[]` | `R1`：`algo.restart 1` → `COM3@921600`，`after_fail` 两级止损，`record: reset_log.jsonl`，`basis: kb:kb-bug-241022256518`，`supporting: kb:kb-method-5a97db5bfc22` |
| `stop_rules` | `[]` | ①同一崩溃签名 ≥5 次（kb，`kb-knowledge-0f2bab9750fc`）②恢复命令 10 秒未恢复 → 断电（kb，`kb-bug-241022256518`）③连续 3 段同轮次崩溃（default，由 `kb-run-3228baa6c3f6` 激活） |
| `steps` / `stop_conditions` / `reset_policy` / `patrol` | 7 / 10 / 同 / 5 | 完全一致 |

**诚实的边界**：真实条目里没有记录"13/14 次崩在 round 19"这条轮次分布（该结论在 NOAH-19 的评论里，不在 entry 的 claim 里）。所以真实回归里的"同轮次连崩"规则是 `source: default` 的**检测器**，basis 指向证明崩溃史的 run 回执；`source: kb` 的轮次规则只在条目自己写出轮次分布时才出现（单测 `kb-bug-fixture-round19` 覆盖该分支）。没有为了"看起来生效"把评论内容伪装成条目。

### 测试

`python -m unittest discover -s tests -v` → **74 条全绿**（原 46 条 + 新增 28 条：恢复步骤生成与出处、轮次规则、去重、`reset_allowed=false`、设备不符不继承、端口回落要确认、无 kb 为空、篡改拦截、映射表自检、真实知识库回归 4 条）。

**仍未做真机验证**：`R1` 的发送方式标 `cmd_verified: false`；`algo.restart 1 → COM3@921600` 的有效性来自 NOAH-19 的实测记录，不由本 skill 签发。
