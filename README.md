# 包装产线喷码追溯台

离线虚构喷码生产：品牌授权码段、实物喷码与校准扫描、箱托封装出货三账。重复码、废码重印、破箱重封保留来源和旧结论，当前能否出货按真实单品与关系重算。与产品批次召回项目的触达决定不同，本系统决定单品身份与箱托出货资格。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m codetrace demo --db demo.sqlite
python -m codetrace audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `code-import`、`scan-import`、`pack-import` 使用 `--db state.sqlite --input fixtures/<code|scan|pack>_ok.json`；`_bad.json` 分别展示未授权码段、失效扫描与破箱未注销旧封签。全部是合成数据，不接真实品牌系统或产线设备。

## 环缓存原件提取（archive-import）

扫描器把每张原始片段写入环形缓存的下一格，写满一圈即覆盖最旧片段；设备写指针只暴露“下一写入格模环长”，不是绝对序号，读取时也无法阻止写手移动。`archive-import` 针对同一写进程的绝对序号半开区间，统一裁决两条已授权取证路线能证明哪些真实写入：

- `freezes`：在允许的停采窗封存绝对下一序号锚点，并附停止/恢复回执后扫取物理窗口，可证明缓存仍持有的提交；旧序号已覆盖时只留原件缺口，不延长封存伪造。
- `mirrors`：软件观测台在提交时见证的只追加镜像，以提交回执绑定片段字节；镜像自身丢段保持缺口，回读到相同摘要不得补齐。
- `sweeps`：未封存的扫读保留所有与指针和读取时序相容的候选历史；仅当每条候选历史都证明同一（序号，字节）时才可外发，同字节的两次真实采集不会被摘要合并。
- `restarts`：写进程重启见证把后继世代显式链接到原世代；未链接的证据记为 `restart_unlinked`，见证晚到后重算解除。

输出按绝对序号给出 `delivered`（每片段独立重算 sha256，附来源与路线）、`gaps`（原件缺口）、`unattributed_originals`（观察到但无法确定发生次数的原件）与 `conflicts`（两条路线矛盾时保留两份原件，禁止以本地入库顺序覆盖）。`archive-report` 查看判定，`audit` 从 `file_journal` 重放全部入口并核对快照。范围限定四格环、一个重启与最多两圈候选历史，输入均为模拟字节。
