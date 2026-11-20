# 医疗器械成套出入库的序列号仓

合成业务资料 + 可装载的序列号仓 + 业务查询。字段与来源语义见
[docs/资料口径.md](docs/资料口径.md)，仓表与查询口径见
[docs/仓表设计.md](docs/仓表设计.md)，完整性目录为 `data/catalog.json`。
不得把文件自检成功解释成业务结论成立。

全部数据为合成数据，不含患者信息，也不替代器械审批。

## 环境

Python 3.11.2；仓库依赖已在 requirements.txt 固定版本。

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack

不带参数时只做资料自检：读取资料目录，核对文件摘要、字段类型、记录键、引用与行数，
不进行估计、推荐、批准或结算。目录摘要只对应当前保存的输入文件。

## 装载与查询

    python3 -m sourcepack load --db build/wh.duckdb --root . --label 初始资料
    python3 -m sourcepack availability --db build/wh.duckdb --customer C1
    python3 -m sourcepack kit --db build/wh.duckdb --kit A:K1 --as-of 2026-10-15T00:00:00+08:00
    python3 -m sourcepack component --db build/wh.duckdb --key A:2
    python3 -m sourcepack lot --db build/wh.duckdb --lot L2
    python3 -m sourcepack service --db build/wh.duckdb --customer C1
    python3 -m sourcepack reconcile --db build/wh.duckdb --customer C1 --note 首次对账
    python3 -m sourcepack revisions --db build/wh.duckdb --customer C1
    python3 -m sourcepack compare --db build/wh.duckdb --left 1 --right 2
    python3 -m sourcepack conflicts --db build/wh.duckdb --session session-0001
    python3 -m sourcepack sessions --db build/wh.duckdb

每次装载生成会话；旧会话的事实不被后来的修订改写。同一批文件再次装载不会产生新事实。
所有查询支持 `--session`（历史事实版本）和 `--as-of`（业务时点）。

## 检查与测试

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack tests

`tests/test_sources.py` 覆盖资料容器与输入诊断；`tests/test_warehouse.py` 覆盖装载、
历史截点、交付证据、换件/借件、批次追查、冲突待核与会话修订。运行不需要访问外部业务系统。
