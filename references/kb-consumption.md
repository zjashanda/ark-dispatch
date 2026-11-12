# 调度侧的知识消费

本文件说明 ark-dispatch 在生成 `plan.v1` 时如何查询、留痕、刷新与过滤知识库条目。
知识同步的完整规格是 NOAH-66 的 `kb-sync-proposal.md`，本文只覆盖其中的派发侧
（提案 §1、§4）：什么时候查、查到的怎么记、什么时候重查、哪些条目允许进入计划。
字段名一律照抄提案，不另立别名；与 [gate-policy.md](gate-policy.md) 冲突时以门禁文档为准。

调度仍然「只决策、不执行」（`SKILL.md`、`README.md`）：本文的查询发生在生成计划的本地
快照上，不联网、不写知识库、不产生 run，也不代表任何知识已被验证。

## 1. 查询时机

### 1.1 派发前：生成计划之前

`scripts/dispatch.py plan` 组装 `steps[]` 之前必须完成一次知识查询，命令形如：

```bash
python3 "$ARK_KB/tools/kb.py" query --json \
  --capability "<能力信号>" --project "<项目>" \
  --device "<设备型号>" --firmware "<固件>" --skill "<skill>"
```

- 维度与提案 `ark.kb-context.v1` 的 `dims` 对齐：`capability / project / device / firmware / profile / skill`；
  `profile` 没有对应命令行开关时留空字符串，不臆造取值。
- 只有实际存在的维度才传参；`kb.py query` 的过滤是「有值才参与打分」，空值不等于「查全部」。
- 查询结果为空不是错误：记 `no_hit=true` 后照常出计划，只是没有 `kb:` 来源。

### 1.2 无派发的节点任务：首个动作之前

不经 ark-dispatch、由 Agent 直接在节点上开工的任务，同样要在**首个动作前**完成一次查询，
维度取任务自己的设备/项目/能力。这时计划不是 `plan.v1`，但 `## 知识查询` 段与
`kb_context` 的字段要求完全相同，不允许因为「没有计划」就跳过留痕。

### 1.3 两处都查时按 `id` 合并

派发侧查过、节点侧又查一次时，按命中条目的 `id` 去重合并，保留较新的 `queried_at` 与
更高的 `score`；`registry_commit` 不同则以较新的快照为准，并在 `refresh_log` 里留一条记录。
禁止同一 `id` 在 `hits[]` 里出现两次，也禁止只保留一边导致出处丢失。

## 2. 留痕

留痕有两处，内容同源、字段同名：人读的 `plan.md` 与机读的回执字段。

### 2.1 任务目录 `plan.md` 的 `## 知识查询` 段

必须写入：命中条目的 `id`、`commit`、`status`、`confidence`、`score`、`source_skill`，
查询时间 `queried_at`，以及快照的 `registry_commit`。`kb.py query --json` 的顶层键是
`entries`，落盘时映射为契约里的 `hits[]`；`returned=0` 映射为 `no_hit=true`。
逐条 `commit` 不在查询输出里，取查询时 `$ARK_KB` 仓库的 HEAD（`git -C "$ARK_KB" rev-parse HEAD`），
`hits[].commit` 记完整 40 位 sha；取不到写 `unknown`，禁止编造。`sources[]` 里的 `@<commit>`
可用其 7 位缩写，与既有 `registry/skills.json@4af4f6d` 口径一致。

示例（时间一律北京时间 `YYYY-MM-DD HH:MM:SS`，不带 `T`、`Z` 或时区偏移）：

```markdown
## 知识查询

- queried_at: 2026-11-12 16:40:00
- registry_commit: a04b61eb2fe64a4f0db151de5635d5678e6ddd51
- dims: capability=拓扑/串口; project=tcl; device=CSK7+HI3863; firmware=null; skill=usb-tree
- hits:
  - id=kb-run-3228baa6c3f6 commit=a04b61eb2fe64a4f0db151de5635d5678e6ddd51 status=active confidence=high score=7 source_skill=tcl-test-sub
- no_hit: false
```

### 2.2 回执顶层 `kb_context`（`ark.kb-context.v1`）

任务结束写 `run.receipt.json` 时，把同一份内容放进可选顶层字段 `kb_context`：

