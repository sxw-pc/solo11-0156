# 包装产线喷码追溯台

离线虚构喷码生产：品牌授权码段、实物喷码与校准扫描、箱托封装出货三账。重复码、废码重印、破箱重封保留来源和旧结论，当前能否出货按真实单品与关系重算。与产品批次召回项目的触达决定不同，本系统决定单品身份与箱托出货资格。

```powershell
python -m pip install . --no-build-isolation --no-deps
python -m codetrace demo --db demo.sqlite
python -m codetrace audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口 `code-import`、`scan-import`、`pack-import` 使用 `--db state.sqlite --input fixtures/<code|scan|pack>_ok.json`；`_bad.json` 分别展示未授权码段、失效扫描与破箱未注销旧封签。全部是合成数据，不接真实品牌系统或产线设备。
