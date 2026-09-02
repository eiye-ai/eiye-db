import { useCallback, useEffect, useState } from "react";
import { api, getApiKey, setApiKey } from "./api";
import type { DataSource } from "./types";
import { TYPE_LABELS } from "./types";
import DataSourceForm from "./components/DataSourceForm";
import DataSourcePanel from "./components/DataSourcePanel";
import SemanticPanel from "./components/SemanticPanel";
import AccessPanel from "./components/AccessPanel";

type Mode = "idle" | "new" | "edit";
type View = "sources" | "semantic" | "access";

export default function App() {
  const [view, setView] = useState<View>("sources");
  const [sources, setSources] = useState<DataSource[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("idle");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [key, setKey] = useState(getApiKey());

  const refresh = useCallback(async () => {
    try {
      setSources(await api.list());
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const selected = sources.find((s) => s.id === selectedId) ?? null;

  function saveKey() {
    setApiKey(key);
    refresh();
  }

  function onSaved(ds: DataSource) {
    setMode("idle");
    setSelectedId(ds.id);
    refresh();
  }

  return (
    <div className="app">
      <header>
        <h1>
          eiye_db <span>Semantic Surface</span>
        </h1>
        <nav className="tabs">
          <button className={view === "sources" ? "tab active" : "tab"} onClick={() => setView("sources")}>
            Datasources
          </button>
          <button className={view === "semantic" ? "tab active" : "tab"} onClick={() => setView("semantic")}>
            Semantic model
          </button>
          <button className={view === "access" ? "tab active" : "tab"} onClick={() => setView("access")}>
            Access
          </button>
        </nav>
        <div className="api-key">
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="API key (optional)"
          />
          <button onClick={saveKey}>Save</button>
        </div>
      </header>

      {view === "semantic" ? (
        <main className="single">
          <SemanticPanel sources={sources} />
        </main>
      ) : view === "access" ? (
        <main className="single">
          <AccessPanel sources={sources} />
        </main>
      ) : (
      <div className="layout">
        <aside>
          <div className="aside-head">
            <h2>Datasources</h2>
            <button
              className="primary"
              onClick={() => {
                setMode("new");
                setSelectedId(null);
              }}
            >
              + New
            </button>
          </div>
          {loadError && <div className="error">{loadError}</div>}
          {sources.length === 0 && !loadError && <p className="hint">None yet. Add one to get started.</p>}
          <ul className="ds-list">
            {sources.map((s) => (
              <li
                key={s.id}
                className={s.id === selectedId ? "active" : ""}
                onClick={() => {
                  setSelectedId(s.id);
                  setMode("idle");
                }}
              >
                <span className="ds-name">{s.name}</span>
                <span className="ds-sub">
                  {TYPE_LABELS[s.type]} · <span className={`dot status-${s.status}`} />
                  {s.status}
                </span>
              </li>
            ))}
          </ul>
        </aside>

        <main>
          {mode === "new" && <DataSourceForm existing={null} onSaved={onSaved} onCancel={() => setMode("idle")} />}
          {mode === "edit" && selected && (
            <DataSourceForm existing={selected} onSaved={onSaved} onCancel={() => setMode("idle")} />
          )}
          {mode === "idle" && selected && (
            <DataSourcePanel
              source={selected}
              onChanged={refresh}
              onEdit={() => setMode("edit")}
              onDeleted={() => {
                setSelectedId(null);
                refresh();
              }}
            />
          )}
          {mode === "idle" && !selected && (
            <div className="empty">
              <p>
                Select a datasource, or{" "}
                <button className="link" onClick={() => setMode("new")}>
                  add a new one
                </button>
                .
              </p>
            </div>
          )}
        </main>
      </div>
      )}
    </div>
  );
}
