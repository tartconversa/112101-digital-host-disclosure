"""助农产地承诺事实资料库（离线数据仓库）。

仅依赖 Python 标准库，用 SQLite 沉淀三类可追溯事实：

1. 内容团队的视频版本公开承诺（content claims）；
2. 供应履约团队的批次、产地凭证与实际发货（lots / shipments）；
3. 售后团队沿订单、发货批次解释争议所需的核验历史。

模块划分：

- ``timeutil``：ISO 8601 与采收窗口解析；
- ``schema`` / ``db``：SQLite 连接与表结构；
- ``ingest``：把本地 CSV 交付目录追加写入落地层；
- ``integrate``：从落地层重建整合层并执行承诺核验；
- ``verify``：纯函数的承诺-批次核验规则；
- ``queries`` / ``reports`` / ``cli``：面向采购、内容、售后的查询与命令行。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
