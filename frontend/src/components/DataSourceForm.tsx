import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { DataSource, DataSourceType } from "../types";
import { TYPE_LABELS } from "../types";

interface Props {
  existing: DataSource | null;
  onSaved: (ds: DataSource) => void;
  onCancel: () => void;
}

export default function DataSourceForm({ existing, onSaved, onCancel }: Props) {
  const editing = existing !== null;
  const [name, setName] = useState(existing?.name ?? "");
  const [type, setType] = useState<DataSourceType>(existing?.type ?? "filesystem");
  const [root, setRoot] = useState((existing?.config.root as string) ?? "");
  const [dsn, setDsn] = useState((existing?.config.dsn as string) ?? "");
  const [baseUrl, setBaseUrl] = useState((existing?.config.base_url as string) ?? "");
  const [headers, setHeaders] = useState(
    existing?.config.headers ? JSON.stringify(existing.config.headers, null, 2) : "",
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function buildConfig(): Record<string, unknown> | null {
    if (type === "filesystem") return { root: root.trim() };
    if (type === "postgresql" || type === "mysql" || type === "sqlserver") return { dsn: dsn.trim() };
    const cfg: Record<string, unknown> = { base_url: baseUrl.trim() };
    if (headers.trim()) {
      try {
        cfg.headers = JSON.parse(headers);
      } catch {
        setError('Headers must be valid JSON, e.g. {"Authorization": "Bearer …"}.');
        return null;
      }
    }
    return cfg;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    const config = buildConfig();
    if (config === null) return;
    setSaving(true);
    try {
      const ds = editing
        ? await api.update(existing.id, { name: name.trim(), config })
        : await api.create({ name: name.trim(), type, config });
      onSaved(ds);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>{editing ? `Edit “${existing.name}”` : "New datasource"}</h2>

      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. customer-exports" />
      </label>

      <label>
        Type
        <select value={type} onChange={(e) => setType(e.target.value as DataSourceType)} disabled={editing}>
          {(Object.keys(TYPE_LABELS) as DataSourceType[]).map((t) => (
            <option key={t} value={t}>
              {TYPE_LABELS[t]}
            </option>
          ))}
        </select>
        {editing && <span className="hint">Type can’t be changed after creation.</span>}
      </label>

      {type === "filesystem" && (
        <label>
          Root directory
          <input value={root} onChange={(e) => setRoot(e.target.value)} placeholder="/absolute/path/to/folder" />
          <span className="hint">Read-only. CSV, text, PDF, and XLSX files under this path are exposed.</span>
        </label>
      )}

      {type === "postgresql" && (
        <label>
          Connection string (DSN)
          <input
            value={dsn}
            onChange={(e) => setDsn(e.target.value)}
            placeholder="postgresql://user:pass@host:5432/db"
          />
          <span className="hint">Queries run in read-only transactions.</span>
        </label>
      )}

      {type === "mysql" && (
        <label>
          Connection string (DSN)
          <input value={dsn} onChange={(e) => setDsn(e.target.value)} placeholder="mysql://user:pass@host:3306/db" />
          <span className="hint">
            MySQL or MariaDB. Must be a login with no write privileges — eiye refuses to connect otherwise, because a
            MySQL read-only transaction does not stop DDL.
          </span>
        </label>
      )}

      {type === "sqlserver" && (
        <label>
          Connection string (DSN)
          <input
            value={dsn}
            onChange={(e) => setDsn(e.target.value)}
            placeholder="sqlserver://user:pass@host:1433/db"
          />
          <span className="hint">
            SQL auth only. Must be a login with no write permissions — SQL Server has no read-only transaction, so the
            login is the only thing holding, and eiye refuses to connect without it.
          </span>
        </label>
      )}

      {type === "rest_api" && (
        <>
          <label>
            Base URL
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com" />
            <span className="hint">GET-only; OpenAPI discovery when available.</span>
          </label>
          <label>
            Headers (optional JSON)
            <textarea
              value={headers}
              onChange={(e) => setHeaders(e.target.value)}
              rows={3}
              placeholder='{"Authorization": "Bearer …"}'
            />
          </label>
        </>
      )}

      {error && <div className="error">{error}</div>}

      <div className="row">
        <button type="submit" className="primary" disabled={saving}>
          {saving ? "Saving…" : editing ? "Save changes" : "Create datasource"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
