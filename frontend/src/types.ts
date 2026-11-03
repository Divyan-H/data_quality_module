export type PhaseKey = "A" | "B" | "C" | "D" | "T";
export const PHASE_KEYS: PhaseKey[] = ["A", "B", "C", "D", "T"];

export type Status = "locked" | "ready" | "running" | "review" | "accepted" | "failed" | "cancelled";

export interface Artifact {
  id: string;
  label: string;
  kind: "review" | "data" | "audit" | "log" | "config";
  primary: boolean;
  description: string;
  bytes: number;
}

export interface LlmRecord {
  requested: "on" | "off";
  used: boolean | null;
  model: string | null;
  calls: number | null;
  reason: string | null;
}

export interface Problem {
  code?: string;
  sheet?: string;
  cell?: string;
  message: string;
}

export interface PhaseError {
  code?: string;
  message: string;
  problems?: Problem[];
  at?: string;
}

export interface ConfigState {
  uploaded: boolean;
  valid: boolean;
  name: string | null;
  table: string | null;
  preview: ConfigPreview | null;
  error: { message: string; problems?: Problem[] } | null;
}

export interface Phase {
  key: PhaseKey;
  title: string;
  short: string;
  describes: string;
  uses_llm: boolean;
  needs_config: boolean;
  /** May be skipped (Phase T). */
  optional?: boolean;
  supports_agent?: boolean;
  review_name: string;
  status: Status;
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  error: PhaseError | null;
  upload_error: PhaseError | null;
  uploads: { name: string; at: string; ok: boolean; applied?: Record<string, unknown> | null }[];
  llm: LlmRecord | null;
  config: ConfigState;
  can_continue: boolean;
  blocked_reason: string | null;
  has_summary: boolean;
  skipped: boolean;
  skip_reason: string | null;
  artifacts: Artifact[];
  /** Fix-with-agent: how many agent changes were applied / undone, and the last failure (if any). */
  agent?: { applied: number; undone: number; last_at: string | null } | null;
  agent_error?: PhaseError | null;
}

export interface Run {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  inputs: { path: string; bytes: number }[];
  options: { llm: "on" | "off" };
  job: { id: string; phase: PhaseKey; action: string; started_at: string } | null;
  phases: Phase[];
  complete: boolean;
}

export interface RunListItem {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  files: number;
  running: boolean;
  phases: Record<PhaseKey, Status>;
  complete: boolean;
}

export interface LlmStatus {
  available: boolean;
  url: string;
  model: string;
  reason: string | null;
  message: string;
  version: string | null;
  digest: string | null;
  installed: string[] | null;
}

export interface FixOption {
  id: string;
  what: string;
  default: boolean;
}

export interface ServiceOptions {
  llm: { model: string; review: "near_ties" | "all" };
  limits: { max_upload_mb: number; max_review_upload_mb: number; extensions: string[]; legacy_converter: boolean; legacy_extensions: string[] };
  hygiene_fixes: FixOption[];
  agent?: { phases: PhaseKey[]; llm: boolean; model: string | null };
}

/* ------------------------------------------------------------------ Fix-with-agent */

export type AgentMessageKind = "text" | "proposal" | "ask" | "answer" | "result" | "error";

export interface AgentMessage {
  id: number;
  role: "user" | "agent";
  kind: AgentMessageKind;
  text: string;
  changeset: string | null;
  at: string;
}

export interface CellSample {
  table: string;
  file: string | null;
  sheet: string | null;
  excel_row: number | null;
  row_id: string | null;
  column: string | null;
  old: string | null;
  new: string | null;
  what?: string;
}

export interface OpEffect {
  cells: number;
  items_resolved: number;
  rows_removed: number;
  rows_added: number;
  columns_added: string[];
  columns_renamed: [string, string][];
  tables: string[];
  samples: CellSample[];
  unmatched: string[];
  unmatched_total: number;
  notes: string[];
}

export interface ChangePreview {
  operations: { text: string; summary: string; effect: OpEffect }[];
  totals: { cells: number; items_resolved: number; rows_removed: number; rows_added: number; columns_added: string[]; columns_renamed: [string, string][]; tables: string[] };
  summary: string;
  items_answered_by_edits: number;
  follow_up_fixes: number;
  open_items_after: number;
  changes_anything: boolean;
}

/** A Phase T step's effect: rows and columns per table, notes, and the first rows of the result. */
export interface TransformEffect {
  tables: { table: string; rows_before: number | null; rows_after: number | null; new?: boolean; dropped?: boolean; columns_added: string[]; columns_removed: string[] }[];
  cells: number;
  notes: string[];
  unmatched: string[];
  unmatched_total: number;
  sample: { table: string; columns: string[]; rows: (string | null)[][] } | null;
}

