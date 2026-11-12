# TTS 共享音频库：派发计划编排（v1）

- 状态：派发侧契约（只决策，不执行）；本轮不改 `plan.v1` 字段
- 时间：2026-11-12 20:20:00
- 关联：NOAH-66；规格 `result/tts-library-proposal.md` v1.1.0 §5–§7；`asset-tools` 的
  `tts-library` 子命令；`tts-runtime` 的 `docs/library-integration.md`；`audio-runtime` 的
  `docs/tts-library-derived.md`
- 范围：说明 ark-dispatch 如何把 1006 共享 TTS 音频库的「先查库、未命中才合成、发布后回查」
  写成 `plan.v1`。本文不新增命令、不新增字段、不执行任何一步。

## 0. 一句话

计划必须**先表达查库、再表达合成**：`lookup` → 命中则 `fetch` + 校验 → 未命中才 `synth` + 质检
→ `publish` → `query-back` → 只用任务副本做派生处理。`ark-dispatch` 把这些写成步骤、门禁与
失败退出点；真正调用 `asset-tools` / `tts-runtime` / `audio-runtime` 的是执行方。

## 1. 强制顺序（不可交换）

| 序 | 动作 | 承担 skill | 为什么不能换 |
|---|---|---|---|
| 1 | `tts-library lookup`（用完整规格算键并查 1006 索引） | `asset-tools` | 没有 lookup 就合成，等于绕过复用；`tts-runtime` 不判断命中，也无法拒绝一次本不该发生的 `synth` |
| 2 | 命中：`tts-library fetch` + manifest/SHA 校验 | `asset-tools` | 命中判定属于库侧；调用方只接受返回值，不自行推断 |
| 3 | 未命中：`tts-runtime synth` + `inspect` 质检 | `tts-runtime` | 只有 miss 才允许合成；质检未过不得进入 publish |
| 4 | `tts-library publish`（暂存→锁→compare-and-create→原子 rename→query-back） | `asset-tools` | 库根只允许 `asset-tools` 写；合成仓库不发布、不建库 |
| 5 | `query-back`：回读 manifest + 音频 + 重算 SHA | `asset-tools` | 回执自证不算数，必须独立复核一次 |
| 6 | 派生处理（重采样/增益/降噪/裁剪/转播放格式） | `audio-runtime` | 只能作用于任务副本；库原件发布后不可变 |

```text
lookup ──hit──▶ fetch + 校验 ──▶ 任务副本 assets/tts/original/<asset_id>/
   │
   └─miss─▶ synth ─▶ inspect(质检) ─▶ 暂存 ─▶ publish ─▶ query-back ─▶ 任务副本
                                                        │
                                                  派生处理（只动副本）
```

顺序不可交换的三条硬理由：

1. `synth` 之前没有 `lookup`，直接违反「先查库、miss 才合成」；会制造重复 canonical 资产。
2. `publish` 之前没有质检，会把静音/过短/不可读文件写进公共库。
3. `query-back` 之前声称「库已更新」，等于用发布回执替代可复核事实；调度与执行方都不允许。

## 2. 用既有 `plan.v1` 字段表达（不新增字段）

### 2.1 每个字段承载什么

