# 调度侧的知识消费（v2：事件驱动 · 多作用域）

本文件说明 ark-dispatch 在生成 `plan.v1` 时如何查询、刷新、叠加、过滤与留痕知识条目。
唯一规格是 NOAH-66 的 `result/kb-sync-proposal-v2.md`（下称「提案 v2」，§1–§4），
v1 同名文档只作历史参考；提案 v2 与本文冲突时以提案 v2 为准。
本文只覆盖派发侧：什么时候查、查到的怎么记、什么时候重查、哪些条目允许进入计划。
字段名一律照抄提案，不另立别名。

v2 相对 v1 只改消费侧的后半段，不改「派发前必须查询」「刷新追加不覆盖历史」
「`kb:` 来源格式」「candidate 不作为动作依据」这四条；被替换的是 v1 的单层 `scope` 假设。

调度仍然「只决策、不执行」（`SKILL.md`、`README.md`、`gate-policy.md` §3）：本文的查询发生在
生成计划的本地快照上，不联网、不写知识库、不产生 run、不产生 checkpoint，也不代表任何知识已被验证。

时间一律北京时间 `YYYY-MM-DD HH:MM:SS`，秒精度，不带时区标记。示例中的 `kb-` 条目 id 与
commit 均为格式示例，不代表真实条目。

## 1. 查询时机：事件驱动，不是只有开跑一次

### 1.1 派发前（硬性，不可跳过）

`scripts/dispatch.py plan` 组装 `steps[]` 之前必须完成一次知识查询，命令形如：

```bash
python3 "$ARK_KB/tools/kb.py" query --json \
  --capability "<能力信号>" --project "<项目>" \
  --device "<设备型号>" --firmware "<固件>" --skill "<skill>" --top 20
```

- 维度与提案 `ark.kb-context.v1` 的 `dims` 对齐：`capability / project / device / firmware / profile / skill`。
- `profile` 在 `kb.py query` 没有对应开关时留空字符串，不臆造取值。
- 只有实际存在的维度才传参；`--device` / `--project` / `--capability` 是「有值才参与打分」，空值不等于查全部。
- 查询结果为空不是错误：记 `no_hit=true` 后照常出计划，只是没有 `kb:` 来源。
- 消费用途禁止加 `--include-stale`：它会把 `stale` / `superseded` 放进结果，只能用于审计复核。

### 1.2 每个 checkpoint 之后 refresh

v2 的刷新单位是生命周期事件，不是「任务结束」。执行侧每写一个 `ark.checkpoint.v1`，
下一段计划生成前都要 refresh 一次（写入同一 `refresh_log`）：

| checkpoint `trigger` | refresh 时机 | 是否必需 | 留痕落点 |
|---|---|---|---|
| `EXECUTION_CHECK` | 预检完成、首个动作之前 | 必需（基线） | `kb_context.queried_at` / `registry_commit` |
| `STAGE_FINISHED` | 下一阶段 `plan` 生成前 | 必需 | `refresh_points: {"at": "stage:<N>", "reason": "stage_boundary"}` |
| `BLOCKED` / `FAIL` / `NEEDS_REVIEW` | 事件落盘后、重排计划前 | 必需（失败知识要参与下一轮） | `reason: "checkpoint:<trigger>"` |
| `WATCHDOG_*` | 恢复 run 开始前 | 必需 | `reason: "watchdog:<trigger>"` |
| `MANUAL_CANCEL` / `RUNTIME_CRASH` | 下一次 run 扫 WAL / 补发 outbox 后 | 必需 | `reason: "recovery_replay"` |
| `FINAL_RECEIPT` | 不需要再查 | 否（只聚合、提质量、关 outbox） | 只记 `delivery.kb_query_back_status` |

- 计划侧读 checkpoint 的 `kb_context_ref`（`queried_at` / `registry_commit` / `hit_ids`），
  不自己产生 checkpoint；`kb_context_ref` 缺失说明该 run 没留查询基线，按 §6 的不可用处理。
