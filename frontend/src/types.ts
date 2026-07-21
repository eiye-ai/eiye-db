export type DataSourceType = "filesystem" | "postgresql" | "rest_api";

export interface DataSource {
  id: string;
  name: string;
  type: DataSourceType;
  status: string;
  config: Record<string, unknown>;
  description: string;
  tags: string[];
  pii_risk_level: string;
  created_at: string;
  updated_at: string;
  last_connected: string | null;
}

export interface SchemaField {
  name: string;
  type: string;
}

export interface SchemaTable {
  name: string;
  fields: SchemaField[];
}

export interface Relationship {
  id: string;
  from_datasource_id: string;
  from_table: string;
  from_column: string;
  to_datasource_id: string;
  to_table: string;
  to_column: string;
  kind: "foreign_key" | "candidate_join";
  source: "structural" | "heuristic" | "proposed";
  status: "approved" | "candidate" | "rejected";
  confidence: number;
  rationale: string;
}

export interface Schema {
  datasource_id: string;
  tables: SchemaTable[];
  discovered_at: string;
  relationships?: Relationship[];
}

export interface QueryResponse {
  datasource_id: string;
  rows: Record<string, unknown>[];
  row_count: number;
  pii_filtered: boolean;
  pii_counts: Record<string, number>;
  execution_time_ms: number;
  lineage?: Record<string, unknown>;
}

export interface Metric {
  id: string;
  name: string;
  description: string;
  datasource_id: string;
  request_template: Record<string, unknown>;
  params: Record<string, { type: "string" | "number"; default?: unknown }>;
  source: "human" | "proposed";
  status: "approved" | "candidate" | "rejected";
}

export const TYPE_LABELS: Record<DataSourceType, string> = {
  filesystem: "Filesystem",
  postgresql: "PostgreSQL",
  rest_api: "REST API",
};
