import { number } from "./api";
export default function Chart({ history }) {
  const prices = history.filter((r) => r.close != null);
  const isPrice = prices.length > 1;
  const points = isPrice ? prices : history.filter((r) => r.momentum != null);
  if (points.length < 2)
    return (
      <div className="empty">
        Für einen Verlauf werden mindestens zwei Beobachtungen benötigt.
      </div>
    );
  const values = points.map((r) => (isPrice ? r.close : r.momentum * 100));
  const low = Math.min(...values),
    high = Math.max(...values),
    spread = high - low || 1;
  const line = values
    .map(
      (v, i) =>
        `${12 + (i / (values.length - 1)) * 276},${120 - ((v - low) / spread) * 95}`,
    )
    .join(" ");
  return (
    <section className="chart">
      <h3>
        {isPrice ? "Kursverlauf" : "Momentum-Verlauf"}{" "}
        <small>historisch{isPrice ? "" : " · %"}</small>
      </h3>
      <svg
        viewBox="0 0 300 145"
        role="img"
        aria-label={`${isPrice ? "Kurs" : "Momentum"} von ${points[0].date} bis ${points.at(-1).date}`}
      >
        {[25, 57, 89, 120].map((y) => (
          <line key={y} x1="12" x2="288" y1={y} y2={y} stroke="#25364a" />
        ))}
        <polyline points={line} fill="none" stroke="#58d5de" strokeWidth="2" />
        <text x="12" y="140" fill="#91a8c0" fontSize="10">
          {points[0].date}
        </text>
        <text x="288" y="140" textAnchor="end" fill="#91a8c0" fontSize="10">
          {points.at(-1).date}
        </text>
      </svg>
      <p className="muted">
        Bandbreite {number(low)} – {number(high)}
        {isPrice ? "" : " %"} · {points.length} Beobachtungen
      </p>
    </section>
  );
}