- 失败路径同样要 refresh：`BLOCKED` / `FAIL` 之后计划会改写 `recovery[]` 与 `stop_rules[]`，
  用旧快照等于让失败知识永远进不了下一轮（提案 v2 §1.4）。

### 1.3 长任务与快照变化

以下条件任一命中即 refresh，取先到者：

1. **阶段边界**：每个 `STAGE_FINISHED` 之前一次。
2. **长任务**：每 30 分钟或每 100 轮；`constraints.overnight=true` 或 `constraints.hours >= 6`
   必然落入这一条。`at` 记 `minute:30` / `round:100`，两者都命中记先到者。
3. **快照变化**：`registry_commit` 与首次查询不同，立即刷新，`reason` 记 `registry_commit_changed`。
4. **索引过期**：`index/generated_at` 落后于当前 `registry_commit`，按索引过期处理（§6），
   刷新后再判断是否恢复可用。

### 1.4 无派发的节点任务：首个动作之前

不经 ark-dispatch、由 Agent 直接在节点上开工的任务，同样要在**首个动作前**完成一次查询，
维度取任务自己的设备/项目/能力。这时计划不是 `plan.v1`，但 `## 知识查询` 段与
`kb_context` 的字段要求完全相同，不允许因为「没有计划」就跳过留痕。

### 1.5 可用于 `plan.v1` 的步骤级时机表

| 计划位置 | 必须用的快照 | 允许的 `scope_level` | 快照缺失时 |
|---|---|---|---|
| `preconditions[]`（precheck 类） | 派发前基线（`EXECUTION_CHECK` 前那次） | `project` / `device_family` / `platform` / `capability` / `global` | 不加前置检查，进 `warnings` |
| `steps[].basis[]` | 基线；步骤重排后用最近一次 refresh | 同上，`run` 层仅用于本 run | 保留 `registry:` / `routing:` 出处 |
| `recovery[]` | 最近一次 refresh（含 `BLOCKED` / `FAIL` 后的） | `node` / `project` / `device_family` / `platform` / `capability` / `global` | 不生成恢复步骤，不改 `reset_policy` |
| `stop_rules[]` | 最近一次 refresh | 同上；`run` 层条目不得升为全局止损 | 不生成 `source: kb` 的规则 |
| `needs_confirmation[]` | 最近一次 refresh | 任意（含 `candidate` 所在层） | 写「知识不可用」确认项（§6） |
| `sources[]` | 上述所有被引用快照的并集 | 不重要，记 entry 来源即可 | `registry:` / `routing:` 仍必须存在 |
| `plan.kb[]`（摘要） | 最近一次 refresh | 任意 | 为空数组，不报错 |

`steps[].gate` / `steps[].evidence` 不由知识决定：知识只能影响选型、风险提示与失败分支，
不能把某一步的通过条件改写成「知识说没问题」。

### 1.6 两处都查时按 `id` 合并

派发侧查过、节点侧又查一次时，按命中条目的 `id` 去重合并：保留较新的 `queried_at` 与更高的
`score`；`registry_commit` 不同则以较新的快照为准，并在 `refresh_log` 追加一条记录。
禁止同一 `id` 在 `hits[]` 里出现两次，也禁止只保留一边导致出处丢失。

## 2. 查询维度与叠加：多作用域，近者优先

### 2.1 五类维度与真实命令行

| 维度 | 含义 | `kb.py query` 表达 | 说明 |
|---|---|---|---|
| `scope` | 本 run 的隔离域 | 无独立开关，由 `run_id` + `node` 推导 | `run` 层知识只对本 run 有效 |
| `capability` | 能力信号（受控词表） | `--capability` | 与台账 `tags` 同源，命中即加分 |
| `platform` / `device` | 平台与设备型号 | `--device`（平台作为设备名的前缀维度） | 没有独立 `--platform`，不要臆造 |
| `project` | 项目 | `--project` | 项目只能加例外（§2.4） |
| `node` / `profile` | 台架节点与运行 profile | `--device`（节点名）；`profile` 无开关，留空 | 不得用 `--skill` 冒充 profile |

