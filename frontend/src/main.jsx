import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  Star,
  Database,
  RefreshCw,
  Search,
  LogOut,
  Clock,
  Eye,
  Globe,
} from "lucide-react";
import { api, json, layers } from "./api";
import Market from "./Market";
import Inspector from "./Inspector";
import Operations from "./Operations";
import Drivers from "./Drivers";
import "./style.css";

function App() {
  const [authenticated, setAuthenticated] = useState(null),
    [token, setToken] = useState(""),
    [items, setItems] = useState([]),
    [status, setStatus] = useState(null),
    [context, setContext] = useState(null),
    [view, setView] = useState("market"),
    [query, setQuery] = useState(""),
    [selected, setSelected] = useState(null),
    [history, setHistory] = useState([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [enabled, setEnabled] = useState({
      price: true,
      lexicon: false,
      finbert: false,
      llm: false,
    });
  const handleError = useCallback((e) => {
    setError(e.message);
    if (e.status === 401) setAuthenticated(false);
  }, []);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [market, operation, drivers] = await Promise.all([
        api("/market"),
        api("/status"),
        api("/context"),
      ]);
      setContext(drivers);
      setItems(market.securities);
      setStatus(operation);
      setAuthenticated(true);
      setSelected((old) =>
        old
          ? market.securities.find((i) => i.symbol === old.symbol) || null
          : null,
      );
    } catch (e) {
      handleError(e);
    } finally {
      setLoading(false);
    }
  }, [handleError]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!authenticated) return;
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 60000);
    return () => clearInterval(timer);
  }, [authenticated, load]);
  useEffect(() => {
    let canceled = false;
    setHistory([]);
    if (selected)
      api(`/securities/${encodeURIComponent(selected.symbol)}`)
        .then((r) => {
          if (!canceled) setHistory(r.history);
        })
        .catch(handleError);
    return () => {
      canceled = true;
    };
  }, [selected?.symbol, selected?.imported_at, handleError]);
  async function login(e) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      await api("/session", json("POST", { token }));
      setToken("");
      await load();
    } catch (e) {
      handleError(e);
    } finally {
      setLoading(false);
    }
  }
  async function watch(item) {
    try {
      await api(
        `/watchlist/${encodeURIComponent(item.symbol)}`,
        json("PUT", { watched: !item.watched }),
      );
      await load();
    } catch (e) {
      handleError(e);
    }
  }
  async function logout() {
    try {
      await api("/session", { method: "DELETE" });
      setItems([]);
      setSelected(null);
      setAuthenticated(false);
      setError("");
    } catch (e) {
      handleError(e);
    }
  }
  if (authenticated === null)
    return (
      <div className="login">
        <h1>
          SECURITIES <span>EYE</span>
        </h1>
        <p>Konsole wird geladen …</p>
        {error && <p role="alert">{error}</p>}
      </div>
    );
  if (!authenticated)
    return (
      <main className="login">
        <Eye size={36} />
        <h1>
          SECURITIES <span>EYE</span>
        </h1>
        <p className="muted">Dein Lagebild für Wertpapiere.</p>
        <form onSubmit={login}>
          <label>
            Zugangsschlüssel
            <input
              type="password"
              required
              autoComplete="current-password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <button className="primary" disabled={loading}>
            {loading ? "Verbinden …" : "Konsole öffnen"}
          </button>
        </form>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </main>
    );
  const filtered = items.filter(
    (i) =>
      (view !== "watchlist" || i.watched) &&
      `${i.symbol} ${i.name}`.toLowerCase().includes(query.toLowerCase()),
  );
  const last = items.reduce(
    (date, i) => (i.date && i.date > date ? i.date : date),
    "",
  );
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          SECURITIES <span>EYE</span>
          <small>Wertpapier-Konsole</small>
        </div>
        <nav aria-label="Hauptnavigation">
          {[
            ["market", "Lagebild", Activity],
            ["watchlist", "Watchlist", Star],
            ["drivers", "Treiber & Ereignisse", Globe],
            ["operations", "Daten & Betrieb", Database],
          ].map(([key, label, Icon]) => (
            <button
              key={key}
              className={view === key ? "active" : ""}
              onClick={() => setView(key)}
            >
              <Icon size={19} />
              {label}
            </button>
          ))}
        </nav>
        <div className="layer-panel">
          <h2>Analyse-Layer</h2>
          {Object.entries(layers).map(([key, label]) => (
            <label key={key}>
              <input
                type="checkbox"
                checked={enabled[key]}
                onChange={(e) =>
                  setEnabled({ ...enabled, [key]: e.target.checked })
                }
              />
              {label}
            </label>
          ))}
          <p>
            Scores je Modell.
            <br />
            Keine automatische Fusion.
          </p>
        </div>
        <button className="logout" onClick={logout}>
          <LogOut size={17} /> Abmelden
        </button>
      </aside>
      <main className="workspace">
        <header>
          <h1>
            {view === "watchlist"
              ? "Watchlist"
              : view === "operations"
                ? "Daten & Betrieb"
                : view === "drivers"
                  ? "Treiber & Ereignisse"
                  : "Marktübersicht"}
          </h1>
          {!["operations", "drivers"].includes(view) && (
            <label className="search">
              <Search size={18} />
              <input
                aria-label="Wertpapier suchen"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Wertpapier suchen (Name, Ticker …)"
              />
            </label>
          )}
          <button className="primary refresh" disabled={loading} onClick={load}>
            <RefreshCw size={17} className={loading ? "spin" : ""} />
            {loading ? "Lädt …" : "Aktualisieren"}
          </button>
        </header>
        {error && (
          <div className="error" role="alert">
            {error}
            <button
              onClick={() => setError("")}
              aria-label="Fehlermeldung schließen"
            >
              Schließen
            </button>
          </div>
        )}
        {view === "drivers" ? (
          <Drivers
            data={context}
            items={items}
            onRefresh={load}
            onError={handleError}
            onSelectSecurity={(item) => {
              setSelected(item);
              setView("market");
            }}
          />
        ) : view === "operations" ? (
          <Operations status={status} onImported={load} onError={handleError} />
        ) : (
          <div className="content-grid">
            <div className="overview">
              <div className="historical">
                <Clock size={19} />
                <strong>Beobachteter Datenbestand · Kein Live-Feed</strong>
                <button onClick={() => setView("operations")}>
                  Quellen prüfen
                </button>
              </div>
              <div className="summary">
                <div>
                  <span>Wertpapiere</span>
                  <strong>{items.length.toLocaleString("de-DE")}</strong>
                  <small>
                    aus{" "}
                    {new Set(items.map((i) => i.country).filter(Boolean)).size}{" "}
                    Ländern
                  </small>
                </div>
                <div>
                  <span>Beobachtet</span>
                  <strong>{items.filter((i) => i.watched).length}</strong>
                  <small>in aktueller Watchlist</small>
                </div>
                <div>
                  <span>Letzter Datenstand</span>
                  <strong className="date">{last || "Keine Daten"}</strong>
                  <small>
                    {last
                      ? "Beobachtungsdatum, kein Importdatum"
                      : "CSV oder Forschungsdaten importieren"}
                  </small>
                </div>
              </div>
              <Market
                items={filtered}
                selected={selected}
                onSelect={setSelected}
                onWatch={watch}
                enabled={enabled}
              />
            </div>
            <Inspector
              item={selected}
              history={history}
              onWatch={watch}
              onClose={() => setSelected(null)}
            />
          </div>
        )}
        <footer>
          <Database size={17} />
          <strong>Quellen & Aktualität</strong>
          <span>
            {status?.imports.length
              ? `Letzter Import: ${status.imports[0].source}`
              : "Noch kein Import"}
          </span>
          <span>Kein Live-Feed · Analyse, keine Handelsausführung</span>
        </footer>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
