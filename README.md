# 助农产地承诺事实资料库

离线运行的 Python 数据仓库，把短视频公开承诺、农产品批次凭证、订单发货与售后记录
沉淀为**可追溯事实**，供采购、内容运营、售后三类人员共同核实：视频里声称的产地与
采收承诺，是否能由用户实际收到货物的批次凭证支撑。

项目仅使用 Python 标准库（含 SQLite），无需网络、无需安装第三方包。

## 业务口径（核验规则）

1. **名称相同不等于来源相同。** 视频承诺与商品目录同名不能证明货物来自所称批次，
   核验只基于发货记录中的实际 `lot_id`；同一 SKU 有多个供货来源时，绝不按 SKU
   推定所有订单同产地。
2. **只有 `verified` 批次记录构成有效凭证。** `pending`、`corrected` 等状态、
   缺失批次关系、批次未知、字段缺失等任何证据缺口，结论一律保留
   `pending_verification`（待核实），不得填成符合或不符合。
3. **证据齐全且直接冲突才判 `contradicted`。** 例如有效凭证产地为 REGION-B，
   而下单时版本承诺 REGION-A。
4. **订单钉住下单时的视频版本。** 后续内容更正只产生新版本，不回溯改变历史订单
   核验所依据的承诺。
5. **后补凭证可以改变结论，但历史轨迹永久留痕。** 批次状态变化在 SCD 历史与
   核验历史中均可回放。
6. **一笔订单可拆多次发货、缺货可换批次/替代发货。** 订单整体状态取各发货核验的
   最差结果（冲突 > 待核实 > 符合）；未发货订单为待核实。

## 数据分层

| 层 | 表 | 性质 |
| --- | --- | --- |
| 落地层 | `ingest_batch`、`raw_record` | 只追加。按业务 `record_id` 保存每次交付的完整 JSON 载荷与 SHA-256 指纹，修正不覆盖旧观察 |
| 整合层 | `video_post` / `video_revision` / `video_claim`、`supplier` / `product` / `lot` / `lot_history` / `certificate_evidence`、`customer_order` / `order_pinned_claim` / `shipment` | 每次摄入后从落地层确定性重建；批次表含 SCD2 风格状态历史 |
| 核验层 | `shipment_claim_check`、`check_history`、`order_status_snapshot`、`order_status_history` | 当前判定 + 逐批次只追加历史 |
| 视图 | `v_evidence_applicability`、`v_shipment_full`、`v_order_trace` | 面向三个角色的查询面 |

时间采用双时间轴：事件时间（`published_at`、`placed_at`、`shipped_at` 等）与
记录时间（摄入批次 `batch_id` / `delivered_at`）。

## 目录

- `sources/`：题目随附的基线合成 CSV（UTF-8，时间为带时区 ISO 8601）。
- `examples/daily_deliveries/day1…day4/`：模拟平台每日接收的交付快照，
  覆盖后补凭证翻转、内容更正、拆单发货、替代发货等场景。
- `src/origin_fulfillment/`：仓库包。
  - `ingest.py` 落地摄入（指纹去重、幂等、输入校验）
  - `integrate.py` 整合层重建与核验重算
  - `verify.py` 纯函数核验规则（单元测试友好）
  - `queries.py` / `reports.py` / `cli.py` 三角色查询与命令行
- `tests/`：45 个 `unittest` 用例（含随附源文件格式基线检查）。

## 使用方式

包采用 `src/` 布局，运行前设置 `PYTHONPATH=src`（或自行 `pip install -e .`）。

### 持续摄入每日交付

```bash
export PYTHONPATH=src
python -m origin_fulfillment --db data/warehouse.sqlite3 \
    ingest --dir examples/daily_deliveries/day1-2026-07-08 \
    --at 2026-07-08T00:00:00Z
# 随后每天指向新的交付目录；重复摄入同一目录保持幂等，不产生空批次。
```

### 采购：每项凭证适用于哪些批次、被哪些订单使用

```bash
python -m origin_fulfillment --db data/warehouse.sqlite3 evidence
python -m origin_fulfillment --db data/warehouse.sqlite3 lot LOT-32   # 批次状态轨迹
```

### 内容运营：按视频版本查看公开承诺

```bash
python -m origin_fulfillment --db data/warehouse.sqlite3 posts --post POST-4
```

### 售后：沿订单与实际发货批次解释争议

```bash
python -m origin_fulfillment --db data/warehouse.sqlite3 trace ORD-93
python -m origin_fulfillment --db data/warehouse.sqlite3 history --shipment SHIP-52
python -m origin_fulfillment --db data/warehouse.sqlite3 history --order ORD-91
python -m origin_fulfillment --db data/warehouse.sqlite3 orders-state pending_verification
```

所有查询均可加全局 `--json` 输出机器可读 JSON；`batches` 查看摄入台账。

## 示例场景（`examples/daily_deliveries`）

| 订单 | 场景 | 结论轨迹 |
| --- | --- | --- |
| ORD-90 / SHIP-51 | LOT-31 凭证有效，产地与承诺一致 | 始终 conforming |
| ORD-91 / SHIP-52 | LOT-32 凭证先是 pending（即使产地名称已不同也只能待核实），第 3 天补交凭证 verified 后露出 REGION-B 与承诺 REGION-A 冲突 | pending → contradicted，历史留痕 |
| ORD-92 / SHIP-53 | 替代发货（`replaces_shipment`）；LOT-33 采收日在承诺窗口外但凭证 corrected，第 4 天 verified 后判定冲突 | pending → contradicted |
| ORD-93 / SHIP-54+55 | 一单拆两次发货，分别来自 REGION-A、REGION-B 两个批次 | 订单整体 contradicted |
| ORD-94 / SHIP-56 | 第 4 天内容更正为 revision 2 后下单，钉住新承诺 CL-21，不受 revision 1 历史影响 | conforming |

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 程序化调用

```python
from origin_fulfillment.db import connect
from origin_fulfillment.ingest import ingest_delivery
from origin_fulfillment.integrate import rebuild
from origin_fulfillment import queries

conn = connect("data/warehouse.sqlite3")
ingest_delivery(conn, "sources", delivered_at="2026-07-10T00:00:00Z")
rebuild(conn)
for row in queries.evidence_applicability(conn):
    print(dict(row))
```

资料为虚构样例，不对应真实个人或平台。
