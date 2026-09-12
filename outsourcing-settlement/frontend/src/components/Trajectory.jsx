import React, { useEffect, useState } from "react";
import { api } from "../api";

const EVENT_LABEL = {
  ISSUE: "委外领料",
  REPORT: "报工（备查）",
  RECEIVE_QUALIFIED: "收货·合格",
  RECEIVE_REWORK: "收货·返工",
  RECEIVE_SCRAP: "收货·报废",
  REWORK_RETURN_QUALIFIED: "返工回厂·合格",
  REWORK_RETURN_SCRAP: "返工回厂·报废",
  TRANSFER_OUT: "转厂发出",
  TRANSFER_RETURN_QUALIFIED: "新厂回厂·合格",
  TRANSFER_RETURN_SCRAP: "新厂回厂·二次报废",
  SETTLE_LOCK: "结算锁定",
  REWORK_FEE: "责任账·返工费",
  COMPENSATION: "责任账·赔偿",
};

export default function Trajectory({ code }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.trajectory(code).then(setData).catch((e) => setError(e.message));
  }, [code]);

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>加载中…</p>;

  return (
    <section className="panel">
      <h3>货物轨迹：同一批货上两家供应商各自承担什么</h3>
      <table>
        <thead>
          <tr>
            <th>时间</th><th>事件</th><th>数量</th><th>金额</th>
            <th>实物所在方</th><th>质量责任方</th><th>应收应付方</th><th>摘要</th>
          </tr>
        </thead>
        <tbody>
          {data.events.map((e, i) => (
            <tr key={i} className={e.kind === "MONEY" ? "money-row" : ""}>
              <td>{e.at.slice(0, 10)}</td>
              <td>{EVENT_LABEL[e.event] || e.event}</td>
              <td>{e.qty ?? "—"}</td>
              <td>{e.amount ?? "—"}</td>
              <td>{e.custodian}</td>
              <td>{e.liability_party}</td>
              <td>{e.settlement_party}</td>
              <td>{e.memo}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