| 字段 | 在 TTS 库流程里的作用 | 写法要求 |
|---|---|---|
| `steps[].n` | 执行顺序；分支步骤也占用连续编号 | 从 1 连续；不因「分支」跳号 |
| `steps[].skill` | 该步由谁执行：`asset-tools` / `tts-runtime` / `audio-runtime` | 必须在台账内且非 `archived`；不得写成引擎名 `lstts` 当 skill |
| `steps[].phase` | 阶段：库流程整体归 `material`（素材供给） | 只能是 `access/action/material/execute/evidence`；顺序由 `n` 决定 |
| `steps[].label` | 一句话说明这一步做什么，供 `EXECUTION_CHECK.first_action` 取值 | 首步必须能直接当第一个动作 |
| `steps[].cmd` | 入口命令与固定输入（路径一律落在任务目录内） | 使用 `$HOME/.codex/skills/<skill>` 作安装根；未登记模板时标 `cmd_verified: false` |
| `steps[].precheck` | 执行前必须成立的本地条件（任务目录、规格完整、字符编码） | 不写「检查一下」这类不可判定描述 |
| `steps[].gate` | 该步是否通过、以及**本步是否适用**的判据（分支全靠它表达） | 必须可观测：退出码、JSON 字段、实测 SHA、文件存在与只读位 |
| `steps[].on_fail` | `stop` / `retry` / `skip`；分支步骤用 `skip` 让另一分支接手 | 只允许这三个值 |
| `steps[].rollback` | 失败时撤哪些任务目录产物；**不撤库资产** | 库内资产不删除、不改写；只撤任务副本与暂存 |
| `steps[].evidence` | 该步产物的相对落盘位置 | 首步必须给出，供 `first_artifact` 取值 |
| `steps[].cmd_verified` | 发送方式是否在真机/真库验证过 | 本轮这些 skill 均未登记调用模板，一律 `false` |
| `steps[].basis` | 为什么排这一步；至少一个 `registry:` / `kb:` / `routing:` 引用 | 不得无出处 |
| `stop_conditions` | 通用止损：库不可达、query-back 失败、连续质检失败 | 与 `stop_rules` 并存，互不覆盖 |
| `stop_rules` | 只装**本设备历史**的止损规律；没有知识库时必须为空 | `basis` 必须是 `kb:<id>` 且在 `sources` 里 |
| `recovery` | 知识驱动的恢复预案（`routing.json.kb_actions` 命中才有） | 不得用它承载纯运维动作（见 §4.2） |
| `sources` | 钉住本次判定所用快照 | 至少 `registry/skills.json@<rev>` 与 `routing:<version>` |

### 2.2 完整示例片段：5 个逻辑步骤 / 6 条 `steps[]`

「取件或合成」是一个逻辑步骤，但 `plan.v1` 没有条件分支字段，只能用**两条互斥 gate 的步骤**
表达（`n=2` 取件、`n=3` 合成），所以 `steps[]` 是 6 条。示例假定任务根为 `${TASK_DIR}`。

