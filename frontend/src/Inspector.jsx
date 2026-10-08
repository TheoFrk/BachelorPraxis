import Chart from "./Chart";
import { assetLabels, layers, number, percent } from "./api";
import { Star, X } from "lucide-react";
export default function Inspector({ item, history, onWatch, onClose }) {
  if (!item)
    return (
      <aside className="inspector empty">
        Wähle ein Wertpapier aus der Übersicht.
      </aside>
    );
  return (
    <aside className="inspector">
      <div className="panel-title">
        Wertpapier
        <button
          className="icon"
          onClick={onClose}
          aria-label="Detail schließen"
        >
          <X size={18} />
        </button>
      </div>
      <div className="detail-body">
        <div className="detail-heading">
          <h2>{item.symbol}</h2>
          <button
            className="icon"
            onClick={() => onWatch(item)}
            aria-label="Watchlist ändern"
            aria-pressed={!!item.watched}
          >
            <Star size={22} fill={item.watched ? "#efb95b" : "none"} />
          </button>
        </div>
        <p className="cyan company">{item.name}</p>
        <dl className="facts">
          <dt>Typ</dt>
          <dd>{assetLabels[item.asset_class] || item.asset_class}</dd>
          <dt>Basiswert</dt>
          <dd>{item.underlying || "—"}</dd>
          <dt>Einheit</dt>
          <dd>{item.unit}</dd>
          <dt>Land</dt>
          <dd>{item.country || "Nicht angegeben"}</dd>
          <dt>Sektor</dt>
          <dd>{item.sector || "Nicht angegeben"}</dd>
          <dt>Währung</dt>
          <dd>{item.currency || "Nicht angegeben"}</dd>
          <dt>Datenstand</dt>
          <dd>{item.date || "Keine Beobachtung"}</dd>
        </dl>
        {item.asset_class === "commodity_reference" && (
          <p className="note">
            Monatlicher Referenzpreis der World Bank, kein handelbares
            Wertpapier und kein Intraday-Kurs. Nominale USD je angegebener
            Einheit.
          </p>
        )}
        {item.asset_class === "future" && (
          <p className="note">
            Kontinuierliche Futures-Reihe: Rollmethode und Fälligkeit müssen vom
            Kursanbieter dokumentiert werden. Kein direkt handelbarer
            Einzelkontrakt.
          </p>
        )}
        <Chart history={history} />
        <h3>Kennzahlen</h3>
        <p className="muted">{item.metric_basis}</p>
        <div className="metrics-detail">
          <div>
            Momentum<strong>{percent(item.momentum)}</strong>
          </div>
          <div>
            Volatilität<strong>{percent(item.volatility)}</strong>
          </div>
        </div>
        <h3>Signalquellen</h3>
        <div className="signal-list">
          {Object.entries(layers).map(([key, label]) => (
            <div key={key}>
              <span>{label}</span>
              <strong className={item.signals[key] ? "cyan" : "muted"}>
                {item.signals[key]
                  ? number(item.signals[key].score)
                  : "Nicht importiert"}
              </strong>
              {item.signals[key] && (
                <small>
                  {item.signals[key].source} · {item.signals[key].date}
                </small>
              )}
            </div>
          ))}
        </div>
        {item.exposures?.length > 0 && (
          <section>
            <h3>Verbundene Treiber</h3>
            {item.exposures.map((e) => (
              <p className="note" key={e.driver + e.region}>
                {e.driver} · {e.region}
                <br />
                {e.relation}
              </p>
            ))}
          </section>
        )}
        <p className="note">
          Scores sind modellspezifisch. Sie werden nicht ungeprüft gemittelt und
          sind keine Kauf- oder Verkaufsempfehlung.
        </p>
        <p className="muted">
          Quelle: {item.source || "Keine Kursquelle"}
          <br />
          Import:{" "}
          {item.imported_at
            ? new Date(item.imported_at).toLocaleString("de-DE")
            : "noch keiner"}
        </p>
      </div>
    </aside>
  );
}
