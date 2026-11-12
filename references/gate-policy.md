# 调度侧的执行门禁映射

本文件说明 NoahsArk 执行门禁如何约束 ark-dispatch 的输出。门禁的完整规则是 NOAH-66 的
`gate-plan-integration.md`（下称「整合规则」），本文只覆盖调度这一片：`plan.v1` 里哪些字段
承载门禁语义、受门禁约束的任务必须自带什么、阻塞码怎么落到退出码。两者冲突时以整合规则为准。

## 1. 门禁状态机对调度的约束

平台只有 `issue.status`、`issue.stage`、`run.status` 三个真实状态位（整合规则 §3），调度不产生
其中任何一个。调度能做的是让产出的计划**具备被门禁判定的形状**：

- 每个 stage 的执行片段都要有可观测的通过条件，否则执行方无法写出 `STAGE_FINISHED`（§5.3）。
- 计划的第一个可执行步骤与第一件产物必须明确，执行方才能填 `EXECUTION_CHECK` 的
  `first_action` / `first_artifact`（§5.1）。
- 计划不得声明「已执行」「已通过」——调度侧没有 run，也不会有 run。

## 2. `plan.v1` 中承载门禁语义的字段

字段形状见 [plan-schema.md](plan-schema.md) §2，这里只解释它们在门禁里的角色。

| 字段 | 门禁语义 | 约束 |
|---|---|---|
| `steps[].gate` | 该步是否通过的判据，对应 `STAGE_FINISHED.gate_result` 的判定依据 | 必须可观测（串口有字节、文件存在、哈希可核），不能写「正常」「OK」这类不可复核的描述 |
| `stop_conditions` | 通用止损；触发即执行方写 `BLOCKED` 而不是硬撑 | 与 `stop_rules` 并存，互不覆盖（plan-schema.md §7.3） |
| `recovery[].gate` / `after_fail` | 恢复动作的准入与放弃条件；`recovery` 只是预案 | 调度不代发串口、不点声卡；`cmd_verified=false` 表示发送方式未真机验证 |
| `reset_policy` | `allowed=false` 时故障即停，`channels` 为空 | `verified=false` 的通道只登记，必须进 `needs_confirmation` |
| `confidence` / `needs_confirmation` | 计划是否可信到可以开工 | `low` 必须非空 `needs_confirmation`；`check` 会拦 `low` 且无确认项的计划 |
| `sources` | 钉住本次判定所依据的快照 | 至少含 `registry/skills.json@<rev>` 与 `routing:<version>`，门禁复核时按同一快照复算 |

`steps[].phase` 的顺序（`access → action → material → execute → evidence`）是阶段划分的依据，
但不等于 issue 的 `stage` 整数。两者的换算只在任务目录的 `plan.md` 与 `receipts/stage-index.json`
里出现，不写进回执正文（见 ark-archive `docs/gate-mapping.md`）。

## 3. 调度只决策，不执行

`SKILL.md` 与 `README.md` 都把边界写死了：调度只选型、排序、写门禁与回滚，不碰设备、不联网、
不写台账与知识库。这条边界在门禁语境下有三层含义：

1. 计划里的 `gate` 是**交给执行方的判据**，不是调度自己验证出的结论；调度无法把某一步标成已通过。
2. 调度不能发 `PROGRESS_5M`、`STAGE_FINISHED`、`FINAL_RECEIPT`，这些事件的产生者只有真实 run 的执行方。
3. 调度报告里的 `BLOCKED_*` 只描述「计划无法产出」或「计划不完整」，不描述设备故障。

## 4. 受门禁约束的任务：计划必须自带的两件事

### 4.1 让 `EXECUTION_CHECK` 填得出来

`EXECUTION_CHECK` 的八个字段（整合规则 §5.1）由执行方在开跑时写进任务目录的
`receipts/events/gates-<YYYYMMDD>.jsonl`，不是 `plan.v1` 的字段。调度要做的是提供其中两个可填值：

- `first_action`：`steps[0].label` 与 `steps[0].cmd` 必须能直接作为第一个动作。
- `first_artifact`：`steps[0].evidence` 必须给出相对任务目录的落盘位置。

计划缺少首步证据位置时，执行方只能编路径；编路径在门禁里是非法行为（§5.2 对 `artifacts`
的要求），因此这种计划应当直接判为 `BLOCKED_PLAN_INCOMPLETE`。

