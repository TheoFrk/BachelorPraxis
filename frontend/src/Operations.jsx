import { useState } from "react";
import { api } from "./api";
export default function Operations({ status, onImported, onError }) {
  const [kind, setKind] = useState("prices"),
    [source, setSource] = useState(""),
    [file, setFile] = useState(null),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  async function upload(e) {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      if (file.size > 10 * 1024 * 1024)
        throw new Error("Maximal 10 MiB pro Datei");
      const result = await api(
        `/import?kind=${kind}&source=${encodeURIComponent(source)}`,
        { method: "POST", headers: { "Content-Type": "text/csv" }, body: file },
      );
      setMessage(
        `${result.imported_rows.toLocaleString("de-DE")} Zeilen importiert.`,
      );
      await onImported();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="operations">
      <h2>Daten & Betrieb</h2>
      <p className="muted">
        Quellen kontrollieren. Daten nachvollziehbar aktualisieren.
      </p>
      <div className="operation-grid">
        <form onSubmit={upload}>
          <h3>CSV importieren</h3>
          <label>
            Datentyp
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="prices">Kursdaten</option>
              <option value="signals">Modell-Signale</option>
            </select>
          </label>
          <label>
            Quelle
            <input
              required
              maxLength={160}
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="Anbieter oder Datensatz"
            />
          </label>
          <label>
            CSV-Datei
            <input
              type="file"
              accept=".csv,text/csv"
              required
              onChange={(e) => setFile(e.target.files[0])}
            />
          </label>
          <p className="muted">
            {kind === "prices"
              ? "Spalten: symbol,date,close. Optional: name,country,sector,currency,asset_class,underlying,unit,frequency."
              : "Spalten: symbol,date,layer,score. Ebenen: price,lexicon,finbert,llm. Wertpapier muss bereits existieren."}
          </p>
          <p className="muted">
            UTF-8 · Komma-getrennt · Datum YYYY-MM-DD · Dezimalpunkt · maximal
            10 MiB / 100.000 Zeilen. Der Import ist atomar und wiederholbar.
          </p>
          <button
            className="primary"
            disabled={busy || !file || !source.trim()}
          >
            {busy ? "Import läuft …" : "Daten importieren"}
          </button>
          <p role="status" className="cyan">
            {message}
          </p>
        </form>
        <div>
          <h3>Betriebsstatus</h3>
          <dl className="facts">
            <dt>Speicherung</dt>
            <dd>SQLite · persistent</dd>
            <dt>Schema</dt>
            <dd>Version {status?.schema_version || "—"}</dd>
            <dt>Live-Feed</dt>
            <dd>Nicht verbunden</dd>
            <dt>Modelle</dt>
            <dd>Importierte Scores</dd>
          </dl>
          <p className="note">
            Ein aktueller Import ist kein Echtzeit-Feed. Datenalter und Quelle
            bleiben an jedem Wertpapier sichtbar. Die ursprünglichen
            Forschungsdaten werden nicht überschrieben.
          </p>
        </div>
      </div>
      <h3>Import-Protokoll</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Quelle</th>
              <th>Zeitpunkt</th>
              <th>Typ</th>
              <th>Zeilen</th>
            </tr>
          </thead>
          <tbody>
            {status?.imports.map((r) => (
              <tr key={r.id}>
                <td>{r.source}</td>
                <td>{new Date(r.imported_at).toLocaleString("de-DE")}</td>
                <td>{r.kind}</td>
                <td>{r.rows.toLocaleString("de-DE")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!status?.imports.length && (
        <p className="empty">Noch keine Daten importiert.</p>
      )}
    </section>
  );
}
