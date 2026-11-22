# 包装产线喷码追溯台

离线虚构喷码生产：品牌授权码段、实物喷码与校准扫描、箱托封装出货三账。重复码、废码重印、破箱重封保留来源和旧结论，当前能否出货按真实单品与关系重算。与产品批次召回项目的触达决定不同，本系统决定单品身份与箱托出货资格。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m codetrace demo --db demo.sqlite
python -m codetrace audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `code-import`、`scan-import`、`pack-import` 使用 `--db state.sqlite --input fixtures/<code|scan|pack>_ok.json`；`_bad.json` 分别展示未授权码段、失效扫描与破箱未注销旧封签。全部是合成数据，不接真实品牌系统或产线设备。

## 运输过载原件（合成）

`impact-import` 处理仓内预先合并好的力值（DEMO-LEG 基准）。新增 `witness-import` 处理真实的双通道/见证片运输资料（TRANSPORT-X），两者互不相关：

```powershell
python -m codetrace witness-import --db state.sqlite --input fixtures/impact_witness.json
python -m codetrace witness-report --db state.sqlite
python -m codetrace audit --db state.sqlite
```

一次 `witness-import` 可以装入一份案件原件，也可以装入带 `cases` 列表的本地文件（`fixtures/impact_witness.json` 即多份独立案件）。单份案件的字段：

- `runs`：每只传感器的本地刻度序列、所属连续运行轮次，以及本轮的 `offset` 或 `allowed_offsets`。同一轮只允许一个整轮时差，不能逐峰挑偏移；
- `syncs`：同轮独立对时脉冲及可信交接刻度 `tick`；`a_tick - b_tick` 给出两轮时差；
- `strips`：不可逆合成见证片。只有布置/回收/本段首末手续齐全且布置时未转态，才能签“本段已越线”，刻度与峰值仍未知；同一片不能签两段；
- `receipts`：交接回执，可分别信事发刻度、阶段与采样方式；不连续采样的回执不能逐刻对照原始序列。

输出按运输段给出 `overload`（含真实并发刻度与和值）、`overload_phase`（只签越线、刻度未核）、`no_overload`、`insufficient_coverage`、`unverified` 或 `conflict`，并列出 `missing_observations`。缺样不填零；重启后的校准不回填旧段；各历史分别求解，不跨历史拼证。每个原件入 `file_journal`，`audit` 从原件重放；后到的矛盾原件保留旧结果并另写冲突回执。合成数据，不用于现实运输责任认定。
