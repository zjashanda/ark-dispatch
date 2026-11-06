# 离线快照（tests/fixtures）

这些文件是从 `zjashanda/ark-kb` 的 `main`（提交 `4af4f6d`，2026-11-06）复制的固定快照，只用于单测与本地演示：

- `registry.skills.json` ← `ark-kb/registry/skills.json`（35 条）
- `kb/entries/{knowledge,method,run,bug}.jsonl` ← `ark-kb/entries/*/entries.jsonl`

sha256：

```
270ce59593e72a2af63f06b545eff86ae83a65b57c89e19bfe9846e39511eb87  registry.skills.json
6740104642d5e995316bb43335a346ad9debcce3756fb86c1b0018a13c72e5aa  kb/entries/knowledge.jsonl
c60b73098185c902c4a5e3b640c0f5e065b80a9ac4bdff72974a7aa63a2ff709  kb/entries/method.jsonl
d457819845a73c88b2c6b89061482d12ab3a5b4d8f2018823365231ace9aa9c9  kb/entries/run.jsonl
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  kb/entries/bug.jsonl
```

更新方式（只在确实需要跟进台账/知识库时手动做，并同步上面的哈希与 `tests/test_dispatch.py` 里的 `registry_rev`）：

```bash
cp /path/to/ark-kb/registry/skills.json tests/fixtures/registry.skills.json
for d in knowledge method run bug; do
  cp /path/to/ark-kb/entries/$d/entries.jsonl tests/fixtures/kb/entries/$d.jsonl
done
sha256sum tests/fixtures/registry.skills.json tests/fixtures/kb/entries/*.jsonl
python -m unittest discover -s tests -v
```

测试只读这些快照，不访问网络、不访问真实设备，也不回写命中计数。