### 4.2 阶段屏障

- 每个 stage 的计划片段必须能独立回滚：产物写在各自子目录，回滚只撤该 stage 的提交与产物指针（§4）。
- 阶段屏障用 `multica issue wakeup create --until-children-done --stage N` 表达；平台只能表达
  「子 issue 全部完成」，不能表达「全部验收通过」，后者仍由人判定（§4、§8 P4）。
- 跨阶段并行可以，但每个并行分支必须收在同一个顶层 run 内，禁止 background-and-yield（§10）。

## 5. `BLOCKED_*` 与退出码

阻塞码写在 `BLOCKED` 事件与 `plan.md` 的待办块里（§5.4）。退出码是脚本层面对同一件事的缩写：

| 阻塞码 | 触发场景 | 退出码 |
|---|---|---|
| `BLOCKED_SOURCE_MISSING` | 台账、知识库、routing 或输入资产缺失，无法产出可信计划 | `3` NO_SOURCE |
| `BLOCKED_PLAN_INCOMPLETE` | 计划自检不通过：缺 `cmd` / `gate` / `rollback`、`n` 不连续、`low` 无确认项 | `4` FORBIDDEN |
| `BLOCKED_WORKDIR_POLICY` | 未在规定的任务目录执行，或无法切换到该目录 | `2` USAGE |
| `BLOCKED_APPROVAL_REQUIRED` | 需要人批准的配置、模板或发布动作尚未获批 | `4` FORBIDDEN |
| `BLOCKED_EXECUTION_CHANNEL` | 无法证明执行通道无窗口、非交互、可观测 | `1` FAILED |

退出码全表：`0` OK（计划产出且自检通过）、`1` FAILED（执行阶段失败）、`2` USAGE（用法/目录错）、
`3` NO_SOURCE（输入源缺失）、`4` FORBIDDEN（越权或自检不通过）。`scripts/dispatch.py` 现有常量
`EXIT_OK=0`、`EXIT_INPUT=2`、`EXIT_ENV=3`、`EXIT_PLAN=4` 与这张表对齐；`1` 不由调度返回，
它留给真正执行的阶段。

## 6. 本轮没有新增 `plan.v1` 字段

`plan.v1` 的字段集、`scripts/routing.json` 的步骤模板、`scripts/dispatch.py` 的 `check_plan`
校验路径（实现为 `validate_plan`，由 `check` 子命令调用）**均未改动**。原因是新增字段不是加一行文档：

- 字段一旦进入计划，就要在 `plan` 生成路径里填充，`validate_plan` 必须同时校验，`check` 的
  `OK` / `FAIL` 输出也会变化。
- 74 条既有用例里有 golden plan 与逐字段断言，新增字段会让这些固定期望全部失配；改测试等于
  改门禁本身，超出本轮「映射文档」的范围。
- 对外契约先加字段再回头补语义，会让执行方按新字段写逻辑却拿不到值，比没有字段更危险。

因此本轮只交付映射文档；门禁语义靠既有字段 + 任务目录事件流承载。

## 7. 未落地的字段提案与迁移规则

以下提案**全部未落地**，不得出现在当前生成的计划里，也不得被消费方提前依赖：

| 提案字段 | 想解决的问题 | 状态 |
|---|---|---|
| `plan.gate_policy` | 在计划内声明所属 stage、`EXECUTION_CHECK` 的必填锚点与期望屏障 | 未落地 |
| `steps[].gate_result_path` | 指定该步 gate 结果的落盘位置，免去执行方自行约定 | 未落地 |
| `steps[].receipt_layer` | 声明该步产物允许进入哪一层回执 | 未落地 |
| `plan.approval_class` | 标注本次计划涉及代码 / 配置 / 模板 / OSS 对外发布中的哪一类 | 未落地 |

迁移规则（任何一项要落地时逐条满足）：

1. 先升 `plan.v2`，同步改 `SCHEMA_PLAN`、`validate_plan`、`routing.json` 的步骤模板与全部
   golden plan；不得就地修改 `plan.v1`。
2. v1 计划保持可读，采用「双读一写」过渡：校验器同时接受 v1 与 v2，生成器只写新版本。
3. 新的必填字段必须先在执行侧有真实取值来源，否则只能做成可选字段并给出缺省语义。
4. 提案通过评审前，`references/plan-schema.md` 不得出现该字段，避免文档先于契约。
