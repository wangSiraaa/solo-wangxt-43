import React, { useEffect, useState } from "react";
import { api } from "./api";
import BatchFlow from "./components/BatchFlow";
import SettlementDesk from "./components/SettlementDesk";

export default function App() {
  const [batches, setBatches] = useState([]);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState("flow");
  const [error, setError] = useState("");

  const load = () =>
    api.batches().then(setBatches).catch((e) => setError(e.message));

  useEffect(() => {
    load();
  }, []);

  const current = batches.find((b) => b.code === selected) || batches[0];

  return (
    <div className="page">
      <header>
        <h1>委外加工结算对账平台</h1>
        <span className="hint">领料 → 报工 → 收货检验 → 结算，全程数量流水可追溯</span>
      </header>
      {error && <div className="error">{error}</div>}
      <div className="layout">
        <aside>
          <h3>加工批次</h3>
          {batches.map((b) => (
            <button
              key={b.code}
              className={"batch-item" + (current && current.code === b.code ? " active" : "")}
              onClick={() => setSelected(b.code)}
            >
              <b>{b.code}</b>
              <span>{b.supplier}</span>
              <span>
                合格 {b.quantities.qualified_qty} / 应产 {b.quantities.expected_output}
              </span>
              <span className={"badge " + (b.locked ? "locked" : "open")}>
                {b.locked ? "已结算锁定" : "进行中"}
              </span>
            </button>
          ))}
        </aside>
        <main>
          {current && (
            <>
              <nav className="tabs">
                <button className={tab === "flow" ? "on" : ""} onClick={() => setTab("flow")}>
                  数量去向
                </button>
                <button
                  className={tab === "settlement" ? "on" : ""}
                  onClick={() => setTab("settlement")}
                >
                  结算工作台
                </button>
              </nav>
              {tab === "flow" ? (
                <BatchFlow key={current.code} code={current.code} />
              ) : (
                <SettlementDesk key={current.code} code={current.code} onChanged={load} />
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
