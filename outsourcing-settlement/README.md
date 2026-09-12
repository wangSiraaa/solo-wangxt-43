# 委外加工结算对账平台

把 **委外领料 → 报工 → 收货检验 → 结算** 串成一条可追溯的数量流水，解决生产财务
"外协厂报工数量"与"实际可结算数量"对不上的问题。

- **React**：每个加工批次的数量去向（领料、应产、合格、返工、报废、在制、可结算）与双方差异公示
- **Django REST Framework**：分批收货过账、幂等回传、结算核算与快照
- **PostgreSQL**：数量流水（`QuantityLedgerEntry`）、合同价版（`ContractPriceVersion`）、结算快照
- **金额一律 `Decimal`**，数量 3 位小数、金额 2 位（`ROUND_HALF_UP`）

## 核心规则

| 规则 | 实现 |
|---|---|
| 报工 ≠ 收货 | 报工只入备查流水（`REPORT`），永不进入可结算数量 |
| 收货拆三向 | 每张收货单拆 合格/返工/报废；返工回厂单消耗"在返"数量且不得超出 |
| 数量可回溯 | 应产 = 领料 × 转化比例；约定损耗 = 应产 × 约定损耗率；恒等式 `应产 = 合格 + 报废 + 在返 + 在制` 由测试保证，**不用任何完成百分比** |
| 可结算数量 | `min(累计合格, 应产)`；超交部分只列差异不结算 |
| 超损耗扣款 | `max(0, 总报废 − 约定损耗) ÷ 转化比例 × 原料单价`，折回原料计费 |
| 超期扣款 | 按合同交付节点逐段计算：节点短交数 = 累计计划 − 到期日累计合格；**每个节点只对"新增短交"扣一次**，部分交付不整单重复扣罚，单节点封顶 |
| 价版 | 每张收货单按收货日取当时生效的合同价版 |
| 结算快照 | 双方确认前公示差异；任一方确认期间数据变化会自动重置确认；双方确认后冻结 JSON 快照（含 SHA-256 指纹）并锁定批次 |

## 运行（容器化演示）

```bash
docker compose up --build
# 前端 http://localhost:8080   后端 http://localhost:8000/api/
```

后端启动时自动建表并载入演示数据（`seed_demo`），演示数据**内置三类异常**：

1. **部分不合格**：`RC-002` 拆为 合格 9000 / 返工 800 / 报废 600
2. **重复回传**：`RC-002` 回传两次，第二次幂等返回原单，流水不重复入账
3. **超约定损耗**：总报废 900 > 约定损耗 600（应产 30000 × 2%）

演示批次 `B-2026-001` 的手工核对账：

```
应产      10000米 × 3.0                 = 30000 件
加工费    15000×1.20 + 13700×1.15       = 33755.00   (7/1 起价版 1.20→1.15)
超损耗    (900−600) ÷ 3.0 × 6.00        =   600.00
超期      节点8/31短交2000，9/5交齐迟5天 × 0.05 = 500.00
应付合计                                = 32655.00
```

## 独立核对用例

账面数量与费用都有**不信任服务层、独立重算**的测试（`core/tests/test_reconciliation.py`，16 个用例）：

- 账面数量：单据汇总 == 数量流水重算 == 领料×比例×损耗恒等式
- 费用：用原始收货单与价版独立重算，与冻结快照逐分比对；快照行合计 == 快照总额
- 重复回传幂等、返工未回不结算、超交不结算、超期不重复扣、快照冻结后拒收拒改

```bash
# 容器内
docker compose exec backend python manage.py test core
# 或本地（SQLite 回退，无需 PostgreSQL）
cd backend && pip install -r requirements.txt
DJANGO_SETTINGS_MODULE=config.settings python manage.py test core
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches/` | 批次列表（含数量汇总） |
| GET | `/api/batches/{code}/flow/` | 数量去向 + 数量流水 + 收货/报工明细 |
| GET | `/api/batches/{code}/reconciliation/` | 账面核对（单据侧 vs 流水侧） |
| POST | `/api/receipts/` | 收货回传（`supplier_receipt_no` 幂等） |
| POST | `/api/work-reports/` | 报工回传（备查） |
| GET | `/api/batches/{code}/settlement/preview/` | 结算试算 + 双方差异 |
| POST | `/api/batches/{code}/settlement/confirm/` | 双方确认 `{party: supplier\|us}` |
| GET | `/api/batches/{code}/settlement/` | 结算单与冻结快照 |
| GET | `/api/contracts/{code}/` | 合同价版与交付节点 |

## 目录

```
backend/   Django + DRF：models(流水/价版/快照) · services(核算) · tests(独立核对)
frontend/  React + Vite：批次数量去向看板 · 结算工作台（差异公示/双方确认/快照）
docker-compose.yml  PostgreSQL 16 + 后端 + Nginx 前端
```
