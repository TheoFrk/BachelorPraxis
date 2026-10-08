import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { assetLabels, api, number } from "./api";

function RegionMap({ regions, selected, onSelect }) {
  const ref = useRef(null),
    map = useRef(null),
    lastSelected = useRef(selected);
  useEffect(() => {
    const instance = L.map(ref.current, {
      scrollWheelZoom: false,
      zoomAnimation: false,
      fadeAnimation: false,
    }).setView([6.2, -4.2], 6);
    map.current = instance;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(instance);
    Object.entries(regions).forEach(([id, r]) => {
      const marker = L.circleMarker([r.lat, r.lon], {
        radius: 9,
        color: "#58d5de",
        fillColor: "#58d5de",
        fillOpacity: 0.6,
        weight: 2,
      }).addTo(instance);
      const label = document.createElement("span");
      label.textContent = r.name;
      marker.bindTooltip(label);
      marker.on("click", () => {
        instance.setView([r.lat, r.lon], 7, { animate: false });
        onSelect(id);
      });
    });
    return () => {
      instance.stop();
      instance.remove();
      map.current = null;
    };
  }, [regions, onSelect]);
  useEffect(() => {
    if (lastSelected.current === selected) return;
    lastSelected.current = selected;
    const r = regions[selected];
    if (r && map.current)
      map.current.setView([r.lat, r.lon], 7, { animate: false });
  }, [regions, selected]);
  return (
    <div
      ref={ref}
      className="region-map"
      aria-label="Karte der beobachteten Kakao-Anbaugebiete"
    />
  );
}