```json
{
  "schema": "plan.v1",
  "plan_id": "p-20261112-6a1f",
  "task": "为 TCL 新命令词取用或合成 1006 共享 TTS 音频",
  "confidence": "medium",
  "needs_confirmation": [
    "asset-tools / tts-runtime / audio-runtime 在 routing.json 未登记步骤模板，cmd_verified=false，执行前须按各自 SKILL.md 核对子命令与参数",
    "规格字段 model / model_version / rate / pitch / style / emotion / speed / normalization_profile 必须由任务显式给出，缺任一项按未命中处理，不得用默认值补齐"
  ],
  "steps": [
    {
      "n": 1,
      "skill": "asset-tools",
      "phase": "material",
      "label": "用完整规格查 1006 TTS 库（lookup）",
      "cmd": "python \"$HOME/.codex/skills/asset-tools/asset_tools/cli.py\" tts-library lookup --spec \"$TASK_DIR/work/tts/spec.json\" --project tcl --node win009 --json",
      "precheck": [
        "spec.json 的字段集完整：normalized_text/language/voice/engine/model/model_version/synthesis_params/output/normalization_profile 均显式给出，缺省键写 null",
        "TASK_DIR 为本任务目录；库根只读，不向库根写入任何文件"
      ],
      "gate": "lookup 退出码 0，且返回 JSON 可判定：unreachable=false；命中时 asset.cache_key 与本次规格逐字相等、asset.audio.sha256 与库内文件实测一致、asset.status=active、asset.quality=formal、asset.shareable=true、ACL 命中本次 project/node",
      "on_fail": "stop",
      "rollback": "只读查询，无副作用；保留 lookup.json 作为后续判定证据",
      "evidence": "logs/tts-library/lookup.json",
      "cmd_verified": false,
      "basis": ["registry:asset-tools.capability", "routing:routing.v1（steps 未登记 asset-tools）"]
    },
    {
      "n": 2,
      "skill": "asset-tools",
      "phase": "material",
      "label": "命中分支：取件到任务目录并校验",
      "cmd": "python \"$HOME/.codex/skills/asset-tools/asset_tools/cli.py\" tts-library fetch tts-0123456789abcdef \"$TASK_DIR/assets/tts/original/tts-0123456789abcdef\" --project tcl --node win009 --json",
      "precheck": [
        "lookup.json 已落盘且可判定；本次只在 hit=true 且 usable=true 时执行",
        "目标目录为空或不存于 assets/tts/original/ 之外；禁止硬链接、符号链接与共享可写挂载"
      ],
      "gate": "hit=true 且 usable=true 时：目标目录存在 <audio.sha256>.wav，字节数等于 manifest.audio.bytes，实测 sha256 等于 manifest.audio.sha256，文件置为只读；hit=false 时本步记为不适用并跳过，证据里必须保留 lookup.hit=false",
      "on_fail": "skip",
      "rollback": "删除本步在 assets/tts/original/ 下新建的目录即可；不触碰库内资产",
      "evidence": "assets/tts/original/tts-0123456789abcdef/",
      "cmd_verified": false,
      "basis": ["registry:asset-tools.capability", "routing:routing.v1（steps 未登记 asset-tools）"]
    },
    {
      "n": 3,
      "skill": "tts-runtime",
      "phase": "material",
      "label": "未命中分支：合成并质检（miss 才执行）",
      "cmd": "python \"$HOME/.codex/skills/tts-runtime/tts_runtime/cli.py\" synth --text-file \"$TASK_DIR/work/tts/text.txt\" --engine lstts --voice 付小春 --out \"$TASK_DIR/work/tts/tts-0123456789abcdef.wav\" --json && python \"$HOME/.codex/skills/tts-runtime/tts_runtime/cli.py\" inspect \"$TASK_DIR/work/tts/tts-0123456789abcdef.wav\" --json",
      "precheck": [
        "lookup.hit=false，或命中被判为无效（stale/superseded/损坏/ACL 拒绝），且判定理由已写进证据",
        "规格中的 voice/engine 与将写入 manifest 的值一致；token 只从环境变量读取，不落命令行与日志"
      ],
      "gate": "hit=true 时本步记为不适用并跳过；miss 时 synth 退出 0 且 inspect 无 fatal：产物可读、非静音、时长 ≥ 1.0 秒、采样率/声道/位深与规格 output 完全一致，输出路径在任务目录内",
      "on_fail": "stop",
      "rollback": "删除 work/tts/ 下本次合成的 wav 与暂存目录；不发布、不写库根",
      "evidence": "work/tts/",
      "cmd_verified": false,
      "basis": ["registry:tts-runtime.capability", "routing:routing.v1（steps 未登记 tts-runtime）"]
    },
    {
      "n": 4,
      "skill": "asset-tools",
      "phase": "material",
      "label": "发布到 1006 共享库（暂存→锁→原子 rename→query-back）",
      "cmd": "python \"$HOME/.codex/skills/asset-tools/asset_tools/cli.py\" tts-library publish \"$TASK_DIR/work/tts/staging\" --task-dir \"$TASK_DIR\" --project tcl --node win009 --json",
      "precheck": [
        "暂存目录含 manifest.json（ark.tts-asset.v1）与 <audio.sha256>.wav；quality=formal、status=active、shareable=true",
        "合成/质检已通过；敏感文本不得发布（shareable=false 一律停）"
      ],
      "gate": "publish 退出码 0：created=true，或 created=false 且返回 audio_sha256 与既有资产相同（幂等成功）；返回 query_back=pass；index 中该 asset_id 只出现一条 canonical 记录",
      "on_fail": "stop",
      "rollback": "不删除库资产；失败时保留 outbox 记录（pending）与暂存目录，按 §4 重放",
      "evidence": "receipts/tts-library/publish.json",
      "cmd_verified": false,
      "basis": ["registry:asset-tools.capability", "routing:routing.v1（steps 未登记 asset-tools）"]
    },
    {
      "n": 5,
      "skill": "asset-tools",
      "phase": "material",
      "label": "独立 query-back：回读 manifest 与音频并重算摘要",
      "cmd": "python \"$HOME/.codex/skills/asset-tools/asset_tools/cli.py\" tts-library verify tts-0123456789abcdef --json",
      "precheck": ["publish.json 已落盘；本步不复用 publish 的自证结果，独立重算"],
      "gate": "回读的 manifest.cache_key 与 lookup 时的 cache_key 逐字相等，manifest.audio.sha256 等于文件实测 sha256，status=active 且 index 条目存在；任一项不等即判 query-back 失败",
      "on_fail": "stop",
      "rollback": "不改库；失败时写 outbox（pending）并在收尾前保持「库未确认更新」的措辞",
      "evidence": "receipts/tts-library/query-back.json",
      "cmd_verified": false,
      "basis": ["registry:asset-tools.capability", "routing:routing.v1（steps 未登记 asset-tools）"]
    },
    {
      "n": 6,
      "skill": "audio-runtime",
      "phase": "material",
      "label": "在任务副本上做派生处理（重采样/归一化）",
      "cmd": "python \"$HOME/.codex/skills/audio-runtime/audio_runtime/cli.py\" normalize \"$TASK_DIR/work/audio/derived/tts-0123456789abcdef/input.wav\" \"$TASK_DIR/work/audio/derived/tts-0123456789abcdef/bench.wav\" --format json",
      "precheck": [
        "输入是可写的任务副本（assets/tts/original 只读，先复制到 work/audio/derived/）",
        "derived.json 待填字段已知：derived_from_asset_id / upstream_audio_sha256 / processing_chain / output.sha256"
      ],
      "gate": "normalize 退出码 0；派生文件只落在 work/audio/ 或 result/audio/；原件实测 sha256 与本步前一致（未变）；derived.json 四个必填来源字段齐全，output.sha256 为实测值而非回执推断",
      "on_fail": "stop",
      "rollback": "删除 work/audio/derived/ 下本次产物；库原件与 assets/tts/original 不动",
      "evidence": "work/audio/derived/tts-0123456789abcdef/derived.json",
      "cmd_verified": false,
      "basis": ["registry:audio-runtime.capability", "routing:routing.v1（steps 未登记 audio-runtime）"]
    }
  ],
  "stop_conditions": [
    "lookup 退出码 3（库不可达）：不继续判定命中/未命中，按 §4 走 outbox 重放",
    "query-back 失败或 publish 报 query_back=fail：停止发布链路，不得声称公共库已更新",
    "合成/质检连续失败 ≥3 次：停止该条任务，不静默降级到其他引擎或 mock"
  ],
  "stop_rules": [],
  "recovery": [],
  "sources": ["registry/skills.json@4af4f6d", "routing:routing.v1"]
}
```

