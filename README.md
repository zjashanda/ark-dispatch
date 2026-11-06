# ark-dispatch

方舟调度 v1：**只决策、不执行**的 skill。给定任务、设备与现场条件，查能力台账与知识库，输出可执行的 `plan.v1`——选哪些 skill、什么顺序、每步入口/门禁/失败退出点/回滚。

- 选型依据：`ark-kb/registry/skills.json`（能力台账）+ `ark-kb/entries/*.jsonl`（知识库）
- 不联网、不碰设备：台账与知识库都用本地快照
- 每步理由可复核：命中哪个台账字段、哪条知识条目，都写在计划里

## 快速开始

```bash
python scripts/dispatch.py plan --request examples/tcl-offline.json --out plan.json
python scripts/dispatch.py plan --request examples/tcl-offline.json --format text
python scripts/dispatch.py check --plan plan.json
python -m unittest discover -s tests -v
```

没有同级 `ark-kb` 检出时，用 `--registry` 与 `--kb` 指向本地快照；缺失时失败关闭（退出码 3）。

契约与规则：

- [SKILL.md](SKILL.md)：何时用、决策流程、硬边界
- [references/plan-schema.md](references/plan-schema.md)：请求/计划字段、置信度、退出码
- [references/routing-rules.md](references/routing-rules.md)：角色、门禁、竞标、排序与扩展方式

## 示例

| 请求 | 预期选型 |
|---|---|
| `examples/tcl-offline.json` | `usb-tree` → `listenai-play` → `tcl-test-sub`，排除 `mideaSmoke` |
| `examples/csk6012-burn.json` | `usb-tree` → `mideaSmoke`，排除 `tcl-test-sub` |
| `examples/tts-corpus.json` | 加 `lstts`（引用知识库合成规范） |
| `examples/tcl-overnight.json` | 长跑命令带截止时长、停止条件与巡检项 |
| `examples/unknown-device.json` | `confidence: low` + 要求确认型号，不硬选设备专用 skill |

代码测试通过不等于真机验证通过；真机结论由执行方按计划跑完并留下证据后判定。
