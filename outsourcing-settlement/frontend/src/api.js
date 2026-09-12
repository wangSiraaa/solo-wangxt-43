const BASE = "/api";

async function req(path, options) {
  const resp = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || JSON.stringify(data));
  return data;
}

export const api = {
  batches: () => req("/batches/"),
  flow: (code) => req(`/batches/${code}/flow/`),
  reconciliation: (code) => req(`/batches/${code}/reconciliation/`),
  trajectory: (code) => req(`/batches/${code}/trajectory/`),
  contract: (code) => req(`/contracts/${code}/`),
  settlementPreview: (code) => req(`/batches/${code}/settlement/preview/`),
  confirm: (code, party) =>
    req(`/batches/${code}/settlement/confirm/`, {
      method: "POST",
      body: JSON.stringify({ party }),
    }),
  postReceipt: (payload) =>
    req("/receipts/", { method: "POST", body: JSON.stringify(payload) }),
  postTransfer: (payload) =>
    req("/transfers/", { method: "POST", body: JSON.stringify(payload) }),
};