示例里 `stop_rules` 与 `recovery` 为空是**有意为之**：两者都只能由知识库条目驱动，
没有命中 `kb_actions` 的条目就不生成空动作（见 [plan-schema.md](plan-schema.md) §7.2）。

### 2.3 每条 `gate` 的判定表达式

| 步 | 分支 | `gate` 表达式（可观测判据） | 观测点 |
|---|---|---|---|
| `n=1` lookup | 共用 | `exit=0 && unreachable==false && (hit==false \|\| (asset.cache_key==spec.cache_key && asset.audio.sha256==实测 && status==active && quality==formal && shareable==true && acl_ok))` | `logs/tts-library/lookup.json` |
| `n=2` fetch | 命中 | `hit==true ? (file_exists && bytes==manifest.audio.bytes && sha256(file)==manifest.audio.sha256 && readonly) : 不适用(记录 hit=false)` | `assets/tts/original/<asset_id>/` |
| `n=3` synth+QC | 未命中 | `hit==false ? (synth.exit==0 && inspect.fatal==[] && duration>=1.0 && format==spec.output) : 不适用` | `work/tts/` |
| `n=4` publish | 共用 | `exit==0 && query_back=="pass" && (created==true \|\| (created==false && audio_sha256==existing_sha)) && canonical_count(asset_id)==1` | `receipts/tts-library/publish.json` |
| `n=5` query-back | 共用 | `manifest.cache_key==lookup.cache_key && sha256(audio)==manifest.audio.sha256 && status=="active" && index_has(asset_id)` | `receipts/tts-library/query-back.json` |
| `n=6` derived | 共用 | `exit==0 && sha256(original)==before && derived.json.has(derived_from_asset_id,upstream_audio_sha256,processing_chain,output.sha256)` | `work/audio/derived/<asset_id>/derived.json` |

