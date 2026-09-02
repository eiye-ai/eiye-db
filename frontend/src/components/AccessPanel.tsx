import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AccessReview, DataSource, Policy } from "../types";

interface Props {
  sources: DataSource[];
}

/** Mirrors scripts/grant.py, so the console and the CLI cannot leave the same
 * deployment with two naming conventions for the policies they both write. */
function grantName(subject: string, sourceLabel: string): string {
  return `allow-${subject}-${sourceLabel}`;
}

export default function AccessPanel({ sources }: Props) {
  const [subject, setSubject] = useState("");
  const [review, setReview] = useState<AccessReview | null>(null);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const loadPolicies = useCallback(async () => {
    try {
      setPolicies(await api.policies());
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    }
  }, []);

  useEffect(() => {
    loadPolicies();
  }, [loadPolicies]);

  async function run(action: string, fn: () => Promise<unknown>, okText?: string) {
    setBusy(action);
    setMsg(null);
    try {
      await fn();
      if (okText) setMsg({ kind: "ok", text: okText });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  const reviewed = review?.key_id ?? null;

  async function doReview(keyId: string) {
    if (!keyId.trim()) return;
    await run("review", async () => {
      setReview(await api.access(keyId.trim()));
    });
  }

  async function grant(resourceId: string, label: string) {
    if (!reviewed) return;
    await run(`grant:${resourceId}`, async () => {
      await api.createPolicy({
        name: grantName(reviewed, resourceId === "*" ? "all" : label),
        description: `Grant ${reviewed} read+discover on ${label}. Written from the operator console.`,
        effect: "allow",
        resource_id: resourceId,
        actions: ["read", "discover"],
        subjects: [reviewed],
      });
      setReview(await api.access(reviewed));
      await loadPolicies();
    }, `Granted read + discover on ${label} to ${reviewed}.`);
  }

  async function remove(policy: Policy) {
    await run(`del:${policy.id}`, async () => {
      await api.removePolicy(policy.id);
      await loadPolicies();
      // A deletion changes who can reach what, so the open review is stale.
      if (reviewed) setReview(await api.access(reviewed));
    }, `Deleted ${policy.name}.`);
  }

  const resourceLabel = (id: string) =>
    id === "*" ? "all sources" : sources.find((s) => s.id === id)?.name ?? id.slice(0, 8);

  return (
    <div className="panel">
      <div className="card">
        <div className="panel-head">
          <div>
            <h2>Access</h2>
            <p className="hint">
              Explicit deny beats explicit allow beats the default. Admins bypass policy entirely, because they
              are the ones who write it. Everything here needs the admin API key.
            </p>
          </div>
        </div>
        <div className="row subject-row">
          <input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doReview(subject)}
            placeholder="Key id to review, e.g. support-agent"
          />
          <button className="primary" onClick={() => doReview(subject)} disabled={busy === "review" || !subject.trim()}>
            {busy === "review" ? "Reviewing…" : "Review"}
          </button>
        </div>
        <p className="hint">
          A subject is an <code>EIYE_API_KEYS</code> entry, the reserved ids <code>primary</code> or{" "}
          <code>admin</code>, or whatever an MCP client puts in <code>EIYE_KEY_ID</code>.
        </p>
        {msg && <div className={msg.kind === "ok" ? "ok" : "error"}>{msg.text}</div>}
      </div>

      {review && (
        <div className="card">
          <div className="panel-head">
            <h3>
              {review.key_id}{" "}
              <span className={`badge ${review.is_admin ? "status-connected" : "type"}`}>
                {review.is_admin ? "admin" : "not admin"}
              </span>
            </h3>
            <button
              onClick={() => grant("*", "all sources")}
              disabled={review.is_admin || busy === "grant:*"}
              title={review.is_admin ? "Admins already bypass policy" : "Grant on every source, present and future"}
            >
              Grant all sources
            </button>
          </div>
          <p className="hint">
            Configured by <b>{review.credential === "none" ? "no credential" : review.credential}</b>
            {review.credential === "none" &&
              " — normal for an MCP subject, which asserts its own id rather than presenting a secret"}
            . Posture: <b>{review.default_deny ? "default-deny" : "allow-by-default"}</b>.
          </p>
          {review.dev_mode && (
            <div className="error">
              Open dev mode: no API keys are set, so every HTTP caller is an admin and bypasses everything below.
              The rows describe the MCP path only.
            </div>
          )}
          {review.is_admin && (
            <div className="ok">This subject is an admin. It bypasses ABAC, so every source reads as reachable.</div>
          )}
          {review.datasources.length === 0 && <p className="hint">No datasources registered yet.</p>}
          {review.datasources.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Read</th>
                    <th>Discover</th>
                    <th>Masked columns</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {review.datasources.map((d) => (
                    <tr key={d.datasource_id}>
                      <td>{d.name}</td>
                      <td>{d.read ? "yes" : "no"}</td>
                      <td>{d.discover ? "yes" : "no"}</td>
                      <td>{d.masked_columns.length ? d.masked_columns.join(", ") : "—"}</td>
                      <td>
                        {!review.is_admin && !(d.read && d.discover) && (
                          <button
                            disabled={busy === `grant:${d.datasource_id}`}
                            onClick={() => grant(d.datasource_id, d.name)}
                          >
                            Grant
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="card">
        <h3>
          Policies <span className="hint">{policies.length} total</span>
        </h3>
        {policies.length === 0 && (
          <p className="hint">
            None. Under allow-by-default that means everything is reachable; under default-deny it means nothing
            is, for every non-admin subject.
          </p>
        )}
        {policies.map((p) => (
          <div key={p.id} className="review-row settled">
            <div className="review-main">
              <span className="join">
                <span className={`badge ${p.effect === "allow" ? "status-connected" : "status-error"}`}>
                  {p.effect}
                </span>{" "}
                {p.name}
              </span>
              <span className="hint">
                {p.subjects.join(", ")} · {p.actions.join(" + ")} · {resourceLabel(p.resource_id)}
                {p.conditions?.columns ? ` · masks ${(p.conditions.columns as string[]).join(", ")}` : ""}
              </span>
            </div>
            <button className="danger" disabled={busy === `del:${p.id}`} onClick={() => remove(p)}>
              Delete
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