单次 `query` 只能表达一组维度，多作用域必须**多次查询后合并**：`run` / `node` / `project`
各查一次，再按 `device_family`→`platform`→`capability`→`global` 逐层放宽补查。

### 2.2 层级与优先级（由近到远）

`run → node/bench → project → device_family/platform → capability → global`

| 层（`scope_level`） | 例子 | 可否覆盖更远层事实 | 典型用途 |
|---|---|---|---|
| `run` | 本次 run 的临时观察 | 只覆盖本 run，不外溢 | 本轮崩溃位置、临时绕过 |
| `node` | `win009` 台架 | 可覆盖同设备的执行通道事实 | 端口、SSH 通道、占用约束 |
| `project` | `TCL` | **不可**，只能加 `exceptions` | 项目偏好、语料差异 |
| `device_family` / `platform` | `CSK7` / `windows` | 可覆盖 `capability` 与 `global` | 同芯片族/同平台的通病 |
| `capability` | `flashing`、`wake-recognition` | 可覆盖 `global` | 能力级方法 |
| `global` | 通用事实 | 被上面任一层收窄 | 默认规则 |

合并时的优先级：层越近越优先；同层 `formal` 优先于 `candidate`；同层同质量时新
`registry_commit` 优先于旧；仍相同则 `score` 高者优先。每条命中必须在合并结果里保留
`scope_level`（即提案里的 `source_level`）与 `registry_commit`，供 `sources[]` 与复核使用。

### 2.3 合并去重

1. 按 `canonical id`（条目自己的 `id`）去重，不按标题或 claim 文本去重。
2. 同一 id 命中多层时保留最近层的 `scope_level`，并记下被覆盖的远层 id 供解释，不重复计入 `hits[]`。
3. `evidence_gap` 不参与合并排序，只留在留痕里（§3.2）。
4. 合并后仍要重新过 §3 的硬门禁：近层命中不等于可消费。

### 2.4 项目条目只能加 `exceptions`

项目层不得改写更广层的事实，只能追加带 `reason` 的例外：

```json
{
  "level": "project",
  "key": "TCL",
  "derived_from": ["kb-knowledge-0e8f85cddf7a"],
  "applies_to": {"device_family": "CSK7", "platform": "windows"},
  "exceptions": [
    {"rule": "开机后等待 30 秒再发串口命令", "reason": "该项目固件首启自检较慢，实测 12 次有 9 次早发失败"}
  ]
}
```

- `exceptions[].reason` 必填；没有 reason 的例外视为无效，不得进入计划。
- 项目例外只作用于本项目查询，不得污染其它项目的合并结果（提案 v2 §4 的跨项目共享测试 2）。
- 计划里体现例外的位置只有 `preconditions[]` / `needs_confirmation[]` / `warnings`，
  绝不允许把例外写成对全局事实的否定。

### 2.5 不复制孤岛

- 共同内容只存一条 canonical 条目；项目条目用 `derived_from` / `applies_to` 引用，不复制正文。
- 至少两个项目验证通过且无项目专属字段 ⇒ 生成 `promotion candidate`（`promote_to: capability|global`），
  经证据与审核后提升；发现反例 ⇒ 降级为 `applies_to` 限定或追加 `exceptions`，**不删除历史**。
- 消费侧看到 `derived_from` 只做解释，不把它当第二条命中：来源只记 canonical 那一条（§4.2）。

### 2.6 当前实现的差距（只描述，不在本轮修改）

`scripts/dispatch.py` 的 `kb_relevance` 与 `kb_scope_linked` 目前只读 `scope.device` 与
`scope.project` 两个字段，且把 `project` 参与打分与动作池判断。这会带来两个已知偏差：