`n=2` / `n=3` 的「不适用」分支是 `plan.v1` 的已知表达缺口（§7）：schema 没有 `when` 字段，
只能用 `gate` 文字 + `on_fail: skip` 表达互斥。`validate_plan` 只能校验 `gate` 非空，
**不能**校验两条分支确实互斥，所以执行方必须在证据里写下 `lookup.hit` 的真值。

`cmd_verified=false` 不代表命令一定错，而是「本仓库没有登记这些 skill 的调用模板，执行前必须按
各自的 `SKILL.md` / 库侧文档核对子命令与参数」；`tts-library` 子命令当前仍在 `asset-tools`
落地中，参数以该仓 `docs/tts-library.md` 为准。

## 3. 命中判定：什么算有效命中

### 3.1 六个条件必须同时满足

| # | 条件 | 判据来源 |
|---|---|---|
| 1 | 质量层是 `formal` | `asset.quality` |
| 2 | 状态有效 | `asset.status == "active"` |
| 3 | 可共享 | `asset.shareable == true` |
| 4 | 完整 cache key 相等 | `asset.cache_key` 与本次规格算出的键**逐字**相等（不是前缀、不是相似） |
| 5 | SHA 校验通过 | `asset.audio.sha256` 与库内文件实测一致，且字节数一致 |
| 6 | ACL 允许 | `scope.visibility` 与本次 `project` / `node` 命中 |

任一条件不满足即**不是命中**。条件 4 尤其不能用「文本相同」替代：音色、模型、采样率任一不同
都会得到不同键，误判会导致复用错音频。

### 3.2 `candidate` 只是候选，必须人工确认

- `match=candidate`（按 `normalized_text` / `voice.speaker` / `language` 的相似列表）**不能自动替代**
  精确规格命中。
- 只有执行方带 `--allow-candidate --confirm` 才能把它作为任务输入；计划侧必须把「使用 candidate」
  写进 `needs_confirmation`，并注明 `asset_id` 与待确认点，不得写成动作依据。
- `quality=candidate` / `evidence_gap` 的资产默认拒绝；`evidence_gap` 连提示都不该当成结论。

### 3.3 `stale` / `superseded` / 损坏 = 未命中

| 情形 | 判定 | 计划侧动作 |
|---|---|---|
| `status=stale` | 不可自动复用 | 按 miss 处理，走合成分支；`--include-stale` 只用于人工核查，不得进任务输入 |
| `status=superseded` | 已被新版本替代 | 按 miss 处理；需要旧版本必须人工批准并在 `needs_confirmation` 留痕 |
| SHA 不符 / 不可读 / manifest 非法 | 视为损坏 | 拒绝 + 隔离记录；**不得**用它冒充命中；重新合成后可再发布 |
| manifest `cache_key` 与规格不一致 | 不是同一资产 | 按 miss 处理，不猜测映射关系 |

### 3.4 lookup 返回值的三种形态

| `lookup` 结果 | 语义 | 计划如何继续 |
|---|---|---|
| `hit=true, usable=true` | 有效命中 | `n=2` fetch，`n=3` 跳过 |
| `hit=false`（`match` 为 `none`/`rejected`/`candidate`） | 明确未命中 | `n=3` 合成，`n=2` 跳过 |
| `unreachable=true` + `exit_code=3` | **库状态未知**，不是「没有」 | 见 §4，禁止把它写成 miss |

## 4. 库不可达、outbox 与重放

### 4.1 退出码 3 的含义被严格限定

`asset-tools tts-library lookup` 在库不可达时返回 `unreachable=true` 与退出码 3。计划与执行方
必须遵守：