export interface TransformPreview {
  kind: "transform";
  operations: { text: string; summary: string; effect: TransformEffect }[];
  summary: string;
  steps_before: number;
  tables_after: { name: string; rows: number; columns: number }[];
  changes_anything: boolean;
}

export type ChangeSetStatus = "proposed" | "applied" | "discarded" | "undone" | "stale" | "failed";

export interface ChangeSetView {
  id: string;
  status: ChangeSetStatus;
  instruction: string;
  source: "rules" | "llm" | "none";
  summary: string | null;
  created_at: string;
  applied_at: string | null;
  undone_at: string | null;
  error: string | null;
  preview: ChangePreview | TransformPreview;
}

export interface AgentState {
  phase: PhaseKey;
  status: Status;
  usable: boolean;
  working: boolean;
  job_action: string | null;
  messages: AgentMessage[];
  changesets: Record<string, ChangeSetView>;
  undo: Omit<ChangeSetView, "preview"> | null;
  error: PhaseError | null;
  llm: { enabled: boolean; available: boolean; model: string | null; message: string };
}

export interface IssueGroup {
  table: string;
  rule: string;
  what: string;
  column: string | null;
  count: number;
  cells: number;
  severity: "info" | "warn" | "block";
  client: boolean;
  why: string;
  suggested: string;
  examples: string[];
  rows: number[];
}

export interface AgentTable {
  ref: string;
  file: string | null;
  sheet: string | null;
  rows: number | null;
  columns: string[];
}

export interface RecipeStep {
  step: number;
  text: string;
  summary: string;
  changeset: string | null;
  notes: string[];
}

export interface IssuesView {
  open: number;
  resolved: number;
  resolved_by_agent: number;
  groups: IssueGroup[];
  tables: AgentTable[];
  /** Phase T: the recipe's steps and the step that failed, if any. */
  steps?: RecipeStep[];
  failed?: { step: number; text: string; message: string } | null;
}

export interface SummaryT {
  tables: { name: string; rows: number; columns: number; made_here: boolean }[];
  inputs: { name: string; source_file: string | null; sheet: string | null }[];
  steps: { step: number; text: string; summary: string; notes: string[] }[];
  failed: { step: number; text: string; message: string } | null;
}

export interface CellState {
  rule: string;
  old?: string | null;
  note?: string | null;
  why?: string;
  proposed?: string | null;
}

export interface SheetRow {
  row_id: string;
  excel_row: number | null;
  values: Record<string, string | null>;
  changed: Record<string, CellState>;
  flagged: Record<string, CellState>;
}

export interface RowsPage {
  table: string;
  columns: string[];
  total: number;
  offset: number;
  limit: number;
  rows: SheetRow[];
  column_items: { rule: string; column: string | null; why: string }[];
}

export type EventItem =
  | { seq: number; t: string; job: string; phase: PhaseKey; type: "stage"; stage: string; pct: number | null; detail: string | null }
  | { seq: number; t: string; job: string; phase: PhaseKey; type: "log"; level: "info" | "warn" | "error"; message: string }
  | { seq: number; t: string; job: string; phase: PhaseKey; type: "progress"; done: number; total: number; label: string };

export interface CandidateTable {
  ref: string;
  file: string | null;
  sheet: string | null;
  rows: number;
  columns: number;
  weekly: boolean; // the phase can run on this table
  level?: string;
  from_monthly?: boolean; // a monthly table the phase turns into weeks before checking
  note: string;
}

export interface RuleState {
  key: string;
  code: string;
  label: string;
  threshold: number | null;
  active: boolean;
}

export interface ConfigTable {
  ref: string;
  file: string | null;
  sheet: string | null;
  rows: number;
  checked: boolean;
  reason: string | null;
}

export interface ConfigPreview {
  table: string | null;
  tables?: ConfigTable[]; // every table and whether this config describes it
  rows: number;
  period: string;
  warnings: string[];
  // Phase C
  geography?: string | null;
  series?: string[];
  variables?: number;
  checked?: number;
  ignored?: string[];
  min_zero_weeks?: number;
  date_ranges?: Record<string, (string | null)[]>;
  // Phase D
  pairs?: number;
  rules?: RuleState[];
  ratios?: { name: string; numerator: string; denominator: string; threshold: number | null; active: boolean }[];
}

export interface ResultFile {
  id: string;
  label: string;
  group: string;
  phase: string;
  kind: string;
  description: string;
  bytes: number;
}

export interface Results {
  complete: boolean;
  files: ResultFile[];
  phases: Record<PhaseKey, Status>;
}

