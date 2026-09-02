export type DataSourceType =
  | "filesystem"
  | "postgresql"
  | "mysql"
  | "sqlserver"
  | "sqlite"
  | "s3"
  | "rest_api";

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

/** One source's line in an access review: what this subject may actually do. */
export interface AccessRow {
  datasource_id: string;
  name: string;
  read: boolean;
  discover: boolean;
  masked_columns: string[];
}

export interface AccessReview {
  key_id: string;
  /** Which setting configures this subject, or "none" — an MCP client asserts
   * its own EIYE_KEY_ID, so a subject with no credential behind it is normal. */
  credential: string;
  is_admin: boolean;
  default_deny: boolean;
  dev_mode: boolean;
  datasources: AccessRow[];
}

export interface Policy {
  id: string;
  name: string;
  description: string;
  effect: "allow" | "deny";
  resource_type: string;
  resource_id: string;
  actions: string[];
  subjects: string[];
  conditions: Record<string, unknown> | null;
  created_at: string;
}

export const TYPE_LABELS: Record<DataSourceType, string> = {
  filesystem: "Filesystem",
  postgresql: "PostgreSQL",
  mysql: "MySQL / MariaDB",
  sqlserver: "SQL Server",
  sqlite: "SQLite",
  s3: "S3 / MinIO",
  rest_api: "REST API",
};

// What a source is queried *with*, which is not the same question as what it
// is. Both groups have more than one member now, so asking by group keeps the
// next connector from having to be added to four `||` chains.
const SQL_TYPES: DataSourceType[] = ["postgresql", "mysql", "sqlserver", "sqlite"];
const OBJECT_TYPES: DataSourceType[] = ["filesystem", "s3"];

/** Queried with SQL text. */
export const isSql = (type: DataSourceType) => SQL_TYPES.includes(type);

/** Queried by naming one document out of a discovered listing. */
export const isObjectStore = (type: DataSourceType) => OBJECT_TYPES.includes(type);