```json
{
  "schema": "ark.kb-context.v1",
  "contract_version": "1.0.0",
  "queried_at": "2026-11-12 16:40:00",
  "dims": {"capability": [], "project": "tcl", "device": "CSK7+HI3863", "firmware": null, "profile": "", "skill": "usb-tree"},
  "registry_commit": "a04b61eb2fe64a4f0db151de5635d5678e6ddd51",
  "hits": [{"id": "kb-run-3228baa6c3f6", "type": "run", "status": "active", "confidence": "high", "score": 7, "commit": "a04b61eb2fe64a4f0db151de5635d5678e6ddd51", "source_skill": "tcl-test-sub"}],
  "no_hit": false,
  "refresh_points": [],
  "refresh_log": []
}
```

`kb_context` 是**可选附加字段**，不动回执的 required 集；缺这一字段说明任务没做知识查询，
而不是「查了但没命中」。字段拼写以本节为准，不得改写为 `kbContext`、`hits_count` 之类的别名。

## 3. 刷新策略

刷新是再查一次并追加记录，不是改写历史。触发条件（任一命中即刷新）：

1. **阶段边界**：每个 `STAGE_FINISHED` 之前刷新一次，`refresh_points` 记 `{"at": "stage:<N>", "reason": "stage_boundary"}`。
2. **长任务**：每 30 分钟或每 100 轮，取先到者；`constraints.overnight=true` 或 `constraints.hours >= 6` 时必然落入这一条。
3. **快照变化**：`registry_commit` 与首次查询不同，立即刷新，`reason` 记 `registry_commit_changed`。

约束：

- 刷新结果**追加**到 `refresh_log`，每条形如
  `{"at": "2026-11-12 17:10:00", "reason": "stage:2", "registry_commit": "<sha>", "new_hit_ids": ["kb-..."]}`；
  首次查询的 `hits[]` 不覆盖，只补充新增 `id`。
- 刷新不要求同步整个知识库：只重跑 §1.1 的那条 `kb.py query`，本地快照即可。
- 刷新后新出现的条目要重新过 §5 的过滤才能进入计划；`stale` 掉落的条目只从后续步骤里移除，
  已执行步骤的证据保留。

## 4. 来源格式

`plan.v1` 的 `sources[]` 里知识条目写成 `kb:<entry_id>@<commit>`，与既有的
`registry:<skill>@<rev>`、`routing:<version>` 并列：

```json
"sources": [
  "registry/skills.json@4af4f6d",
  "routing:routing.v1",
  "kb:kb-run-3228baa6c3f6@a04b61e",
  "kb:kb-bug-241022256518@a04b61e"
]
```

- `steps[].basis[]` 用同样的 `kb:` 前缀引用条目；`recovery[].basis`、`stop_rules[].basis`
  必须是 `kb:<id>` 且该 id 在 `sources` 里（`scripts/dispatch.py` 的 `validate_plan` 会逐条核对，
  `check` 子命令据此判 `FAIL`）。
- 任何被消费的条目，必须在 `steps[].basis` 或 `sources[]` 留下出处；**禁止无出处步骤**。
  只在 `plan.kb` 摘要里出现、又没进 `basis`/`sources` 的条目，视为未被消费。
- 旧的 `kb:<id>` 短格式在 `stop_rules[].basis`、`recovery[].basis` 里仍然合法（现有用例依赖），
  新增引用统一带 `@<commit>`，便于按快照复算。

## 5. 消费过滤

### 5.1 准入条件

条目必须**同时**满足，才允许驱动计划：

| 条件 | 字段 | 说明 |
|---|---|---|
| 状态有效 | `status=active` | `pending` 也不算，待验证的知识不能当结论 |
| 证据完整 | `evidence[]` 非空且每项带 `sha256` | 缺 sha256 即 evidence-gap |
| 置信度足够 | `confidence in {high, medium}` | `low` 的用法见 §5.4 |

### 5.2 分层与禁止项

`stale` / `superseded` / candidate（`receipts/candidates/`）/ evidence-gap 一律**不得**进入
`steps[]`、`recovery[]`、`stop_rules[]`（提案 §2.4、§4.3）。它们只能出现在 `plan.kb` 的
说明性摘要或 `warnings` 里，且不得被当作动作依据。`kb.py query` 默认已排除 `stale`/`superseded`，
但 `--include-stale` 或跨节点汇总的数据仍可能带进来，过滤必须在生成侧再做一次。

### 5.3 `kb_actions` 是既有消费路径，规则不改

`scripts/routing.json` 的 `kb_actions` 把命中映射表的条目变成 `recovery[]` 与
`stop_rules[]`（见 [plan-schema.md](plan-schema.md) §7）。它是当前唯一会把知识变成动作的路径，
必须遵守与 §5.1 完全相同的过滤：只有 `active`、证据完整、`high`/`medium` 的条目才参与 `match`。
无法映射成动作的条目只留在 `plan.kb`，不生成空动作；`constraints.reset_allowed=false` 时
恢复类动作一律不生成，这条既有约束继续有效。

