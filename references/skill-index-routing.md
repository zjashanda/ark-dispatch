# Skill 索引路由契约（burner / lstest）

> 生效：2026-11-19（NOAH-242）。机器规则在 `scripts/routing.json` 的 `skill_index` 段，
> 索引结构在 `schema/skill-index.v1.json`，单条能力在 `schema/skill-route.v1.json`，
> 解析器是 `scripts/route.py`（只读、不执行）。

## 1. 为什么先读索引

`SKILL.md` 回答「这个 skill 能做什么、为什么有这个坑」；索引回答「这次该用哪条能力、哪个 profile、哪条命令」。
只读正文，模型只能猜脚本名、profile 名和判定阈值——猜错的代价是一次烧错固件或一轮假 PASS。

因此固定顺序，任何一步不允许跳：

```
capability → skill → chip → project → profile → workflow → preconditions → execute → evidence → ark-kb
```

## 2. 两级索引

| skill | manifest | index | 能力前缀 |
|---|---|---|---|
| `burner`（repo `zjashanda/venusahisiburner`） | `index/manifest.json` | `index/index.json` | `burn.` |
| `lstest`（repo `zjashanda/lstest`） | `index/manifest.json` | `index/index.json` | `test.` |

每条 capability 固定 14 个字段：
`capability_id`、`skill`、`module`、`chip`、`project`、`firmware_family`、`profile`、`workflow`、
`inputs`、`outputs`、`preconditions`、`evidence_schema`、`version`、`status`。

`status` 只有四种含义：

| status | 含义 | 能否执行 |
|---|---|---|
| `active` | 能力与工具齐备 | 可以（仍要过前置检查） |
| `draft` | 能力在，证据/profile 未成型 | 只允许只读/预检 |
| `gap` | 工具或证据缺失 | 禁止执行，先补缺口 |
| `blocked` | 明确不可用 | 禁止执行 |

## 3. 分流规则

| 任务 | 路由 |
|---|---|
| 写固件、换版本写入、烧后读版本、FTM 恢复 | `burn.*` |
| 测试逻辑、用例、断言、稳定性、音频、网络、证据、报告 | `test.*` |
| 烧录后验证 | `burn.*` → `test.*` |
| 烧录 + OTA + 稳定性 | `burn.*` → `test.ota-*` → `test.stability-*` |

组合只在编排层拼接：后一步把前一步的 `outputs`（尤其 `run.receipt.json`）当输入证据，
**能力不复制、回执不重写**。

## 4. 解析器用法

```bash
# 索引结构与引用闭合
python scripts/route.py check \
  --index burner=<repo>/index/index.json \
  --index lstest=<repo>/index/index.json

# 单条路由
python scripts/route.py resolve \
  --index burner=<repo>/index/index.json --index lstest=<repo>/index/index.json \
  --capability burn.tcl-dual-baseline --chip venusa --project tcl-pvm1005

# 组合路由：每一步可用 #chip=..,project=.. 覆盖全局过滤
python scripts/route.py resolve \
  --index burner=<burner>/index/index.json --index lstest=<lstest>/index/index.json \
  --capability 'burn.tcl-dual-baseline#chip=venusa,project=tcl-pvm1005' \
  --capability 'test.wake-command#chip=csk7,project=venusws63'
```

输出 `skill-route-plan.v1`，退出码：`0` 路由通过且门禁 PASS；`2` 请求错误/无匹配/歧义；
`3` 索引缺失或不合格；`4` 路由命中但门禁阻塞（见 `gate`）。

## 5. 门禁码

| gate | 触发 | 处理 |
|---|---|---|
| `BLOCKED_CAPABILITY_NOT_FOUND` | 索引里没有该能力 | 补 chip/project/profile，或先登记能力；不许猜 |
| `BLOCKED_AMBIGUOUS_ROUTE` | 命中多条 | 按 route_order 收窄；禁止自行挑一条 |
| `BLOCKED_CAPABILITY_GAP` | `status=gap` | 先补工具/证据，不改写成 active |
| `BLOCKED_CAPABILITY` | `status=blocked` | 停止 |
| `BLOCKED_UNKNOWN_PROJECT` | 项目不在 `projects[]` | 先登记项目，不许套用别的项目 profile |
| `BLOCKED_PROFILE_CONTRACT` | 项目合同是 draft/absent 且要正式执行 | 只跑 preflight/fixture；正式压测等 formal |

`BLOCKED_PROFILE_CONTRACT` 是**路由成功、执行被门禁拦住**，回执里必须分开写
`route_status=MATCHED` 与 `gate=BLOCKED_PROFILE_CONTRACT`，不得合并成一句 FAIL。

## 6. 禁止事项

- 新建 `csk6-hisi-burner` 之类按芯片/项目命名的同类顶层 skill。
- 把 burner 的烧录逻辑并进 lstest，或在 lstest 里复制 profile。
- 从历史任务目录直接复用未登记的旧工具或旧 profile。
- 让模型自由选择 skill、profile 或阈值。
- 用 pending/stale 知识驱动动作。

## 7. 与知识库的闭环

路由计划里的 `capability_id`、`profile`、`workflow`、`skill_version` 要写进回执；
`ark-kb` 条目的 `skill_ref` / `capability` / `module` / `workflow` / `profile` 反向指回同一组值。
这样「这次为什么这么跑」与「上次同类结论」可以用同一组键互相检索。
