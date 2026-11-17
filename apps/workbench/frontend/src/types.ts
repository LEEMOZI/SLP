export interface CaseItem {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
  archived_at: string | null;
}

export interface CaseList {
  items: CaseItem[];
  total: number;
}

export interface ServiceHealth {
  name: string;
  label: string;
  status: "healthy" | "unavailable";
  latency_ms: number;
  detail: string;
  checked_at: string;
}

export interface ReadinessReport {
  status: "ready" | "degraded";
  can_manage_cases: boolean;
  available_count: number;
  total_count: number;
  checked_at: string;
  services: ServiceHealth[];
}

export type TaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "recovery_pending";

export interface TaskAttempt {
  id: string;
  task_id: string;
  attempt_number: number;
  status: TaskStatus;
  current_stage: string;
  progress: number | null;
  progress_detail: string;
  input_snapshot: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskItem {
  id: string;
  case_id: string;
  case_name: string;
  task_type: string;
  title: string;
  status: TaskStatus;
  current_stage: string;
  progress: number | null;
  progress_detail: string;
  model_role: string | null;
  input_snapshot: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  attempt_number: number;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskDetail extends TaskItem {
  attempts: TaskAttempt[];
}

export interface TaskList {
  items: TaskItem[];
  total: number;
  demo_enabled: boolean;
}

export type DemoOutcome = "completed" | "partial" | "failed" | "cancellable";

export interface ErrorDetail {
  code?: string;
  message?: string;
}

export interface Capabilities {
  version: string;
  parser: {
    formats: string[];
    version: string;
    ocr_available: boolean;
    limits: {
      max_file_bytes: number;
      max_files_per_case: number;
      max_ocr_pages: number;
      max_text_chars: number;
    };
  };
  sources: Array<{ id: string; status: string }>;
  summary?: {
    default_provider: string;
    providers: Array<{ id: string; status: string }>;
  };
}

export type MaterialStatus =
  | "queued"
  | "running"
  | "success"
  | "partial"
  | "failed"
  | "cancelled"
  | "recovery_pending";

export interface Material {
  id: string;
  case_id: string;
  original_name: string;
  format: string;
  size_bytes: number;
  sha256: string;
  status: MaterialStatus;
  current_version_id: string | null;
  latest_task_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ParseVersion {
  id: string;
  material_id: string;
  task_id: string;
  attempt_id: string;
  parser_version: string;
  status: Exclude<MaterialStatus, "queued">;
  page_count: number | null;
  segment_count: number;
  char_count: number;
  locator_type: string | null;
  coverage: Record<string, unknown>;
  error: ErrorDetail | null;
  created_at: string;
  finished_at: string | null;
}

export interface MaterialSegment {
  id: string;
  parse_version_id: string;
  sequence: number;
  text: string;
  status: string;
  locator: Record<string, unknown>;
  extraction_method: string | null;
  error: ErrorDetail | null;
}

export interface MaterialList {
  items: Material[];
  total: number;
}

export interface MaterialDetail {
  material: Material;
  versions: ParseVersion[];
}

export interface SegmentList {
  items: MaterialSegment[];
  next_cursor: number | null;
}

export interface MaterialUploadResult {
  material: Material;
  task: TaskItem;
}

export type SearchMode = "auto" | "law" | "cases";

export interface SearchFilters {
  year_from: number | null;
  year_to: number | null;
  court: string;
  cause: string;
}

export interface SearchCreate {
  question: string;
  query?: string | null;
  mode: SearchMode;
  material_version_ids: string[];
  filters: SearchFilters;
  limit: number;
}

export interface MaterialEnrichment {
  material_version_id: string;
  segment_ids: string[];
  terms: string[];
}

export interface SearchPlan {
  mode: SearchMode;
  question: string;
  base_query: string;
  query: string;
  sources: string[];
  source_queries: Record<string, string>;
  filters: SearchFilters;
  material_enrichment: MaterialEnrichment[];
  uncovered_sources: string[];
  routing_reason: string;
}

export interface ExecutedQuery {
  query?: string;
  kind?: string;
  status?: string;
  [key: string]: unknown;
}

export interface SearchSourceState {
  source: string;
  status: string;
  query?: string;
  filters?: Record<string, unknown>;
  executed_queries?: ExecutedQuery[];
  coverage?: Record<string, unknown>;
  error?: ErrorDetail | null;
  evidence_count: number;
}

export interface EvidenceItem {
  id: string;
  case_id: string;
  task_id: string;
  attempt_id: string;
  group_name: "law" | "cases" | "reference";
  source: string;
  title: string;
  source_label: string;
  excerpt: string;
  content: string;
  hit_reason: string;
  match_type: string;
  partial_match: boolean;
  rank: number;
  score: number | null;
  locator: Record<string, unknown>;
  source_ref: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface SearchAttemptView {
  task_id: string;
  attempt_id: string;
  status: TaskStatus;
  plan: SearchPlan;
  sources: Record<string, SearchSourceState>;
  evidence: {
    law: EvidenceItem[];
    cases: EvidenceItem[];
    reference: EvidenceItem[];
  };
  error: string | null;
}

export interface SearchHistory {
  items: TaskItem[];
  total: number;
}

export interface EvidenceDocument {
  source: string;
  source_ref: Record<string, unknown>;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
  fetched_at: string;
  cached: boolean;
}

export interface EvidenceDetail {
  evidence: EvidenceItem;
  document: EvidenceDocument | null;
  source_error: string | null;
}

export interface SourceReference {
  kind: "evidence" | "material_segment";
  id: string;
}

export interface SummarySource {
  handle: string;
  kind: "evidence" | "material_segment";
  source_id: string;
  case_id: string;
  title: string;
  text: string;
  locator: Record<string, unknown>;
  source_label: string;
  origin_status: string;
  attempt_id: string | null;
  task_id: string | null;
  material_id: string | null;
  parse_version_id: string | null;
  sequence: number | null;
  source_ref: Record<string, unknown>;
}

export interface ResearchSummary {
  id: string;
  case_id: string;
  title: string;
  question: string;
  current_version_id: string | null;
  current_version_number: number | null;
  current_sources: SummarySource[];
  latest_task_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SummaryVersion {
  id: string;
  summary_id: string;
  version_number: number;
  status: "draft" | "edited" | "confirmed";
  body: string;
  citations: Record<string, SummarySource>;
  base_version_id: string | null;
  task_id: string | null;
  attempt_id: string | null;
  generator_provider: string | null;
  requested_provider: string | null;
  actual_model: string | null;
  coverage: Record<string, unknown>;
  saved_at: string;
  confirmed_at: string | null;
}

export interface CitationIssue {
  code: string;
  message: string;
  handle: string | null;
}

export interface AffectedCitation {
  handle: string;
  version_id: string;
  version_number: number;
}

export interface SummaryDetail {
  summary: ResearchSummary;
  sources: SummarySource[];
  current_version: SummaryVersion | null;
  versions: SummaryVersion[];
  coverage: Record<string, unknown>;
  issues: CitationIssue[];
}

export interface SummaryList {
  items: ResearchSummary[];
  total: number;
}

export interface DeliveryVersion {
  id: string;
  case_id: string;
  summary_id: string;
  version_id: string;
  summary_title: string;
  version_number: number;
  format: "markdown" | "docx";
  status: "official" | "draft";
  sha256: string;
  size_bytes: number;
  facts: Record<string, unknown>;
  issues: CitationIssue[];
  generator_provider: string | null;
  actual_model: string | null;
  task_id: string | null;
  exported_at: string;
}

export interface DeliveryList {
  items: DeliveryVersion[];
  total: number;
}

export interface SummaryCreate {
  title: string;
  question: string;
  sources: SourceReference[];
}

export interface SummarySourcesUpdate {
  add: SourceReference[];
  remove: string[];
  confirm_remove?: boolean;
}