- `scope.level` / `scope_level` 未被读取，`run` / `node` / `capability` / `global` 的层级优先级
  目前由 `kb_relevance` 的分数与 `routing.json` 的映射共同近似，不是显式叠加。
- 项目条目被当作普通命中参与打分，尚未强制「只能加 `exceptions`」。

因此本轮把这套叠加规则定义为**消费侧契约**：生成计划的人必须先按 §2.2/§2.3 完成合并，
再把结果交给脚本；脚本层级的字段化在 `plan.v2` 之前不做（§7.4）。

## 3. 消费过滤（硬门禁）

### 3.1 四条准入（含未落地字段的等价判据），必须同时满足

| # | 条件 | 字段/判据 | 说明 |
|---|---|---|---|
| 1 | 质量层是 `formal` | `quality=formal` | `candidate` / `evidence_gap` 一律不满足 |
| 2 | 状态有效 | `status=active` | `pending` / `stale` / `superseded` 都不算；`pending` 是「待验证」，不是结论 |
| 3 | 适用范围匹配 | `applies_to` 与本次 `device/platform/project/capability` 相交 | 不匹配即不适用，不做「大概能套」 |
| 3′ | `applies_to` 未落地时的等价判据 | `scope.device` / `scope.project` / `scope.platform` 与请求字段相交 | 字段缺失不等于放行，仍要逐项比对 |
| 4 | 证据完整 | `evidence[]` 非空，且每项带 `sha256` | 缺 `sha256` 即 `evidence_gap`，不是「弱证据」 |

上述条件之外，`confidence in {high, medium}` 仍然有效；`confidence=low` 的处置见 §3.4。

### 3.2 三分层的处置

| 层 | 允许出现的位置 | 禁止出现的位置 | 理由 |
|---|---|---|---|
| `formal` | `preconditions[]`、`recovery[]`、`stop_rules[]`、`steps[].basis[]`、`sources[]` | —— | 证据完整且可复核 |
| `candidate` | `needs_confirmation[]`、`warnings[]`、`plan.kb[]` 摘要 | `preconditions[]`、`recovery[]`、`stop_rules[]`、`steps[].basis[]` | 未验证，只能提示不能驱动 |
| `evidence_gap` | 仅任务目录查询留痕（`kb_context.hits[]`、`plan.md` 的「知识查询」段） | `plan.v1` 的任何字段 | 无证据，连提示都不该被当成结论 |

`stale` / `superseded` 在 `load_kb` 读取阶段就被丢弃，无需二次判断；但跨节点汇总或手工
`--include-stale` 的数据仍可能带进来，生成侧必须按本表再过滤一次。

### 3.3 `plan.v1` 字段级落点表

| `plan.v1` 位置 | 可放入的条目 | 写法约束 | 校验依据 |
|---|---|---|---|
| `preconditions[]` | `formal` 且 `use=precheck` | 行尾带 `（kb:<id>）`；v2 目标为 `（kb:<id>@<commit>）` | `validate_plan` 不校验该段，靠复核 |
| `steps[].basis[]` | `formal` 且支撑该步选型 | 至少一个 `registry:` / `kb:` / `routing:` | `plan-schema.md` §2 |
| `recovery[]` | `formal` bug/method，且命中 `routing.json.kb_actions.recovery` | `basis` 必须逐字存在于 `sources` | `validate_plan` 集合匹配 |
| `stop_rules[]` | `formal` bug/run/method/knowledge；`source` 只用 `kb` / `default` | `basis` 必须逐字存在于 `sources` | `validate_plan` |
| `needs_confirmation[]` | `candidate`、`confidence=low`、不可用降级 | 必须写明条目 id 与待确认点 | `low` 时非空 |
| `warnings[]` | `candidate`、`derived_from` 去重说明、不可用原因 | 不写动作指令 | 无 |
| `plan.kb[]` | 最近一次 refresh 的摘要，最多 `--top-kb` 条 | 只描述，不改变执行 | 无 |
| `sources[]` | 所有被 `basis` / `preconditions` 引用的条目 | `kb:<id>@<commit>`，与 `registry:` / `routing:` 并列 | `validate_plan` 非空 + 引用核对 |