- **不得**输出「库里没有该资产」「未命中」「不存在」这类结论——库没被读到，事实未知。
- 计划里的措辞只能是「库不可达，命中状态未知」，`stop_conditions` 里单列该条。
- 任务可以继续用本地合成产物，但发布请求必须写 outbox（`status=pending`），恢复后再重放。
- 收尾回执只能写「本地产物可用、公共库未确认更新」，不得写「已发布」。

### 4.2 outbox 重放：先当成条件步骤，不塞进 `recovery[]`

`plan.v1` 的 `recovery[]` 被校验器限定为 `basis: kb:<id>`（`validate_plan` 逐条检查），纯运维动作
（重放 outbox）没有知识条目出处，**不能**合法地放进 `recovery[]`。当前唯一合规写法是把它作为
一条条件 `steps[]`（阶段仍归 `material`，`on_fail` 用 `retry`）：

```json
{
  "n": 7,
  "skill": "asset-tools",
  "phase": "material",
  "label": "库恢复后重放 outbox（幂等，不产生第二个 canonical 资产）",
  "cmd": "python \"$HOME/.codex/skills/asset-tools/asset_tools/cli.py\" tts-library publish \"$TASK_DIR/work/tts/staging\" --task-dir \"$TASK_DIR\" --project tcl --node win009 --json",
  "precheck": [
    "lookup 已恢复可达；outbox 中存在 status=pending 且 idempotency_key 与本次 staging 一致",
    "重放前先按同一 idempotency_key 清理 _incoming 残留，再重试"
  ],
  "gate": "重放退出码 0，created=false 且 audio_sha256 与首次请求相同（幂等成功），index 里该 asset_id 仍只有一条 canonical 记录，attempts 递增",
  "on_fail": "retry",
  "rollback": "不改库；重放失败保持 pending，下次 run 继续",
  "evidence": "receipts/tts-library/outbox-replay.json",
  "cmd_verified": false,
  "basis": ["registry:asset-tools.capability", "routing:routing.v1（steps 未登记 asset-tools）"]
}
```

若 `ark-kb` 将来出现一条 `formal` 的 outbox 重放方法条目，且 `routing.json.kb_actions.recovery`
能把它映射成动作，则可以改用 `recovery[]` 表达；在那之前不得伪造 `kb:` 出处。

### 4.3 重放的幂等约束

- `idempotency_key = sha256(asset_id + "|" + audio.sha256)`；同一键重放**不得**产生第二个资产目录。
- 目标目录已存在且 SHA 相同 ⇒ 幂等成功（`created=false`），不写第二个副本。
- 目录存在但 SHA 不同 ⇒ 才能发布 `<asset_id>-v2`，并由 `supersedes` 指向旧资产；旧目录保留。
- 上传中断遗留的 `_incoming` 不参与 lookup；重放时同一幂等键先清理再重试。
- 并发同 key 发布：锁目录 `locks/<asset_id>.lock`，持锁超时视为陈旧锁回收并留痕；结果必须
  恰好一个 canonical 目录，其余幂等成功。

### 4.4 `BLOCKED_*` 与派发退出码

| 阻塞码 | 触发场景 | 派发退出码 |
|---|---|---|
| `BLOCKED_TTS_LIBRARY_UNREACHABLE` | 库不可达且无法判定命中状态（`lookup` 退出 3） | `3` NO_SOURCE |
| `BLOCKED_APPROVAL_REQUIRED` | 需要人批准：使用 candidate、发布敏感文本降级、删除/GC 库资产、`--include-stale` | `4` FORBIDDEN |
| `BLOCKED_SOURCE_MISSING` | 规格字段缺失、暂存缺 manifest、任务目录不存在 | `3` NO_SOURCE |
| `BLOCKED_PLAN_INCOMPLETE` | 计划缺 `cmd`/`gate`/`rollback`，或分支 gate 不可判定 | `4` FORBIDDEN |

`BLOCKED_TTS_LIBRARY_UNREACHABLE` 是 [gate-policy.md](gate-policy.md) §5 中 `BLOCKED_SOURCE_MISSING`
在 TTS 库场景下的限定写法，**不新增退出码**；退出码全集仍是 `0/1/2/3/4`，`1` 留给真正执行的阶段。
本表不改变 `scripts/dispatch.py` 的 `EXIT_OK/EXIT_INPUT/EXIT_ENV/EXIT_PLAN` 常量。

