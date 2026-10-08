export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : `Anfrage fehlgeschlagen (${response.status})`,
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
export const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
export const layers = {
  price: "Preis-Signal",
  lexicon: "Lexikon",
  finbert: "FinBERT",
  llm: "LLM",
};
export function score(item, enabled) {
  // Different models have different score scales. Never average them silently.
  return Object.keys(layers)
    .filter((k) => enabled[k] && item.signals[k])
    .map((k) => ({ layer: k, ...item.signals[k] }));
}
export function percent(value) {
  return value == null
    ? "—"
    : new Intl.NumberFormat("de-DE", {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(value);
}
export function number(value) {
  return value == null
    ? "—"
    : new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 }).format(
        value,
      );
}

export const assetLabels = {
  equity: "Aktie",
  etf: "ETF",
  etc: "ETC",
  commodity_spot: "Rohstoff-Spot",
  commodity_reference: "Rohstoffreferenz",
  future: "Future",
  bond: "Anleihe",
  index: "Index",
};