### 5.4 `low` 只能是 `needs_confirmation`

`confidence=low` 的条目不得直接成为步骤、恢复动作或止损规则；它只能以
`needs_confirmation` 条目的形式出现（写明条目 id 与需要确认的点），并连带把计划置信度
压到最多 `medium`。这与 `plan-schema.md` §3「`low` 必须非空 `needs_confirmation`」是同一套规则。

## 6. 回写闭环

任务结束必须补齐 `delivery` 的四个状态位（提案 §2.2、§3.1）：

- `archive_status`（`ok` 才算完成）
- `kb_ingest_status`（`recorded`，或 `skipped_no_kb` 且有 `no_kb_reason`）
- `kb_entry_ids`（本次入库的条目 id 列表）
- `kb_query_back_status`（`pass` 才算完成）

四者不全、或 `kb_query_back_status != pass`，判定 `DELIVERY_INCOMPLETE`。此时即使设备侧
结论看起来正确，下一轮 dispatch 也**不得**把它当作已验证结论复用：不进入 `sources[]`、
不生成 `recovery[]`/`stop_rules[]`，只能作为待确认信息。`delivery` 落 `receipts/delivery.json`
并在 `run.receipt.json` 里复制一份，字段名同样照抄提案。

## 7. 本轮不新增 `plan.v1` 字段

`kb_context` 与 `delivery` 只落在任务目录与回执里，**不进入 `plan.v1`**。原因是硬约束而非取舍：

- `scripts/dispatch.py` 的 `validate_plan` 校验 `sources` 必须含 `registry/skills.json@<rev>`
  与 `routing:<version>`；新增必填字段就要同时改生成路径、校验路径与 `check` 的 `OK`/`FAIL` 输出。
- `tests/test_dispatch.py` 的 74 条用例里有 golden plan 与逐字段断言，新增字段会让这些固定期望
  全部失配；改测试等于改门禁本身，超出本轮「文档」范围（与 gate-policy.md §6 同一判断）。
- 契约先加字段再补语义，会让执行方按新字段写逻辑却拿不到值，比没有字段更危险。

因此以下均属**未落地提案**，不得出现在当前生成的计划里，也不得被消费方提前依赖：

| 提案 | 想解决的问题 | 状态 |
|---|---|---|
| `plan.kb_context` | 把查询留痕直接钉进计划，供复核 | 未落地（落在任务目录 `plan.md` 与回执） |
| `steps[].kb_refs` | 逐步声明该步消费了哪些条目 | 未落地（用既有 `basis[]`） |
| `plan.delivery` | 在计划里预声明四个状态位 | 未落地（落在 `receipts/delivery.json`） |

未来若要落地，必须同步改：`scripts/dispatch.py` 的 `SCHEMA_PLAN`/`validate_plan`/生成路径、
`scripts/routing.json` 的步骤模板、`references/plan-schema.md`、`tests/test_dispatch.py` 的
golden plan 与逐字段断言、全部 `examples/*.json` 的期望输出；并按 gate-policy.md §7 的迁移规则
先升 `plan.v2`、双读一写，不得就地修改 `plan.v1`。

## 8. 平台边界

Multica Server **没有** done 硬门禁（提案 §6 的 P9/P10 仍是需求，不是现状），因此：

- 调度侧只能保证计划「具备被判定的形状」，不能声称知识已验证、也不能把回执状态位写进 issue。
- 兜底用 Issue 状态 + 条件唤醒：`multica issue wakeup create --until-property ...`（阶段屏障用
  `--until-children-done --stage N`，见 gate-policy.md §4.2），由下一次 run 复查 `delivery` 四状态位。
- 任何「后端已校验 delivery / 平台已拦 PASS」的说法都不成立；本文只描述文档与既有脚本行为。

## 9. 与其它文档的关系

| 文档 | 关系 |
|---|---|
| `kb-sync-proposal.md`（NOAH-66） | 唯一规格；本文是它的派发侧切片（§1、§4） |
| [gate-policy.md](gate-policy.md) | 门禁字段与阻塞码；本文的 `sources`/`basis` 规则与 §2 一致 |
| [plan-schema.md](plan-schema.md) | `plan.v1` 字段约束；本文不新增字段（§2、§7） |
| `scripts/routing.json` | `kb_actions` 是既有知识→动作映射，受 §5 过滤约束 |
| `tests/test_dispatch.py` | `ValidationGuards`/`KbActionGuards` 是本文过滤规则的回归测试 |