export default function Drivers({
  data,
  onRefresh,
  items,
  onSelectSecurity,
  onError,
}) {
  const [region, setRegion] = useState("GH-ASHANTI"),
    [driver, setDriver] = useState("cocoa"),
    [busy, setBusy] = useState(""),
    [weatherHistory, setWeatherHistory] = useState([]);
  async function sync(feed) {
    setBusy(feed);
    try {
      const result = await api(`/context/sync/${feed}`, { method: "POST" });
      if (result.status === "error") onError(new Error(result.message));
      await onRefresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy("");
    }
  }
  useEffect(() => {
    let canceled = false;
    setWeatherHistory([]);
    if (data)
      api(`/context/history/${region}`)
        .then((r) => {
          if (!canceled) setWeatherHistory(r.history);
        })
        .catch(onError);
    return () => {
      canceled = true;
    };
  }, [region, data, onError]);
  if (!data) return <div className="empty">Kontextdaten werden geladen …</div>;
  const regionRows = data.series.filter((r) => r.region === region);
  const find = (metric) => regionRows.find((r) => r.metric === metric);
  const links = items.filter((i) =>
    i.exposures?.some((e) => e.driver === driver),
  );
  return (
    <section className="drivers">
      <div className="driver-toolbar">
        <div>
          <h2>Treiber & Ereignisse</h2>
          <p className="muted">
            Anbaugebiete, Wetter und geopolitische Meldungen im selben Lagebild.
          </p>
        </div>
        <div className="driver-actions">
          <button
            className="primary"
            disabled={!!busy}
            onClick={() => sync("weather")}
          >
            {busy === "weather" ? "Wetter wird geladen …" : "Wetter abrufen"}
          </button>
          <button disabled={!!busy} onClick={() => sync("commodities")}>
            {busy === "commodities"
              ? "Referenzen werden geladen …"
              : "Rohstoffpreise abrufen"}
          </button>
          <button disabled={!!busy} onClick={() => sync("news")}>
            {busy === "news"
              ? "Meldungen werden geladen …"
              : "Meldungen abrufen"}
          </button>
        </div>
      </div>
      <div className="driver-tabs">
        <button
          className={driver === "cocoa" ? "active" : ""}
          onClick={() => setDriver("cocoa")}
        >
          Kakao · Wetter & Lieferketten
        </button>
        <button
          className={driver === "gold" ? "active" : ""}
          onClick={() => setDriver("gold")}
        >
          Gold · Geopolitik
        </button>
      </div>
      <div className="driver-layout">
        <div>
          {driver === "cocoa" ? (
            <>
              <RegionMap
                regions={data.regions}
                selected={region}
                onSelect={setRegion}
              />
              <div className="region-controls">
                {Object.entries(data.regions).map(([id, r]) => (
                  <button
                    key={id}
                    className={region === id ? "active" : ""}
                    onClick={() => setRegion(id)}
                  >
                    {r.name}
                  </button>
                ))}
              </div>
              <p className="muted">
                Repräsentative Messpunkte, keine landesweite oder
                erntegewichtete Flächenanalyse.
              </p>
            </>
          ) : (
            <div className="gold-context">
              <h3>Gold reagiert auf mehrere Treiber</h3>
              <p>
                Geopolitische Risiken sind eine mögliche Einflussgröße neben
                Realzinsen, US-Dollar, Liquidität und Zentralbanknachfrage.
              </p>
              <p>
                Die Meldungen rechts sind Medienbeobachtungen. Sie bestätigen
                weder ein Ereignis noch eine Preiswirkung.
              </p>
              <a
                href="https://doi.org/10.1111/j.1540-6288.2010.00244.x"
                target="_blank"
                rel="noreferrer"
              >
                Baur & Lucey · Hedge und Safe Haven
              </a>
            </div>
          )}
          <section className="linked">
            <h3>Verbundene Instrumente</h3>
            {links.map((i) => (
              <button key={i.symbol} onClick={() => onSelectSecurity(i)}>
                <strong>{i.symbol}</strong>
                <span>{i.name}</span>
                <small>
                  {assetLabels[i.asset_class] || i.asset_class} ·{" "}
                  {i.close == null
                    ? "Kurs noch nicht importiert"
                    : number(i.close) + " " + i.currency}
                </small>
              </button>
            ))}
            <p className="note">
              ETF- und ETC-Verbindungen folgen dem Basiswert. Bei breiten
              Aktien-ETFs wird ohne Holdings-Daten keine direkte Kakao- oder
              Gold-Exposition angenommen.
            </p>
          </section>
        </div>
        <div>
          {driver === "cocoa" && (
            <section className="weather-panel">
              <h3>{data.regions[region]?.name}</h3>
              <div className="weather-stats">
                {[
                  ["rain_30d", "Niederschlag · 30 Tage", "mm"],
                  ["dry_spell_30d", "Längste Trockenphase", "Tage"],
                  ["heat_days_30d", "Tage mit Tmax ≥ 35 °C", "Tage"],
                ].map(([metric, label, unit]) => (
                  <div key={metric}>
                    <span>{label}</span>
                    <strong>
                      {number(find(metric)?.value)} <small>{unit}</small>
                    </strong>
                  </div>
                ))}
              </div>
              <p className="muted">
                {regionRows.length
                  ? `Fensterende: ${regionRows[0].date} · 7 Tage Reanalyse-Verzug`
                  : "Noch keine Wetterdaten. „Wetter abrufen“ lädt eine 30-Tage-Reanalyse."}
              </p>
              {weatherHistory.length > 1 && (
                <div className="weather-history">
                  <h3>
                    Niederschlag im Verlauf <small>mm / Tag</small>
                  </h3>
                  <svg
                    viewBox="0 0 460 105"
                    role="img"
                    aria-label="Täglicher Niederschlag der beobachteten Region"
                  >
                    {weatherHistory.map((r, i) => {
                      const max = Math.max(
                        1,
                        ...weatherHistory.map((h) => h.value),
                      );
                      const width = 420 / weatherHistory.length;
                      return (
                        <rect
                          key={r.date}
                          x={20 + i * width}
                          y={80 - (r.value / max) * 65}
                          width={Math.max(1, width - 1)}
                          height={(r.value / max) * 65}
                          fill="#58d5de"
                        >
                          <title>
                            {r.date}: {number(r.value)} mm
                          </title>
                        </rect>
                      );
                    })}
                    <text x="20" y="100" fill="#91a8c0" fontSize="10">
                      {weatherHistory[0].date}
                    </text>
                    <text
                      x="440"
                      y="100"
                      textAnchor="end"
                      fill="#91a8c0"
                      fontSize="10"
                    >
                      {weatherHistory.at(-1).date}
                    </text>
                  </svg>
                </div>
              )}
              <p className="note">
                Trockenphase: aufeinanderfolgende Tage mit &lt; 1 mm
                Niederschlag. Kein validierter Dürrealarm. SPI/SPEI benötigt
                eine langfristige Referenz. 35 °C ist eine beschreibende
                Schwelle, kein kalibriertes Ertragsmodell.
              </p>
              <p className="attribution">
                <a
                  href="https://open-meteo.com/"
                  target="_blank"
                  rel="noreferrer"
                >
                  Weather data by Open-Meteo.com
                </a>{" "}
                · ERA5
                <br />
                <a
                  href="https://creativecommons.org/licenses/by/4.0/"
                  target="_blank"
                  rel="noreferrer"
                >
                  CC BY 4.0
                </a>{" "}
                · aus Tagesdaten aggregiert
              </p>
            </section>
          )}
          <section className="events">
            <h3>Wetter- & Konfliktmeldungen</h3>
            <p className="muted">
              GDELT · letzte 7 Tage · Suchtreffer, nicht verifizierte
              Konfliktereignisse. Datum = erste Erfassung durch GDELT.
            </p>
            {data.events
              .filter((e) => e.driver === driver)
              .map((e) => (
                <article key={e.id}>
                  <a href={e.url} target="_blank" rel="noreferrer">
                    {e.title}
                  </a>
                  <small>
                    {new Date(e.date).toLocaleString("de-DE")} · {e.source}
                  </small>
                </article>
              ))}
            {!data.events.some((e) => e.driver === driver) && (
              <p className="empty">
                Keine Meldungen gespeichert. Ein leerer Feed bedeutet nicht,
                dass keine Risiken bestehen.
              </p>
            )}
          </section>
        </div>
      </div>
      <div className="feed-health">
        <h3>Feed-Zustand</h3>
        {data.feeds.map((f) => (
          <div key={f.feed}>
            <strong>
              {f.feed === "weather"
                ? "Wetter"
                : f.feed === "commodities"
                  ? "Rohstoffreferenzen"
                  : "Meldungen"}
            </strong>
            <span className={f.status === "ok" ? "positive" : "negative"}>
              {f.status}
            </span>
            <span>{f.message || "Abruf läuft"}</span>
            <small>
              Letzter Erfolg:{" "}
              {f.last_success
                ? new Date(f.last_success).toLocaleString("de-DE")
                : "noch keiner"}
            </small>
          </div>
        ))}
        {!data.feeds.length && (
          <p className="muted">
            Noch kein Abruf. Die externen Quellen werden nur auf Anforderung
            oder über den eingerichteten Job geladen.
          </p>
        )}
      </div>
    </section>
  );
}
