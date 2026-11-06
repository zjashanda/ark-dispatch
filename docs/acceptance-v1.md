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
