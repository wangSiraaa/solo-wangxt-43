import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api";

const LINE_LABEL = {
  QUALIFIED: "合格加工费",
  OVER_LOSS: "超约定损耗扣款",
  LATE_PENALTY: "超期扣款",
};

export default function SettlementDesk({ code, onChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    () => api.settlementPreview(code).then(setData).catch((e) => setError(e.message)),
    [code]
  );

  useEffect(() => {
    load();
  }, [load]);

  const confirm = async (party) => {
    setBusy(true);
    setError("");
    try {
      await api.confirm(code, party);
      await load();
      onChanged && onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>加载中…</p>;

  const view = data.status === "CONFIRMED" ? data.snapshot : data.preview;
  const frozen = data.status === "CONFIRMED";

  return (
    <div>
      {frozen && (
        <div className="banner locked">
          结算已双方确认并冻结 · 快照指纹 {data.snapshot.calc_hash.slice(0, 16)}…
        </div>
      )}

      <section className="panel">
        <h3>双方差异（确认前公示）</h3>
        <table>
          <tbody>
            <tr>
              <td>供应商报工声称</td>
              <td>{view.diff.supplier_claimed_qty}</td>
              <td>我方检验合格</td>
              <td>{view.diff.our_qualified_qty}</td>
              <td>差异</td>
              <td className="diff">{view.diff.diff_qty}</td>
            </tr>
            <tr>
              <td>我方判定报废</td>
              <td>{view.diff.our_scrap_qty}</td>
              <td>在返未回</td>
              <td>{view.diff.our_rework_outstanding_qty}</td>
              <td>超交不结算</td>
              <td>{view.quantities.over_delivered_qty}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>结算明细{frozen ? "（快照）" : "（试算）"}</h3>
        <table>
          <thead>
            <tr><th>类型</th><th>数量</th><th>单价</th><th>金额</th><th>说明</th></tr>
          </thead>
          <tbody>
            {view.lines.map((l, i) => (
              <tr key={i} className={Number(l.amount) < 0 ? "neg" : ""}>
                <td>{LINE_LABEL[l.line_type]}</td>
                <td>{l.qty}</td>
                <td>{l.unit_price}</td>
                <td>{l.amount}</td>
                <td>{l.note}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr><td colSpan="3">合格加工费</td><td>{view.qualified_amount}</td><td /></tr>
            <tr><td colSpan="3">超损耗扣款</td><td>-{view.over_loss_amount}</td><td /></tr>
            <tr><td colSpan="3">超期扣款</td><td>-{view.late_penalty_amount}</td><td /></tr>
            <tr className="total"><td colSpan="3">应付合计</td><td>{view.total_amount}</td><td /></tr>
          </tfoot>
        </table>
      </section>

      {!frozen && (
        <section className="panel confirm-bar">
          <span>
            确认状态：外协厂 {data.confirmed_by_supplier ? "✅" : "⬜"} · 我方{" "}
            {data.confirmed_by_us ? "✅" : "⬜"}
          </span>
          <button disabled={busy} onClick={() => confirm("supplier")}>
            外协厂确认
          </button>
          <button disabled={busy} onClick={() => confirm("us")}>
            我方确认
          </button>
          <span className="hint">双方确认后冻结快照；期间有新收货将重置确认</span>
        </section>
      )}
    </div>
  );
}
