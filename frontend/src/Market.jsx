import { Star, ArrowUpDown } from "lucide-react";
import { assetLabels, number, percent, score, layers } from "./api";
import { useMemo, useState } from "react";
export default function Market({
  items,
  selected,
  onSelect,
  onWatch,
  enabled,
}) {
  const [asset, setAsset] = useState(""),
    [country, setCountry] = useState(""),
    [sector, setSector] = useState(""),
    [sort, setSort] = useState("symbol"),
    [page, setPage] = useState(0);
  const filtered = useMemo(
    () =>
      items
        .filter(
          (i) =>
            (!asset || i.asset_class === asset) &&
            (!country || i.country === country) &&
            (!sector || i.sector === sector),
        )
        .sort((a, b) =>
          sort === "symbol"
            ? a.symbol.localeCompare(b.symbol)
            : (b.momentum ?? -Infinity) - (a.momentum ?? -Infinity),
        ),
    [items, asset, country, sector, sort],
  );
  const currentPage = Math.min(
    page,
    Math.max(0, Math.ceil(filtered.length / 40) - 1),
  );
  return (
    <section className="market">
      <div className="table-tools">
        <h3>
          Wertpapiere <small>({filtered.length})</small>
        </h3>
        <div className="filters">
          <select
            aria-label="Anlageklasse filtern"
            value={asset}
            onChange={(e) => {
              setAsset(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Alle Anlageklassen</option>
            {[
              ["equity", "Aktien"],
              ["commodity_spot", "Rohstoff-Spot"],
              ["commodity_reference", "Rohstoffreferenzen"],
              ["future", "Futures"],
              ["etf", "ETFs"],
              ["etc", "ETCs"],
              ["bond", "Anleihen"],
              ["index", "Indizes"],
            ].map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <select
            aria-label="Land filtern"
            value={country}
            onChange={(e) => {
              setCountry(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Alle Länder</option>
            {[...new Set(items.map((i) => i.country))]
              .filter(Boolean)
              .sort()
              .map((c) => (
                <option key={c}>{c}</option>
              ))}
          </select>
          <select
            aria-label="Sektor filtern"
            value={sector}
            onChange={(e) => {
              setSector(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Alle Sektoren</option>
            {[...new Set(items.map((i) => i.sector))]
              .filter(Boolean)
              .sort()
              .map((c) => (
                <option key={c}>{c}</option>
              ))}
          </select>
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>
                <button
                  onClick={() =>
                    setSort(sort === "symbol" ? "momentum" : "symbol")
                  }
                >
                  Wertpapier <ArrowUpDown size={13} />
                </button>
              </th>
              <th>Land / Sektor</th>
              <th>Letzter Kurs</th>
              <th>Momentum</th>
              <th>Risiko</th>
              <th>Signal</th>
              <th>
                <Star size={15} />
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered
              .slice(currentPage * 40, (currentPage + 1) * 40)
              .map((item) => (
                <tr
                  key={item.symbol}
                  className={selected?.symbol === item.symbol ? "selected" : ""}
                >
                  <td>
                    <button
                      className="security-link"
                      onClick={() => onSelect(item)}
                    >
                      <strong>{item.symbol}</strong>
                      <small>{item.name}</small>
                      <small>
                        {assetLabels[item.asset_class] || item.asset_class} ·{" "}
                        {item.date || "Keine Beobachtung"}
                        {item.age_days != null
                          ? ` · ${item.age_days} Tage alt`
                          : ""}
                      </small>
                    </button>
                  </td>
                  <td>
                    {item.country || "—"}
                    <small>{item.sector || "—"}</small>
                  </td>
                  <td>
                    {number(item.close)}
                    <small>
                      {item.close == null
                        ? "Kein Kurs importiert"
                        : item.currency || "Währung unbekannt"}
                    </small>
                  </td>
                  <td
                    className={
                      item.momentum == null
                        ? "muted"
                        : item.momentum >= 0
                          ? "positive"
                          : "negative"
                    }
                  >
                    {percent(item.momentum)}
                  </td>
                  <td>{percent(item.volatility)}</td>
                  <td>
                    {score(item, enabled).length ? (
                      score(item, enabled).map((s) => (
                        <span
                          className="score"
                          key={s.layer}
                          title={`${layers[s.layer]} · ${s.source}`}
                        >
                          {layers[s.layer]} {number(s.score)}
                        </span>
                      ))
                    ) : (
                      <small>Keine aktive Quelle</small>
                    )}
                  </td>
                  <td>
                    <button
                      className="icon"
                      aria-label={`${item.symbol} ${item.watched ? "aus Watchlist entfernen" : "beobachten"}`}
                      aria-pressed={!!item.watched}
                      onClick={() => onWatch(item)}
                    >
                      <Star
                        size={18}
                        fill={item.watched ? "#efb95b" : "none"}
                      />
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {!filtered.length && (
        <div className="empty">
          Keine Wertpapiere für diese Auswahl. Importiere Daten unter „Daten &
          Betrieb“ oder passe die Filter an.
        </div>
      )}
      <div className="pagination">
        <span>
          Seite {currentPage + 1} /{" "}
          {Math.max(1, Math.ceil(filtered.length / 40))}
        </span>
        <button
          disabled={!currentPage}
          onClick={() => setPage(currentPage - 1)}
        >
          Zurück
        </button>
        <button
          disabled={(currentPage + 1) * 40 >= filtered.length}
          onClick={() => setPage(currentPage + 1)}
        >
          Weiter
        </button>
      </div>
    </section>
  );
}