`candidate` 不得只因为「看起来相关」就进 `plan.kb[]` 当事实：`plan.kb[]` 是摘要，
每条都要带 `confidence` 与 `use`，且不得被下游当成已验证结论。

### 3.4 与既有置信度规则叠加

- 只要有一条 `candidate` 被写进 `needs_confirmation`，计划置信度最多 `medium`。
- `confidence=low` 的条目不得成为步骤、恢复动作或止损规则，只能进 `needs_confirmation`，
  并把计划置信度压到最多 `medium`（与 `plan-schema.md` §3 同一套规则）。
- 计划最终判定为 `low` 时，`needs_confirmation` 必须非空，否则 `scripts/dispatch.py check`
  会以退出码 4 拒绝该计划。

### 3.5 `kb_actions` 是唯一的知识→动作路径，过滤规则不变

`scripts/routing.json` 的 `kb_actions` 把命中映射表的条目变成 `recovery[]` 与 `stop_rules[]`
（见 [plan-schema.md](plan-schema.md) §7）。它是当前唯一会把知识变成动作的路径，必须遵守
与 §3.1 完全相同的过滤：只有 `formal`、`active`、适用范围匹配、证据完整的条目才参与 `match`。
无法映射成动作的条目只留在 `plan.kb`，不生成空动作；`constraints.reset_allowed=false` 时
恢复类动作一律不生成，这条既有约束继续有效。

### 3.6 生成侧的两次过滤

1. **快照过滤**：`load_kb` 已丢弃 `stale` / `superseded`，但不丢 `pending`。
2. **计划过滤**：`build_plan` 的 `pick_kb` / `kb_action_pool` 只看相关度与设备/项目关联。

`quality` 与 `evidence[].sha256` 目前不在 `scripts/dispatch.py` 的读取范围内，因此
「`formal` + 证据齐全」必须在生成计划前人工/上游工具判定，并在 `plan.md` 的「知识查询」段
逐条标注；脚本只保证已有字段的引用一致性与可追溯性。这是一处**已知差距**，不是已经落地的校验。

## 4. 来源写法

### 4.1 三种来源并列

`plan.v1` 的 `sources[]` 里知识条目写成 `kb:<entry_id>@<registry_commit>`，与既有的
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
  必须是 `kb:<id>`（可带 `@<commit>`）且该串在 `sources` 里。
- 任何被消费的条目，必须在 `steps[].basis` 或 `sources[]` 留下出处；**禁止无出处步骤**。
  只在 `plan.kb` 摘要里出现、又没进 `basis`/`sources` 的条目，视为未被消费。
- `@<commit>` 取查询时 `$ARK_KB` 的 HEAD（`git -C "$ARK_KB" rev-parse HEAD`），允许 7 位缩写；
  取不到写 `unknown`，禁止编造。`hits[].commit` 仍记完整 40 位 sha。

### 4.2 同一 canonical 条目只记一次来源

被多个项目引用的同一条目，`sources[]` 只出现一次；引用关系写进 `warnings[]`：

```json
"warnings": [
  "kb:kb-knowledge-0e8f85cddf7a 同时被项目 TCL 与 CSK7 引用（derived_from），来源只记 canonical 一次"
]
```

- `derived_from` 关系**不得**展开成多条 `sources`：那不是多个快照，只是同一事实的引用链。
- 反过来，同一 id 在不同 `registry_commit` 下被引用，属于不同快照：按被引用的那次 commit
  分别记来源，并在 `warnings` 里说明哪一个 commit 被新计划采用。

### 4.3 与校验器的一致性（当前是短格式）

