import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DataSource, Metric, Relationship } from "../types";

interface Props {
  sources: DataSource[];
}

export default function SemanticPanel({ sources }: Props) {
  const [rels, setRels] = useState<Relationship[]>([]);
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const dsName = (id: string) => sources.find((s) => s.id === id)?.name ?? id.slice(0, 8);
  const endpoint = (r: Relationship, side: "from" | "to") =>
    `${dsName(r[`${side}_datasource_id`])} / ${r[`${side}_table`]}.${r[`${side}_column`]}`;

  const refresh = useCallback(async () => {
    try {
      const [r, m] = await Promise.all([api.relationships(), api.metrics()]);
      setRels(r);
      setMetrics(m);
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(action: string, fn: () => Promise<unknown>, okText?: string) {
    setBusy(action);
    setMsg(null);
    try {
      await fn();
      if (okText) setMsg({ kind: "ok", text: okText });
      await refresh();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  const detect = () =>
    run("detect", async () => {
      const created = await api.detect();
      setMsg({
        kind: "ok",
        text: created.length ? `Detected ${created.length} new candidate(s).` : "No new candidates found.",
      });
    });

  const candidates = rels.filter((r) => r.status === "candidate");
  const decided = rels.filter((r) => r.status !== "candidate");
  const metricCandidates = metrics.filter((m) => m.status === "candidate");
  const metricDecided = metrics.filter((m) => m.status !== "candidate");

  return (
    <div className="panel">
      <div className="card">
        <div className="panel-head">
          <div>
            <h2>Semantic model</h2>
            <p className="hint">
              AI drafts → human governs → engine enforces. Approving requires the admin API key; only approved
              entries become ground truth for agents.
            </p>
          </div>
          <button onClick={detect} disabled={busy === "detect"}>
            {busy === "detect" ? "Detecting…" : "Detect candidates"}
          </button>
        </div>
        {msg && <div className={msg.kind === "ok" ? "ok" : "error"}>{msg.text}</div>}
      </div>

      <div className="card">
        <h3>
          Relationships <span className="hint">{candidates.length} awaiting review</span>
        </h3>
        {rels.length === 0 && <p className="hint">None yet. Discover schemas, then run detection.</p>}
        {candidates.map((r) => (
          <div key={r.id} className="review-row">
            <div className="review-main">
              <span className="join">
                {endpoint(r, "from")} <span className="arrow">↔</span> {endpoint(r, "to")}
              </span>
              <span className="hint">
                {r.source} · confidence {r.confidence} · {r.rationale}
              </span>
            </div>
            <div className="row">
              <button
                className="primary"
                disabled={busy === r.id}
                onClick={() => run(r.id, () => api.reviewRelationship(r.id, "approved"), "Approved.")}
              >
                Approve
              </button>
              <button
                className="danger"
                disabled={busy === r.id}
                onClick={() => run(r.id, () => api.reviewRelationship(r.id, "rejected"), "Rejected.")}
              >
                Reject
              </button>
            </div>
          </div>
        ))}
        {decided.map((r) => (
          <div key={r.id} className="review-row settled">
            <div className="review-main">
              <span className="join">
                {endpoint(r, "from")} <span className="arrow">↔</span> {endpoint(r, "to")}
              </span>
              <span className="hint">
                {r.kind} · {r.source} · confidence {r.confidence}
              </span>
            </div>
            <span className={`badge status-${r.status === "approved" ? "connected" : "error"}`}>{r.status}</span>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>
          Metrics <span className="hint">{metricCandidates.length} awaiting review</span>
        </h3>
        {metrics.length === 0 && (
          <p className="hint">None yet. Agents can propose metrics over MCP; admins can create them via the API.</p>
        )}
        {metricCandidates.map((m) => (
          <div key={m.id} className="review-row">
            <div className="review-main">
              <span className="join">
                {m.name} <span className="hint">on {dsName(m.datasource_id)}</span>
              </span>
              <span className="hint">{m.description || "(no description)"}</span>
              <code className="template">{JSON.stringify(m.request_template)}</code>
            </div>
            <div className="row">
              <button
                className="primary"
                disabled={busy === m.id}
                onClick={() => run(m.id, () => api.reviewMetric(m.id, "approved"), "Approved.")}
              >
                Approve
              </button>
              <button
                className="danger"
                disabled={busy === m.id}
                onClick={() => run(m.id, () => api.reviewMetric(m.id, "rejected"), "Rejected.")}
              >
                Reject
              </button>
            </div>
          </div>
        ))}
        {metricDecided.map((m) => (
          <div key={m.id} className="review-row settled">
            <div className="review-main">
              <span className="join">
                {m.name} <span className="hint">on {dsName(m.datasource_id)}</span>
              </span>
              <span className="hint">{m.description}</span>
            </div>
            <div className="row">
              <span className={`badge status-${m.status === "approved" ? "connected" : "error"}`}>{m.status}</span>
              <button
                disabled={busy === m.id}
                onClick={() => run(m.id, () => api.removeMetric(m.id), "Deleted.")}
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