## 5. 失败与恢复矩阵

| 失败点 | 现象 | 计划里的处置 | 禁止动作 |
|---|---|---|---|
| lookup 退出 3 | 库不可达 | 标 `BLOCKED_TTS_LIBRARY_UNREACHABLE`；本地合成可用；outbox pending | 声称「库中没有」；把不可达写成 miss |
| lookup 返回损坏 | SHA 不符/不可读 | 按 miss 处理 + 隔离记录 | 用损坏文件冒充命中 |
| fetch 失败 | 复制中断/目标不可写 | `on_fail: skip` 转合成分支（若规格仍未变），并留 fetch 证据 | 直接读库内裸路径当输入 |
| synth 退出 1/2/3/4 | 参数错 / 合成失败 / 引擎缺失 / 后端故障 | 参数错不重试；后端故障按上层策略重试或停 | 回落到 `mock` 冒充成功；把后端故障写成「库里没有」 |
| inspect 有 fatal | 静音/过短/不可读 | 不发布；需要降级为 candidate 必须 `BLOCKED_APPROVAL_REQUIRED` | 跳过质检直接 publish |
| publish 锁冲突 | 并发同 key | 重放同一幂等键，得到 `created=false` | 换新键制造第二份资产 |
| publish query_back=fail | 回读不一致 | outbox pending + 退出 1（执行侧） | 声称已更新；手工改 manifest |
| verify 失败 | 资产被改/损坏 | 隔离 + 重新合成后发布新版本 | 原地修复库资产 |

## 6. 调度不得做的事（硬边界）

1. **不执行任何命令**：计划里所有 `cmd` 都是交给执行方的入口，调度脚本不调用 `asset-tools`、
   `tts-runtime`、`audio-runtime`，也不联网。
2. **不计算 cache key**：键由 `asset-tools` 按完整规格计算；调度只能把规格字段写全，不能自己
   算一个键写进计划，也不能用文本哈希冒充。
3. **不持有凭据**：token、1006 上传凭据只存在于执行侧环境变量；计划、日志、证据里不得出现。
4. **不写库**：不发布、不校验修复、不删除、不 GC、不改索引；库根只允许 `asset-tools` 写。
5. **不提前宣布结果**：`query-back` 通过之前，计划与任何回执都不得写「库已更新」「已发布」。
6. **不把库里没有当结论**：`lookup` 不可达时只能说「状态未知」。
7. **不代替执行方做质检判定**：`inspect` 的 fatal 由执行方读取；计划只写判据。
8. **不生成空动作**：没有知识条目就不生成 `recovery`/`stop_rules`；分支不成立的步骤要显式记
   「不适用」，而不是编一个假的通过结果。

## 7. 本轮不改 `plan.v1`

`SCHEMA_PLAN`、`validate_plan`（`scripts/dispatch.py`）、`scripts/routing.json` 的步骤模板、
`examples/*.json` 与 74 条用例的 golden 期望**均未改动**。本文只描述如何用既有字段表达库流程。

以下提案**全部未落地**，不得出现在当前生成的计划里，也不得被消费方提前依赖：

| 提案字段 | 想解决的问题 | 状态 |
|---|---|---|
| `steps[].when` / `steps[].branch` | 让 hit/miss 分支成为可校验的条件，而不是 gate 文字加 `skip` | 未落地（只能用 `gate` + `on_fail`） |
| `steps[].library_ref` | 把 `asset_id` / `cache_key` / `audio.sha256` / `idempotency_key` 变成结构化字段 | 未落地（现在只能写在 `cmd`/`gate`/`evidence` 文本里） |
| `plan.library_state` | 钉住本次 lookup 时的库索引版本与 `unreachable` 状态 | 未落地（现在写进 `logs/tts-library/lookup.json` 与 `sources` 文本） |
| `recovery[].source` | 允许非 `kb` 来源的运维恢复（outbox 重放） | 未落地（现在只能做成条件 `steps[]`） |
| `steps[].quality_gate` | 把 `formal` / `candidate` 的质量判定结构化 | 未落地（现在写在 `precheck`/`gate`） |