`build_plan` 现在写的是 `kb:<id>` 短格式，`tests/test_dispatch.py` 的 golden 断言也按短格式核对
（`ValidationGuards` / `KbActionGuards`）。`validate_plan` 对 `basis` 与 `sources` 做的是
**集合精确匹配**，不是前缀匹配。因此：

- 同一份计划内必须统一：要么全部短格式（当前产物），要么全部 `kb:<id>@<commit>`（v2 目标）。
- 混用会让 `python3 scripts/dispatch.py check --plan <file>` 报 `不在 sources 中` 并返回退出码 4。
- 本轮是 doc-only，不切生成器，所以「统一带 `@<commit>`」仍是**目标口径**；执行方按本文写作时，
  应在任务目录的 `plan.md` 里同时登记 `registry_commit`，以便后续复算。

## 5. 失败知识的消费

### 5.1 可以做什么

- `BLOCKED` / `FAIL` / `NEEDS_REVIEW` 产生的 **`formal` bug/method** 可以生成 `recovery[]`
  与 `stop_rules[]`：现象、`attempts[]`、证据齐全即满足 §3.1。
- `outcome` 字段永远不得写成 `PASS`；失败知识的价值是「怎么失败、怎么恢复、何时停」，
  不是把失败洗成成功。计划里也不得出现「已验证通过」的措辞。

### 5.2 典型落点

| 失败知识形态 | 计划落点 | 例子 |
|---|---|---|
| 崩溃规律（固定轮次、同一签名） | `stop_rules[]`（`source: kb`） | 「连续 3 段都在同一轮次崩溃 → 停止」 |
| 恢复手段（`algo.restart` 等） | `recovery[]`（命中 `kb_actions.recovery`） | 发命令 + 10 秒内看串口字节 |
| 恢复手段失效 | `stop_rules[]`（`needs_recovery`） | 「恢复后 10 秒无新字节 → 停止并人工断电」 |
| 环境限制（通道、端口占用） | `preconditions[]` | 「CSK 口由 runner 独占，先写 STOP」 |

### 5.3 `candidate` 不得驱动 `stop_rules`

崩溃残留、watchdog 从最后事件反推的知识只有 `candidate`。把它写成止损规则会**提前终止合法任务**：
一次崩溃的巧合会被当成设备规律。因此 `candidate` 只能进 `needs_confirmation` 或 `warnings`，
要变成 `stop_rules` 必须先补齐证据升为 `formal`。

## 6. 知识不可用时的降级

### 6.1 三种情形

| 情形 | 判据 | 处理 |
|---|---|---|
| KB 未建立 | 本地无 `ark-kb` 快照，或 `entries/` 为空 | 不得静默继续，先按 §6.2 登记原因 |
| 跨节点不可见 | 本节点查不到、其它节点有（网络/挂载/权限） | 记 `kb_invisible_from_node`，不写成「知识不存在」 |
| 索引过期 | `index/generated_at` 落后于 `registry_commit` | 刷新索引后重查；仍不可用才降级 |

### 6.2 必须写明的字段与降级

计划必须在任务目录的 `plan.md`「知识查询」段写明 `kb_unavailable_reason`
（建议取值：`kb_not_bootstrapped` / `kb_invisible_from_node` / `kb_index_stale`），
回执 `kb_context` 可加同名附加字段（该 schema 的 `additionalProperties` 为 `true`，加字段不破坏契约），并同时满足：

- `confidence=low`；
- `needs_confirmation` 非空，至少一条说明「本轮无知识可用，哪些结论未经知识校验」；
- `sources[]` 仍保留 `registry/skills.json@<rev>` 与 `routing:<version>`，不得为空。

这与 `validate_plan`（`scripts/dispatch.py check` 调用的校验函数）的既有规则一致：
`confidence=low` 且 `needs_confirmation` 为空会直接判 `FAIL`。

### 6.3 现状差距

