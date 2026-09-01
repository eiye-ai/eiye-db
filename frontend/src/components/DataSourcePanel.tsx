import { useEffect, useState } from "react";
import { api } from "../api";
import type { DataSource, QueryResponse, Schema } from "../types";
import { TYPE_LABELS, isObjectStore, isSql } from "../types";

interface Props {
  source: DataSource;
  onChanged: () => void;
  onEdit: () => void;
  onDeleted: () => void;
}

export default function DataSourcePanel({ source, onChanged, onEdit, onDeleted }: Props) {
  const [schema, setSchema] = useState<Schema | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const [table, setTable] = useState("");
  const [sql, setSql] = useState("");
  const [path, setPath] = useState("");
  const [limit, setLimit] = useState(20);
  const [result, setResult] = useState<QueryResponse | null>(null);

  useEffect(() => {
    setSchema(null);
    setResult(null);
    setMsg(null);
    setTable("");
    setSql("");
    setPath("");
    api
      .schema(source.id)
      .then(setSchema)
      .catch(() => setSchema(null));
  }, [source.id]);

  async function run(action: string, fn: () => Promise<void>) {
    setBusy(action);
    setMsg(null);
    try {
      await fn();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  const testConn = () =>
    run("test", async () => {
      const ds = await api.test(source.id);
      setMsg({ kind: "ok", text: `Connection ${ds.status}.` });
      onChanged();
    });

  const discover = () =>
    run("discover", async () => {
      const s = await api.discover(source.id);
      setSchema(s);
      setMsg({ kind: "ok", text: `Discovered ${s.tables.length} table(s).` });
      onChanged();
    });

  const del = () =>
    run("delete", async () => {
      if (!window.confirm(`Delete “${source.name}”? This removes the registration, not your data.`)) return;
      await api.remove(source.id);
      onDeleted();
    });

  function buildRequest(): Record<string, unknown> | null {
    if (isSql(source.type)) {
      if (!sql.trim()) {
        setMsg({ kind: "err", text: "Enter a SQL query." });
        return null;
      }
      return { sql: sql.trim() };
    }
    const p = (isObjectStore(source.type) ? table || path : path).trim();
    if (!p) {
      setMsg({ kind: "err", text: source.type === "s3" ? "Enter an object key." : "Enter a path to query." });
      return null;
    }
    // S3 keys are opaque strings resolved against the configured prefix, not
    // paths resolved against a root, so the connector names the field `key`.
    return source.type === "s3" ? { key: p } : { path: p };
  }

  const runQuery = () =>
    run("query", async () => {
      const request = buildRequest();
      if (!request) return;
      const res = await api.query({ datasource_id: source.id, request, limit });
      setResult(res);
    });

  return (
    <div className="panel">
      <div className="card">
        <div className="panel-head">
          <div>
            <h2>{source.name}</h2>
            <div className="meta">
              <span className={`badge status-${source.status}`}>{source.status}</span>
              <span className="badge type">{TYPE_LABELS[source.type]}</span>
            </div>
          </div>
          <div className="row">
            <button onClick={onEdit}>Edit</button>
            <button className="danger" onClick={del} disabled={busy === "delete"}>
              Delete
            </button>
          </div>
        </div>

        <dl className="config">
          {Object.entries(source.config).map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd>
            </div>
          ))}
        </dl>

        <div className="row">
          <button onClick={testConn} disabled={busy === "test"}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </button>
          <button onClick={discover} disabled={busy === "discover"}>
            {busy === "discover" ? "Discovering…" : "Discover schema"}
          </button>
        </div>
        {msg && <div className={msg.kind === "ok" ? "ok" : "error"}>{msg.text}</div>}
      </div>

      {schema && (
        <div className="card">
          <h3>
            Schema <span className="hint">discovered {new Date(schema.discovered_at).toLocaleString()}</span>
          </h3>
          {schema.tables.length === 0 && <p className="hint">No tables found.</p>}
          {schema.tables.map((t) => (
            <details key={t.name}>
              <summary>
                {t.name} <span className="hint">({t.fields.length} cols)</span>
              </summary>
              <div className="cols">
                {t.fields.map((f) => (
                  <span key={f.name} className="col">
                    <b>{f.name}</b> {f.type}
                  </span>
                ))}
              </div>
            </details>
          ))}
          {schema.relationships && schema.relationships.length > 0 && (
            <>
              <h3 style={{ marginTop: 14 }}>
                Relationships <span className="hint">approved = ground truth · candidate = unreviewed</span>
              </h3>
              {schema.relationships.map((r) => (
                <div key={r.id} className="review-row settled">
                  <span className="join">
                    {r.from_table}.{r.from_column} <span className="arrow">↔</span> {r.to_table}.{r.to_column}
                  </span>
                  <span className={`badge status-${r.status === "approved" ? "connected" : "discovered"}`}>
                    {r.status}
                  </span>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      <div className="card">
        <h3>
          Query <span className="hint">read-only · PII redacted · audited</span>
        </h3>
        {isSql(source.type) ? (
          <textarea value={sql} onChange={(e) => setSql(e.target.value)} rows={3} placeholder="SELECT * FROM customers" />
        ) : isObjectStore(source.type) ? (
          <div className="row">
            {schema && schema.tables.length > 0 && (
              <select
                value={table}
                onChange={(e) => {
                  setTable(e.target.value);
                  setPath("");
                }}
              >
                <option value="">{source.type === "s3" ? "— pick an object —" : "— pick a file —"}</option>
                {schema.tables.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            )}
            <input
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                setTable("");
              }}
              placeholder={source.type === "s3" ? "or type a key under the prefix" : "or type a relative path"}
            />
          </div>
        ) : (
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/endpoint" />
        )}
        <div className="row">
          <label className="inline">
            Limit
            <input
              type="number"
              min={1}
              max={1000}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 1)}
            />
          </label>
          <button className="primary" onClick={runQuery} disabled={busy === "query"}>
            {busy === "query" ? "Running…" : "Run query"}
          </button>
        </div>

        {result && <QueryResult result={result} />}
      </div>
    </div>
  );
}

function QueryResult({ result }: { result: QueryResponse }) {
  const cols = Array.from(new Set(result.rows.flatMap((r) => Object.keys(r))));
  const redactions = Object.entries(result.pii_counts);
  return (
    <div className="result">
      <div className="result-meta">
        <span>{result.row_count} row(s)</span>
        <span>{result.execution_time_ms.toFixed(1)} ms</span>
        {result.pii_filtered && (
          <span className="badge redacted">
            PII redacted{redactions.length ? `: ${redactions.map(([k, v]) => `${v} ${k}`).join(", ")}` : ""}
          </span>
        )}
      </div>
      {result.rows.length === 0 ? (
        <p className="hint">No rows.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {cols.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((r, i) => (
                <tr key={i}>
                  {cols.map((c) => {
                    const v = r[c];
                    const s = v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v);
                    return (
                      <td key={c} title={s}>
                        {s.length > 160 ? s.slice(0, 160) + "…" : s}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
