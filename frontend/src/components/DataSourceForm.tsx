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
  const [dbPath, setDbPath] = useState((existing?.config.path as string) ?? "");
  const [bucket, setBucket] = useState((existing?.config.bucket as string) ?? "");
  const [prefix, setPrefix] = useState((existing?.config.prefix as string) ?? "");
  const [endpointUrl, setEndpointUrl] = useState((existing?.config.endpoint_url as string) ?? "");
  const [region, setRegion] = useState((existing?.config.region as string) ?? "");
  const [accessKeyId, setAccessKeyId] = useState((existing?.config.access_key_id as string) ?? "");
  const [secretAccessKey, setSecretAccessKey] = useState((existing?.config.secret_access_key as string) ?? "");
  const [baseUrl, setBaseUrl] = useState((existing?.config.base_url as string) ?? "");
  const [email, setEmail] = useState((existing?.config.email as string) ?? "");
  const [apiToken, setApiToken] = useState((existing?.config.api_token as string) ?? "");
  // Confluence calls it a space and Jira calls it a project; a source is never
  // both, so one field carries whichever this type uses.
  const [username, setUsername] = useState((existing?.config.username as string) ?? "");
  const [tenantId, setTenantId] = useState((existing?.config.tenant_id as string) ?? "");
  const [clientId, setClientId] = useState((existing?.config.client_id as string) ?? "");
  const [clientSecret, setClientSecret] = useState((existing?.config.client_secret as string) ?? "");
  const [siteUrl, setSiteUrl] = useState((existing?.config.site_url as string) ?? "");
  const [library, setLibrary] = useState((existing?.config.library as string) ?? "");
  const [folder, setFolder] = useState((existing?.config.folder as string) ?? "");
  const [password, setPassword] = useState((existing?.config.password as string) ?? "");
  const [tables, setTables] = useState(
    Array.isArray(existing?.config.tables) ? (existing?.config.tables as string[]).join(", ") : "",
  );
  const [scopeKey, setScopeKey] = useState(
    ((existing?.config.space_key ?? existing?.config.project_key) as string) ?? "",
  );
  const [headers, setHeaders] = useState(
    existing?.config.headers ? JSON.stringify(existing.config.headers, null, 2) : "",
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function buildConfig(): Record<string, unknown> | null {
    if (type === "filesystem") return { root: root.trim() };
    if (type === "sqlite") return { path: dbPath.trim() };
    if (type === "postgresql" || type === "mysql" || type === "sqlserver" || type === "oracle")
      return { dsn: dsn.trim() };
    if (type === "s3") {
      const cfg: Record<string, unknown> = { bucket: bucket.trim() };
      // Omitted rather than sent empty: the connector reads an absent key pair
      // as "use the ambient AWS credential chain", and empty strings would
      // instead look like a half-filled credential and be rejected.
      if (prefix.trim()) cfg.prefix = prefix.trim();
      if (endpointUrl.trim()) cfg.endpoint_url = endpointUrl.trim();
      if (region.trim()) cfg.region = region.trim();
      if (accessKeyId.trim() || secretAccessKey.trim()) {
        cfg.access_key_id = accessKeyId.trim();
        cfg.secret_access_key = secretAccessKey.trim();
      }
      return cfg;
    }
    if (type === "servicenow") {
      // `tables` has no default on purpose — an instance carries thousands, so
      // the operator names the ones this datasource may read. Sent as a list
      // rather than the raw string, which is the shape the connector validates.
      const named = tables.split(",").map((t) => t.trim()).filter(Boolean);
      if (named.length === 0) {
        setError("Name at least one table — a ServiceNow datasource has no “all tables” mode.");
        return null;
      }
      return {
        base_url: baseUrl.trim(),
        username: username.trim(),
        password: password.trim(),
        tables: named,
      };
    }
    if (type === "sharepoint") {
      const cfg: Record<string, unknown> = {
        tenant_id: tenantId.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
        site_url: siteUrl.trim(),
      };
      // Both omitted rather than sent empty: the connector defaults the library
      // to "Documents", and an empty folder means the whole library, which is
      // not the same thing as a folder literally named "".
      if (library.trim()) cfg.library = library.trim();
      if (folder.trim()) cfg.folder = folder.trim();
      return cfg;
    }
    if (type === "confluence" || type === "jira") {
      const cfg: Record<string, unknown> = {
        base_url: baseUrl.trim(),
        email: email.trim(),
        api_token: apiToken.trim(),
      };
      // Omitted rather than sent empty: an empty scope would read as one naming
      // nothing, and both connectors refuse that at test time.
      if (scopeKey.trim()) {
        cfg[type === "confluence" ? "space_key" : "project_key"] = scopeKey.trim();
      }
      return cfg;
    }
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

      {type === "oracle" && (
        <label>
          Connection string (DSN)
          <input
            value={dsn}
            onChange={(e) => setDsn(e.target.value)}
            placeholder="oracle://user:pass@host:1521/SERVICE"
          />
          <span className="hint">
            Must be a login that holds only read privileges — eiye reads the account&rsquo;s effective privileges on
            every connect and refuses anything else, including writes reaching it through a role or through PUBLIC.
            No Oracle Instant Client is needed.
          </span>
        </label>
      )}

      {type === "sqlite" && (
        <label>
          Database file
          <input value={dbPath} onChange={(e) => setDbPath(e.target.value)} placeholder="/absolute/path/to/app.db" />
          <span className="hint">
            Opened read-only (<code>mode=ro</code>), which SQLite enforces for writes and schema changes alike. The path
            must be absolute, and a file that isn’t there is an error rather than a new empty database.
          </span>
        </label>
      )}

      {type === "s3" && (
        <>
          <label>
            Bucket
            <input value={bucket} onChange={(e) => setBucket(e.target.value)} placeholder="my-exports" />
          </label>
          <label>
            Prefix (optional)
            <input value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="exports/" />
            <span className="hint">Bounds what this datasource exposes, the way a root does for a filesystem.</span>
          </label>
          <label>
            Endpoint URL (optional)
            <input
              value={endpointUrl}
              onChange={(e) => setEndpointUrl(e.target.value)}
              placeholder="http://minio.internal:9000"
            />
            <span className="hint">Set for MinIO or another S3-compatible server; leave empty for AWS.</span>
          </label>
          <label>
            Region (optional)
            <input value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us-east-1" />
          </label>
          <label>
            Access key ID (optional)
            <input value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="AKIA…" />
          </label>
          <label>
            Secret access key (optional)
            <input
              type="password"
              value={secretAccessKey}
              onChange={(e) => setSecretAccessKey(e.target.value)}
              placeholder="…"
            />
            <span className="hint">
              Leave both empty to use the host’s AWS credentials (instance role, profile, environment). eiye only ever
              calls ListObjectsV2 and GetObject — scope the key to those two anyway.
            </span>
          </label>
        </>
      )}

      {type === "servicenow" && (
        <>
          <label>
            Instance URL
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://acme.service-now.com"
            />
          </label>
          <label>
            Username
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="eiye_ro" />
          </label>
          <label>
            Password
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
            <span className="hint">
              Use a dedicated integration account with read roles only. It also needs read access to
              sys_db_object and sys_dictionary for schema discovery.
            </span>
          </label>
          <label>
            Tables
            <input
              value={tables}
              onChange={(e) => setTables(e.target.value)}
              placeholder="incident, change_request"
            />
            <span className="hint">
              Required, comma separated. Nothing outside this list can be discovered or queried — an
              instance has thousands of tables, so there is deliberately no “everything” option.
            </span>
          </label>
        </>
      )}

      {type === "sharepoint" && (
        <>
          <label>
            Site URL
            <input
              value={siteUrl}
              onChange={(e) => setSiteUrl(e.target.value)}
              placeholder="https://contoso.sharepoint.com/sites/finance"
            />
          </label>
          <label>
            Directory (tenant) ID
            <input value={tenantId} onChange={(e) => setTenantId(e.target.value)} placeholder="a GUID" />
          </label>
          <label>
            Client ID
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="a GUID" />
          </label>
          <label>
            Client secret
            <input type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
            <span className="hint">
              The app registration needs a <code>*.Selected</code> scope — eiye refuses a tenant-wide
              SharePoint credential. Consent alone grants nothing: an administrator must also run the
              matching <code>POST /permissions</code> to give this app a <code>read</code> role on the
              library.
            </span>
          </label>
          <label>
            Library
            <input value={library} onChange={(e) => setLibrary(e.target.value)} placeholder="Documents" />
          </label>
          <label>
            Folder
            <input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="reports" />
            <span className="hint">
              Optional, and the only thing bounding this datasource inside the library. Item-level
              SharePoint permissions are <strong>not</strong> applied — app-only access reads
              everything under the grant — so every file here is visible to any agent your ABAC policy
              allows.
            </span>
          </label>
        </>
      )}

      {(type === "confluence" || type === "jira") && (
        <>
          <label>
            Site URL
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-site.atlassian.net"
            />
            <span className="hint">Cloud only. Paste any page URL from the site — the host is all that is used.</span>
          </label>
          <label>
            Account email
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ops@example.com" />
          </label>
          <label>
            API token
            <input
              type="password"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="from id.atlassian.com"
            />
            <span className="hint">
              The token carries its account&rsquo;s own permissions and expires after a year. Give eiye an account that
              can see only what it should read.
            </span>
          </label>
          <label>
            {type === "confluence" ? "Space key (optional)" : "Project key (optional)"}
            <input value={scopeKey} onChange={(e) => setScopeKey(e.target.value)} placeholder="ENG" />
            <span className="hint">
              Confines discovery and every query to one {type === "confluence" ? "space" : "project"}, including by{" "}
              {type === "confluence" ? "page id" : "issue key"}. Leave empty to expose the whole site.
            </span>
          </label>
        </>
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