当前 `scripts/dispatch.py plan` 的行为是失败关闭：找不到 KB 时返回退出码 3；
只有显式 `--allow-missing-kb` 才降级，且只把置信度压到 `medium`、写一条 `warnings`，
既不写 `kb_unavailable_reason`，也不强制 `low`。因此 §6.2 是**目标口径**，
在生成器落地前，执行方必须在 `plan.md` 手工补齐，否则该计划不得进入评审。

## 7. 边界：只决策，不执行；本轮不改契约

### 7.1 调度只决策

- 不执行设备动作、不联网、不写台账与知识库、不产生 run / checkpoint / outbox。
- `recovery[]` 只是预案：调度给命令、出处与失败退出点，不代发串口、不点声卡。
- 计划里的 `gate` 是交给执行方的判据，不是调度自己验证出的结论。

### 7.2 不写 KB

消费侧对 KB 只读。任何「查完顺手补一条」的行为都属于执行侧，必须走
`kb.py record` / `record --checkpoint` 与 outbox，不在本文范围内。

### 7.3 `plan.v1` schema 不变

`SCHEMA_PLAN`、`validate_plan`、`scripts/routing.json` 的步骤模板、`examples/*.json` 与
74 条用例的 golden 期望都不改。本文新增的都是**描述性契约**与**未落地提案**。

### 7.4 未来字段提案（全部「未落地」）

| 提案字段 | 想解决的问题 | 状态 |
|---|---|---|
| `plan.kb_context` | 把查询留痕钉进计划供复核 | 未落地（现落在任务目录 `plan.md` 与回执） |
| `plan.kb_unavailable_reason` | 让不可用原因可机读 | 未落地（现手工写在 `plan.md`） |
| `steps[].kb_refs` | 逐步声明消费了哪些条目 | 未落地（用既有 `basis[]`） |
| `plan.delivery` | 在计划内预声明四个回流状态位 | 未落地（落在 `receipts/delivery.json`） |
| `entry.quality` | 把 `formal` / `candidate` / `evidence_gap` 变成条目必填字段 | 未落地（`entry.v1.json` 暂无该字段） |
| `entry.scope.level` / `scope_level` | 显式作用域层级，替代按 `device` / `project` 猜测 | 未落地（`scope` 只有 device/project/firmware/platform） |
| `entry.scope.derived_from` / `applies_to` / `exceptions` | 引用而不复制，项目只加例外 | 未落地（`entry.v1.json` 为 `additionalProperties: false`） |
| `checkpoint.kb_context_ref` | 让消费侧直接读到每段计划的查询基线 | 未落地（提案 v2 §1.2 的字段，尚未进 ark-kb schema） |

任何一项要落地，必须同步改：`scripts/dispatch.py` 的 `SCHEMA_PLAN` / `validate_plan` / 生成路径、
`scripts/routing.json` 的步骤模板、`references/plan-schema.md`、`tests/test_dispatch.py` 的
golden plan 与逐字段断言、全部 `examples/*.json` 的期望输出；并按 gate-policy.md §7 的迁移规则
先升 `plan.v2`、双读一写，不得就地修改 `plan.v1`。

## 8. 与其它文档的关系

| 文档 | 关系 |
|---|---|
| `result/kb-sync-proposal-v2.md`（NOAH-66） | 唯一规格；本文是它的派发侧切片（§1–§4） |
| [gate-policy.md](gate-policy.md) | 门禁字段与阻塞码；本文的 `sources` / `basis` 规则与 §2 一致 |
| [plan-schema.md](plan-schema.md) | `plan.v1` 字段约束；本文不新增字段（§7.3、§7.4） |
| `scripts/routing.json` | `kb_actions` 是既有知识→动作映射，受 §3.5 过滤约束 |
| `scripts/dispatch.py` | `plan` / `check` / `rules` 三个子命令；`validate_plan` 是本文引用一致性的执行者 |
| `tests/test_dispatch.py` | `ValidationGuards` / `KbActionGuards` / `KbActionsAreConservative` 是过滤规则的回归测试 |
