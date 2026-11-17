import type {
  AffectedCitation,
  Capabilities,
  CaseItem,
  CaseList,
  DemoOutcome,
  EvidenceDetail,
  Material,
  MaterialDetail,
  MaterialList,
  MaterialUploadResult,
  ReadinessReport,
  CitationIssue,
  SearchAttemptView,
  SearchCreate,
  SearchHistory,
  SegmentList,
  SummaryCreate,
  SummaryDetail,
  SummaryList,
  SummarySourcesUpdate,
  TaskDetail,
  TaskItem,
  TaskList,
} from "./types";

export class ApiRequestError extends Error {
  status: number;
  code: string | null;
  duplicates: Material[];
  affected: AffectedCitation[];
  issues: CitationIssue[];

  constructor(
    message: string,
    status: number,
    code: string | null = null,
    duplicates: Material[] = [],
    affected: AffectedCitation[] = [],
    issues: CitationIssue[] = [],
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.duplicates = duplicates;
    this.affected = affected;
    this.issues = issues;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...init, headers });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code: string | null = null;
    let duplicates: Material[] = [];
    let affected: AffectedCitation[] = [];
    let issues: CitationIssue[] = [];
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        message = payload.detail;
      } else if (payload.detail && typeof payload.detail === "object") {
        if (typeof payload.detail.message === "string") message = payload.detail.message;
        if (typeof payload.detail.code === "string") code = payload.detail.code;
        if (Array.isArray(payload.detail.duplicates)) duplicates = payload.detail.duplicates;
        if (Array.isArray(payload.detail.affected)) affected = payload.detail.affected;
        if (Array.isArray(payload.detail.issues)) issues = payload.detail.issues;
      }
    } catch {
      // 保留 HTTP 状态错误。
    }
    throw new ApiRequestError(message, response.status, code, duplicates, affected, issues);
  }
  return response.json() as Promise<T>;
}

export function createRequestKey() {
  return crypto.randomUUID();
}

export const api = {
  readiness: () => request<ReadinessReport>("/api/readiness"),
  capabilities: () => request<Capabilities>("/api/capabilities"),
  listCases: (includeArchived: boolean) =>
    request<CaseList>(`/api/cases?include_archived=${includeArchived}`),
  createCase: (name: string) =>
    request<CaseItem>("/api/cases", { method: "POST", body: JSON.stringify({ name }) }),
  openCase: (caseId: string) =>
    request<CaseItem>(`/api/cases/${caseId}/open`, { method: "POST" }),
  archiveCase: (caseId: string) =>
    request<CaseItem>(`/api/cases/${caseId}/archive`, { method: "POST" }),
  listMaterials: (caseId: string) => request<MaterialList>(`/api/cases/${caseId}/materials`),
  uploadMaterial: (caseId: string, file: File, confirmDuplicate: boolean, idempotencyKey: string) => {
    const body = new FormData();
    body.append("file", file);
    body.append("confirm_duplicate", String(confirmDuplicate));
    return request<MaterialUploadResult>(`/api/cases/${caseId}/materials`, {
      method: "POST",
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    });
  },
  getMaterial: (materialId: string) => request<MaterialDetail>(`/api/materials/${materialId}`),
  listMaterialSegments: (materialId: string, versionId: string, cursor = 0) =>
    request<SegmentList>(`/api/materials/${materialId}/versions/${versionId}/segments?cursor=${cursor}&limit=100`),
  createSearch: (caseId: string, payload: SearchCreate, idempotencyKey: string) =>
    request<TaskItem>(`/api/cases/${caseId}/searches`, {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  listSearches: (caseId: string) => request<SearchHistory>(`/api/cases/${caseId}/searches`),
  getSearchAttempt: (taskId: string, attemptId: string) =>
    request<SearchAttemptView>(`/api/searches/${taskId}/attempts/${attemptId}`),
  getEvidence: (evidenceId: string) => request<EvidenceDetail>(`/api/evidence/${evidenceId}`),
  createSummary: (caseId: string, payload: SummaryCreate) =>
    request<SummaryDetail>(`/api/cases/${caseId}/summaries`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listSummaries: (caseId: string) => request<SummaryList>(`/api/cases/${caseId}/summaries`),
  getSummary: (summaryId: string) => request<SummaryDetail>(`/api/summaries/${summaryId}`),
  updateSummarySources: (summaryId: string, payload: SummarySourcesUpdate) =>
    request<SummaryDetail>(`/api/summaries/${summaryId}/sources`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  saveSummaryVersion: (summaryId: string, body: string, baseVersionId: string | null) =>
    request<SummaryDetail>(`/api/summaries/${summaryId}/versions`, {
      method: "POST",
      body: JSON.stringify({ body, base_version_id: baseVersionId }),
    }),
  generateSummary: (summaryId: string, idempotencyKey: string) =>
    request<TaskItem>(`/api/summaries/${summaryId}/generate`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  confirmSummary: (summaryId: string) =>
    request<SummaryDetail>(`/api/summaries/${summaryId}/confirm`, { method: "POST" }),
  listTasks: (caseId?: string) =>
    request<TaskList>(`/api/tasks${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ""}`),
  getTask: (taskId: string) => request<TaskDetail>(`/api/tasks/${taskId}`),
  cancelTask: (taskId: string) =>
    request<TaskItem>(`/api/tasks/${taskId}/cancel`, { method: "POST" }),
  retryTask: (taskId: string, scope?: SearchCreate | Record<string, unknown>) =>
    request<TaskItem>(`/api/tasks/${taskId}/retry`, {
      method: "POST",
      body: JSON.stringify(scope === undefined ? {} : { scope }),
    }),
  createDemoTask: (caseId: string, outcome: DemoOutcome) =>
    request<TaskItem>("/api/demo/tasks", {
      method: "POST",
      body: JSON.stringify({ case_id: caseId, outcome, delay_ms: 50 }),
    }),
};
