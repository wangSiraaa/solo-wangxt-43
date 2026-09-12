import React, { useEffect, useState } from "react";
import { api } from "../api";

const ENTRY_LABEL = {
  ISSUE: "委外领料",
  REPORT: "报工（备查）",
  RECEIVE_QUALIFIED: "收货·合格",
  RECEIVE_REWORK: "收货·返工",
  RECEIVE_SCRAP: "收货·报废",
  REWORK_RETURN_QUALIFIED: "返工回厂·合格",
  REWORK_RETURN_SCRAP: "返工回厂·报废",
  SETTLE_LOCK: "结算锁定",
};

function Box({ title, value, sub, tone }) {
  return (
    <div className={"box " + (tone || "")}>
      <div className="box-title">{title}</div>
      <div className="box-value">{value}</div>
      {sub && <div className="box-sub">{sub}</div>}
    </div>
  );
}

export default function BatchFlow({ code }) {
  const [flow, setFlow] = useState(null);
  const [recon, setRecon] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.flow(code), api.reconciliation(code)])
      .then(([f, r]) => {
        setFlow(f);
        setRecon(r);
      })
      .catch((e) => setError(e.message));
  }, [code]);

  if (error) return <div className="error">{error}</div>;
  if (!flow) return <p>加载中…</p>;

  const q = flow.quantities;
  return (
    <div>
      <section className="flow-grid">
        <Box title="委外领料" value={`${q.issued_qty}`} sub={`原料 × 转化比例 ${q.conversion_ratio}`} />
        <div className="arrow">→</div>
        <Box title="应产数量" value={q.expected_output} sub="领料 × 转化比例" tone="info" />
        <div className="arrow">→</div>
        <Box title="累计合格" value={q.qualified_qty} sub={`可结算 ${q.settleable_qty}`} tone="good" />
        <Box title="报废" value={q.scrap_total} sub={`约定损耗内 ${q.allowed_loss_qty} / 超损耗 ${q.over_loss_qty}`} tone={Number(q.over_loss_qty) > 0 ? "bad" : ""} />
        <Box title="在新厂" value={q.at_supplier_b} sub={`累计转出 ${q.transferred_out} / 二次报废 ${q.transfer_scrap_qty}`} tone={Number(q.at_supplier_b) > 0 ? "warn" : ""} />
        <Box title="在返(原厂)" value={q.rework_outstanding} sub="原厂待返工" />
        <Box title="在制/未回" value={q.in_process_qty} sub="应产 − 已交代数" />
      </section>

      {flow.transfers && flow.transfers.length > 0 && (
        <section className="panel">
          <h3>转厂返工（原厂无返工能力，费用由原厂承担）</h3>
          <table>
            <thead>
              <tr><th>转厂单</th><th>新厂</th><th>数量</th><th>返工单价</th><th>回厂合格</th><th>二次报废</th><th>应付新厂</th><th>状态</th></tr>
            </thead>
            <tbody>
              {flow.transfers.map((t) => {
                const qBack = t.receipts.reduce((s, r) => s + Number(r.qualified), 0);
                const sBack = t.receipts.reduce((s, r) => s + Number(r.scrap), 0);
                const fee = t.receipts.reduce((s, r) => s + Number(r.rework_fee), 0);
                return (
                  <tr key={t.code}>
                    <td>{t.code}（{t.transferred_at}）</td>
                    <td>{t.to_supplier}</td>
                    <td>{t.qty}</td>
                    <td>{t.rework_unit_price}</td>
                    <td>{qBack}</td>
                    <td>{sBack}</td>
                    <td>{fee.toFixed(2)}</td>
                    <td>{t.status === "CLOSED" ? "已完结" : "在途"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {flow.liabilities && flow.liabilities.length > 0 && (
            <table style={{ marginTop: 10 }}>
              <thead>
                <tr><th>责任账</th><th>责任方</th><th>应收方</th><th>金额</th><th>已抵扣</th><th>状态/追偿</th><th>说明</th></tr>
              </thead>
              <tbody>
                {flow.liabilities.map((le, i) => (
                  <tr key={i}>
                    <td>{le.entry_type === "REWORK_FEE" ? "返工费" : "赔偿"}</td>
                    <td>{le.supplier}</td>
                    <td>{le.counter_supplier || "—"}</td>
                    <td>{le.amount}</td>
                    <td>{le.offset_amount}</td>
                    <td>{le.claim ? `已转追偿 ${le.claim}` : le.status}</td>
                    <td>{le.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {flow.claims && flow.claims.length > 0 && (
            <p className="claims">
              独立追偿：
              {flow.claims.map((c) => (
                <b key={c.code}> {c.code} {c.amount}（{c.reason}）</b>
              ))}
            </p>
          )}
        </section>
      )}

      <section className="panel">
        <h3>报工对照（供应商声称，不参与结算）</h3>
        <p>
          供应商报工累计 <b>{q.reported_qty}</b>，我方检验合格 <b>{q.qualified_qty}</b>，
          差异 <b className="diff">{Number(q.reported_qty) - Number(q.qualified_qty)}</b>
        </p>
      </section>

      <section className="panel">
        <h3>账面核对（单据汇总 vs 数量流水）</h3>
        {recon && (
          <table>
            <thead>
              <tr><th>核对项</th><th>单据侧</th><th>流水侧</th><th>结果</th></tr>
            </thead>
            <tbody>
              {recon.checks.map((c) => (
                <tr key={c.measure}>
                  <td>{c.measure}</td>
                  <td>{c.document_side}</td>
                  <td>{c.ledger_side}</td>
                  <td>{c.balanced ? "✅" : "❌"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h3>收货明细（按收货日取合同价版）</h3>
        <table>
          <thead>
            <tr><th>回传号</th><th>类型</th><th>日期</th><th>合格</th><th>返工</th><th>报废</th><th>单价(价版)</th></tr>
          </thead>
          <tbody>
            {flow.receipts.map((r) => (
              <tr key={r.no}>
                <td>{r.no}</td>
                <td>{r.kind === "REWORK_RETURN" ? "返工回厂" : "正常收货"}</td>
                <td>{r.received_at}</td>
                <td>{r.qualified}</td>
                <td>{r.rework}</td>
                <td>{r.scrap}</td>
                <td>{r.unit_price}（{r.price_effective_from} 起）</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>数量流水</h3>
        <table>
          <thead>
            <tr><th>#</th><th>类型</th><th>数量</th><th>摘要</th></tr>
          </thead>
          <tbody>
            {flow.ledger.map((e) => (
              <tr key={e.id}>
                <td>{e.id}</td>
                <td>{ENTRY_LABEL[e.entry_type] || e.entry_type}</td>
                <td>{e.qty}</td>
                <td>{e.memo}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