// --- phase summaries (shape per phase; every field optional-safe in the UI)
export interface IssueRow {
  Status: "Warning" | "Fixed" | "Identified" | string;
  Severity: string;
  Issue: string;
  Code: string;
  File: string;
  Sheet: string;
  Table: string;
  Where: string;
  "What was found": string;
  "Action taken": string;
}

export interface SummaryA {
  files: number;
  sheets: number;
  tables_found: number;
  selected_tables: number;
  rows_in: number;
  rows_out: number;
  severity: { block: number; warn: number; info: number };
  blocking: string[];
  issue_counts: Record<string, number>;
  issues: IssueRow[];
  issues_total: number;
  changes: Record<string, string | number>[];
  changes_total: number;
  tables: { ref: string; file: string; sheet: string | null; range: string | null; rows: number; columns: number; selected: boolean; decided_by: string | null }[];
  llm: { mode: string; model?: string; questions?: number; calls?: number; agreed?: number; changed?: number; overruled?: number; fallback: boolean };
}

export interface SummaryB {
  tables: { ref: string; file: string | null; sheet: string | null; rows_in: number; rows_out: number; needs_review: number; summary: string }[];
  totals: { cells_fixed: number; rows_dropped: number; needs_review_open: number };
  unreadable: { file: string; error: string }[];
  skipped: string[];
  by_rule: { rule: string; what: string; fixed: number; flagged: number }[];
  needs_review: { table_ref: string; row_id: string | null; column: string | null; rule: string; why_flagged: string; current_value: string | null; original_value: string | null; proposed_value: string | null; severity: string; client_report: boolean; status: string }[];
  needs_review_total: number;
  fixes: { table_ref: string; row_id: string | null; column: string | null; rule: string; action: string; old_value: string | null; new_value: string | null; note: string }[];
  fixes_total: number;
  client_questions: number;
  /** Columns / rows that add up to another (D-28). Absent or empty when nothing adds up. */
  buckets?: BucketRow[];
  /** The columns where the analyst said zero is a valid value (D-27 never fills them), per table. */
  zero_expected?: Record<string, string[]>;
}

export interface BucketRow {
  table_ref: string;
  sheet: string | null;
  where: "columns" | "rows";
  label_column: string | null;
  bucket: string;
  sum_these: string[];
  do_not_add_with_them: string[];
  status: "open" | "confirmed" | "rejected";
  relations: { total: string; parts: string[]; kind: "sum" | "identical"; rows: number; matched: number; share: number; alternatives: number; decision: string }[];
}

/** A table's numeric columns for the Phase B choice "zero is a valid value here". */
export interface ColumnChoiceTable {
  ref: string;
  file: string | null;
  sheet: string | null;
  rows: number;
  dated: boolean;
  period: string | null;
  columns: { name: string; numbers: number; zeros: number; blanks: number; zero_share: number; likely_zero_valid: boolean; name_hint: boolean }[];
}

export interface WeeklyInfo {
  table_ref: string;
  source_file: string | null;
  sheet: string | null;
  rows_in: number;
  rows_out: number;
  measures: string[];
  exact: number;
  unreadable: number;
  inexact: string[];
}

export interface FindingRow {
  id: string;
  code: string;
  check: string;
  status: string;
  variables: string[];
  periods: string[];
  reason: string;
  client_report: boolean;
}

export interface NotChecked {
  ref: string;
  file: string | null;
  sheet: string | null;
  rows: number;
  reason: string | null;
}

/** Phases C and D check every table their config describes: one result per table, plus the ones passed on. */
export interface SummaryC {
  tables: SummaryCTable[];
  not_checked: NotChecked[];
}

export interface SummaryD {
  tables: SummaryDTable[];
  not_checked: NotChecked[];
  /** Monthly tables that were also spread over Monday-to-Sunday weeks (absent for other data). */
  weekly?: WeeklyInfo[];
}

export interface SummaryCTable {
  table: string;
  source_file: string | null;
  sheet: string | null;
  rows: number;
  layout: { period: string; geography: string | null; variables: string[] };
  variables: { in_range: number; checked: number; ignored: number };
  periods: { total: number; first: string | null; last: string | null };
  dataset_checks: Record<string, "PASS" | "FAIL">;
  counts: Record<string, { open: number; valid: number }>;
  totals: { findings_open: number; findings_valid: number };
  uploads: number;
  findings: FindingRow[];
  findings_by_code: Record<string, number>;
}

export interface SummaryDTable {
  table: string;
  source_file: string | null;
  sheet: string | null;
  rows: number;
  pairs: { mapped: number; run: number; with_spend: number };
  counts: Record<string, Record<string, number>>;
  totals: Record<string, number>;
  inactive_rules: string[];
  rules: Record<string, unknown>[];
  uploads: number;
  findings: FindingRow[];
  findings_by_code: Record<string, number>;
}