任何一项要落地，按 [gate-policy.md](gate-policy.md) §7 的迁移规则执行：先升 `plan.v2`，同步改
`SCHEMA_PLAN`、`validate_plan`、`routing.json` 模板与全部 golden plan，v1 计划保持可读，
双读一写过渡；不得就地修改 `plan.v1`。

## 8. 与 `routing.json` / 台账的差距（本轮只记录，不改）

本轮**没有**向 `scripts/routing.json` 增加任何条目。原因是现有规则形状不支持这条流程，硬加会
让 74 条 golden 用例的选型期望失配。实测差距如下：

| # | 差距 | 实测证据 | 影响 |
|---|---|---|---|
| 1 | `asset-tools` 没有对应角色 | 它的 capability 只命中 `burn` 的「烧录」；任何任务里都竞争不过型号命中的 `mideaSmoke`，从未进入 `steps[]` | lookup / fetch / publish / query-back **无法由当前选型生成** |
| 2 | `tts-runtime` 在 `synth` 竞标中输给 `lstts` | `dispatch.py plan --request examples/tts-corpus.json` 把 `tts-runtime` 写进 `excluded`，理由是「统一走 lstts」 | 合成分支的 skill 与 NOAH-66 v1.1「合成入口是 `tts-runtime`、`lstts` 只是引擎名」不一致 |
| 3 | `audio-runtime` 在 `playback` 竞标中输给 `listenai-play` | 同上，`audio-runtime` 出现在 `excluded` | 派生处理步骤无法由当前选型生成 |
| 4 | 三个 skill 都没有步骤模板 | `routing.json.steps` 只有 `usb-tree/listenai-play/lstts/tcl-test-sub/mideaSmoke/cskflashtest/lstest/melotts` | 只能标 `cmd_verified: false`，不能渲染 `{skill_dir}` 模板 |
| 5 | 台账里 `lstts` 仍是一条 active skill | `ark-kb/registry/skills.json` 有 `lstts`（entry `scripts/synthesize.py`） | 与规格「已登记清单里没有 lstts 这个 skill 名」的表述冲突，需要下一轮对齐口径 |

结论：**本轮只交付文档**。要让调度真正产出这条流程，需要单独一轮同时改 `routing.json`（新增
`asset_library` 类角色或显式模板、调整 `synth` 竞标）、`examples/` 与 golden 期望，并重新登记
`tts-runtime` 的合成入口口径；该项属于 `plan.v2` 之前的规则变更，不在本轮范围。

## 9. 与其它文档的关系

| 文档 | 关系 |
|---|---|
| `result/tts-library-proposal.md`（NOAH-66） | 唯一规格；本文是它的派发侧切片（§5 流程、§6 命中与安全、§7 发布幂等） |
| [plan-schema.md](plan-schema.md) | `plan.v1` 字段约束；本文不新增字段（§7） |
| [routing-rules.md](routing-rules.md) | 角色竞标与步骤模板；本文 §8 记录它与库流程的差距 |
| [gate-policy.md](gate-policy.md) | 阻塞码与退出码；本文 §4.4 的专用码是它的限定写法 |
| [kb-consumption.md](kb-consumption.md) | `recovery[]` / `stop_rules[]` 只能由知识驱动的约束（§4.2 依据） |
| `asset-tools/docs/tts-library.md` | 缓存键、lookup/fetch/publish/verify/index、ACL 与 outbox 的权威 |
| `tts-runtime/docs/library-integration.md` | 只合成不回填；`synth`/`batch`/`inspect` 真实参数 |
| `audio-runtime/docs/tts-library-derived.md` | 派生处理只作用于任务副本与来源记录字段 |

**代码测试通过 ≠ 库流程验证通过**：本 skill 的测试只证明选型与计划格式合法；1006 复用是否真的
生效，由执行方按计划跑完并留下 lookup/publish/query-back 证据后另行判定。
