import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, createRequestKey } from "./api";
import EvidencePanel from "./EvidencePanel";
import type {
  Material,
  MaterialDetail,
  ParseVersion,
  SearchAttemptView,
  SearchCreate,
  SearchMode,
  SourceReference,
  TaskDetail,
  TaskItem,
  TaskStatus,
} from "./types";

const statusLabels: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  recovery_pending: "状态待恢复",
};

interface VersionOption {
  material: Material;
  version: ParseVersion;
  label: string;
}

interface SearchFormState {
  question: string;
  query: string;
  mode: SearchMode;
  materialVersionIds: string[];
  yearFrom: string;
  yearTo: string;
  court: string;
  cause: string;
  limit: number;
}

function emptyForm(): SearchFormState {
  return {
    question: "",
    query: "",
    mode: "auto",
    materialVersionIds: [],
    yearFrom: "",
    yearTo: "",
    court: "",
    cause: "",
    limit: 10,
  };
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function formFromSnapshot(snapshot: Record<string, unknown>): SearchFormState {
  const merged = { ...snapshot, ...objectValue(snapshot.scope) };
  const filters = objectValue(merged.filters);
  return {
    question: typeof merged.question === "string" ? merged.question : "",
    query: typeof merged.query === "string" ? merged.query : "",
    mode: merged.mode === "law" || merged.mode === "cases" ? merged.mode : "auto",
    materialVersionIds: Array.isArray(merged.material_version_ids)
      ? merged.material_version_ids.filter((item): item is string => typeof item === "string")
      : [],
    yearFrom: typeof filters.year_from === "number" ? String(filters.year_from) : "",
    yearTo: typeof filters.year_to === "number" ? String(filters.year_to) : "",
    court: typeof filters.court === "string" ? filters.court : "",
    cause: typeof filters.cause === "string" ? filters.cause : "",
    limit: typeof merged.limit === "number" ? merged.limit : 10,
  };
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

interface SearchPanelProps {
  caseId: string;
  archived: boolean;
  retryTask: TaskItem | null;
  onRetryHandled: () => void;
  stagedSources: SourceReference[];
  addingResearch: boolean;
  onToggleResearchSource: (source: SourceReference) => void;
  onAddResearchSources: (sources: SourceReference[]) => void;
}

export default function SearchPanel({
  caseId,
  archived,
  retryTask,
  onRetryHandled,
  stagedSources,
  addingResearch,
  onToggleResearchSource,
  onAddResearchSources,
}: SearchPanelProps) {
  const [form, setForm] = useState<SearchFormState>(emptyForm);
  const [versionOptions, setVersionOptions] = useState<VersionOption[]>([]);
  const [history, setHistory] = useState<TaskItem[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<SearchAttemptView | null>(null);
  const [retryTaskId, setRetryTaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingAttempt, setLoadingAttempt] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const submissionRef = useRef({ fingerprint: "", key: "" });
  const attemptRequestRef = useRef(0);

  const loadHistory = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const result = await api.listSearches(caseId);
      setHistory(result.items);
      setError("");
      return result.items;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "检索历史加载失败");
      return [];
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [caseId]);

  const openTask = useCallback(async (taskId: string, attemptId?: string, showLoading = true) => {
    const requestId = ++attemptRequestRef.current;
    setSelectedTaskId(taskId);
    if (showLoading) setLoadingAttempt(true);
    try {
      const detail = await api.getTask(taskId);
      const targetAttemptId = attemptId || detail.attempts[detail.attempts.length - 1]?.id;
      if (!targetAttemptId) {
        if (requestId === attemptRequestRef.current) {
          setTaskDetail(detail);
          setSelectedAttemptId(null);
          setAttempt(null);
        }
        return;
      }
      const view = await api.getSearchAttempt(taskId, targetAttemptId);
      if (requestId !== attemptRequestRef.current) return;
      setTaskDetail(detail);
      setSelectedAttemptId(targetAttemptId);
      setAttempt(view);
      setError("");
    } catch (cause) {
      if (requestId === attemptRequestRef.current) {
        setError(cause instanceof Error ? cause.message : "检索结果加载失败");
      }
    } finally {
      if (showLoading && requestId === attemptRequestRef.current) setLoadingAttempt(false);
    }
  }, []);

  useEffect(() => {
    let current = true;
    setForm(emptyForm());
    setRetryTaskId(null);
    setSelectedTaskId(null);
    setTaskDetail(null);
    setSelectedAttemptId(null);
    setAttempt(null);
    setVersionOptions([]);
    api.listMaterials(caseId)
      .then(async (result) => {
        const details = await Promise.all(result.items.map((material) => api.getMaterial(material.id).catch(() => null)));
        if (!current) return;
        const options = details.flatMap((detail: MaterialDetail | null) => {
          if (!detail) return [];
          return detail.versions
            .filter((version) => (version.status === "success" || version.status === "partial") && version.char_count > 0)
            .map((version) => ({
              material: detail.material,
              version,
              label: `${detail.material.original_name} · ${version.status === "partial" ? "部分解析" : "解析完成"} · ${version.char_count.toLocaleString()} 字符`,
            }));
        });
        setVersionOptions(options);
      })
      .catch((cause) => {
        if (current) setError(cause instanceof Error ? cause.message : "可选材料加载失败");
      });
    void loadHistory(true).then((items) => {
      if (current && items[0]) void openTask(items[0].id);
    });
    return () => { current = false; };
  }, [caseId, loadHistory, openTask]);

  useEffect(() => {
    if (!retryTask || retryTask.task_type !== "search" || retryTask.case_id !== caseId) return;
    setForm(formFromSnapshot(retryTask.input_snapshot));
    setRetryTaskId(retryTask.id);
    setNotice("已载入原检索参数；修改后将创建新 Attempt，旧证据和错误保持不变");
    void openTask(retryTask.id);
    onRetryHandled();
  }, [retryTask, caseId, openTask, onRetryHandled]);

  const selectedHistoryTask = history.find((task) => task.id === selectedTaskId);
  const hasActiveSearches = history.some((task) => task.status === "queued" || task.status === "running");
  const selectedIsActive = selectedHistoryTask?.status === "queued" || selectedHistoryTask?.status === "running";
  useEffect(() => {
    if (!hasActiveSearches) return;
    const timer = window.setInterval(() => {
      void loadHistory(false);
      if (selectedTaskId && selectedIsActive) void openTask(selectedTaskId, selectedAttemptId || undefined, false);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hasActiveSearches, selectedIsActive, selectedTaskId, selectedAttemptId, loadHistory, openTask]);

  function updateForm<K extends keyof SearchFormState>(key: K, value: SearchFormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function toggleVersion(versionId: string) {
    setForm((current) => {
      const selected = current.materialVersionIds.includes(versionId);
      if (!selected && current.materialVersionIds.length >= 10) {
        setError("每次检索最多选择 10 个材料版本");
        return current;
      }
      setError("");
      return {
        ...current,
        materialVersionIds: selected
          ? current.materialVersionIds.filter((id) => id !== versionId)
          : [...current.materialVersionIds, versionId],
      };
    });
  }

  function buildPayload(): SearchCreate {
    const yearFrom = form.yearFrom ? Number(form.yearFrom) : null;
    const yearTo = form.yearTo ? Number(form.yearTo) : null;
    if (yearFrom !== null && yearTo !== null && yearFrom > yearTo) {
      throw new Error("起始年份不能晚于结束年份");
    }
    return {
      question: form.question.trim(),
      query: form.query.trim() || null,
      mode: form.mode,
      material_version_ids: form.materialVersionIds,
      filters: {
        year_from: yearFrom,
        year_to: yearTo,
        court: form.court.trim(),
        cause: form.cause.trim(),
      },
      limit: form.limit,
    };
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form.question.trim() || archived) return;
    let payload: SearchCreate;
    try {
      payload = buildPayload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "检索参数无效");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      let task: TaskItem;
      if (retryTaskId) {
        task = await api.retryTask(retryTaskId, payload);
        setNotice("已创建新 Attempt，旧检索词、来源错误和证据仍可查询");
      } else {
        const fingerprint = JSON.stringify(payload);
        if (submissionRef.current.fingerprint !== fingerprint) {
          submissionRef.current = { fingerprint, key: createRequestKey() };
        }
        task = await api.createSearch(caseId, payload, submissionRef.current.key);
        setNotice("统一检索已创建；各来源结果将在返回后分别保存");
      }
      setRetryTaskId(null);
      setForm(emptyForm());
      setSelectedAttemptId(null);
      await loadHistory(false);
      await openTask(task.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : retryTaskId ? "检索重试失败" : "检索创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  function beginRetry(detail: TaskDetail) {
    setForm(formFromSnapshot(detail.input_snapshot));
    setRetryTaskId(detail.id);
    setNotice("正在配置新 Attempt；旧 Attempt 不会被覆盖");
    setError("");
  }

  function cancelRetry() {
    setRetryTaskId(null);
    setForm(emptyForm());
    setNotice("");
  }

  return (
    <section className="search-panel" aria-labelledby="search-title">
      <div className="panel-heading">
        <div><span className="eyebrow">UNIFIED RESEARCH</span><h3 id="search-title">统一法规与类案检索</h3><p>规则路由并行查询来源；展示实际检索词、命中原因与覆盖状态，不生成研究摘要。</p></div>
      </div>

      {(error || notice) && (
        <div className={`message ${error ? "error" : "success"}`} role={error ? "alert" : "status"}><span>{error || notice}</span><button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="关闭检索提示">×</button></div>
      )}

      {!archived && (
        <form className={`search-form ${retryTaskId ? "retry-mode" : ""}`} onSubmit={(event) => void handleSubmit(event)}>
          <div className="search-form-heading"><strong>{retryTaskId ? "配置新检索 Attempt" : "新建统一检索"}</strong>{retryTaskId && <button className="button secondary" type="button" onClick={cancelRetry}>取消重试</button>}</div>
          <label className="field-label" htmlFor="search-question">研究问题</label>
          <textarea id="search-question" className="search-question" value={form.question} onChange={(event) => updateForm("question", event.target.value)} placeholder="例如：合同解除后的违约责任和赔偿范围是什么？" maxLength={2000} required disabled={submitting} />
          <div className="search-form-grid">
            <label>检索模式<select value={form.mode} onChange={(event) => updateForm("mode", event.target.value as SearchMode)} disabled={submitting}><option value="auto">自动</option><option value="law">法规</option><option value="cases">类案</option></select></label>
            <label>每源结果上限<input type="number" min={1} max={50} value={form.limit} onChange={(event) => updateForm("limit", Number(event.target.value))} disabled={submitting} /></label>
          </div>
          <label className="field-label" htmlFor="search-query">自定义检索词（可选）</label>
          <input id="search-query" className="text-input" value={form.query} onChange={(event) => updateForm("query", event.target.value)} placeholder="留空使用研究问题；材料提取词会追加到检索词" maxLength={2000} disabled={submitting} />
          <fieldset className="filter-fieldset"><legend>裁判文书过滤</legend><div className="filter-grid"><label>起始年份<input type="number" min={1000} max={9999} value={form.yearFrom} onChange={(event) => updateForm("yearFrom", event.target.value)} placeholder="例如 2020" disabled={submitting} /></label><label>结束年份<input type="number" min={1000} max={9999} value={form.yearTo} onChange={(event) => updateForm("yearTo", event.target.value)} placeholder="例如 2025" disabled={submitting} /></label><label>法院<input value={form.court} onChange={(event) => updateForm("court", event.target.value)} maxLength={200} placeholder="法院名称关键词" disabled={submitting} /></label><label>案由<input value={form.cause} onChange={(event) => updateForm("cause", event.target.value)} maxLength={200} placeholder="案由关键词" disabled={submitting} /></label></div><p className="field-help">过滤条件仅提交给裁判文书来源；材料不会自动生成法院、年份或案由条件。</p></fieldset>
          <details className="material-version-picker" open={form.materialVersionIds.length > 0}><summary>使用材料辅助构造检索词（已选 {form.materialVersionIds.length}/10）</summary>{versionOptions.length === 0 ? <p className="field-help">暂无可用于检索的解析版本；成功或部分完成且有文本的版本会显示在这里。</p> : <div className="version-option-list">{versionOptions.map((option) => <label key={option.version.id}><input type="checkbox" checked={form.materialVersionIds.includes(option.version.id)} onChange={() => toggleVersion(option.version.id)} disabled={submitting} /><span>{option.label}</span></label>)}</div>}<p className="field-help">仅规则提取法名与争议词，不建立材料向量库。</p></details>
          <div className="search-submit-row"><span>类案模式以裁判文书为主、指导案例为补充；业务参考仅在明确指引或学习意图时触发。</span><button className="button primary" type="submit" disabled={submitting || !form.question.trim() || form.limit < 1 || form.limit > 50}>{submitting ? "正在提交…" : retryTaskId ? "创建新 Attempt" : "开始统一检索"}</button></div>
        </form>
      )}

      <div className="search-workspace">
        <aside className="search-history" aria-label="检索历史">
          <div className="result-section-heading"><h4>检索历史</h4><span>{history.length}</span></div>
          {loading ? <div className="empty-state compact">正在加载检索历史…</div> : history.length === 0 ? <div className="empty-state compact"><strong>暂无检索</strong><span>提交研究问题后，可在这里查看每次 Attempt。</span></div> : history.map((task) => (
            <button type="button" className={`search-history-item ${selectedTaskId === task.id ? "selected" : ""}`} key={task.id} onClick={() => { setSelectedAttemptId(null); void openTask(task.id); }}>
              <span className={`task-status ${task.status}`}>{statusLabels[task.status]}</span>
              <strong>{typeof task.input_snapshot.question === "string" ? task.input_snapshot.question : task.title}</strong>
              <small>{formatDate(task.created_at)} · 第 {task.attempt_number} 次执行{typeof task.result?.evidence_count === "number" ? ` · ${task.result.evidence_count} 条证据` : ""}</small>
            </button>
          ))}
        </aside>

        <div className="search-result-area">
          {!selectedTaskId ? <div className="empty-state compact"><strong>选择检索查看结果</strong><span>旧 Attempt 的检索计划、来源覆盖和证据均可独立查询。</span></div> : (
            <>
              <div className="attempt-toolbar">
                <label>执行记录<select value={selectedAttemptId || ""} onChange={(event) => void openTask(selectedTaskId, event.target.value)} disabled={!taskDetail || loadingAttempt}>{taskDetail?.attempts.slice().reverse().map((item) => <option value={item.id} key={item.id}>第 {item.attempt_number} 次 · {statusLabels[item.status]} · {formatDate(item.created_at)}</option>)}</select></label>
                {taskDetail && !archived && ["failed", "partial", "cancelled", "recovery_pending"].includes(taskDetail.status) && <button className="button secondary" type="button" onClick={() => beginRetry(taskDetail)}>使用新参数重试</button>}
              </div>
              {taskDetail && <div className="attempt-status-line"><span className={`task-status ${taskDetail.status}`}>{statusLabels[taskDetail.status]}</span><span>{taskDetail.current_stage} · {taskDetail.progress_detail}</span></div>}
              {loadingAttempt && !attempt ? <div className="empty-state compact">正在读取检索 Attempt…</div> : attempt ? <EvidencePanel attempt={attempt} archived={archived} stagedSources={stagedSources} adding={addingResearch} onToggleSource={onToggleResearchSource} onAddSources={onAddResearchSources} /> : <div className="empty-state compact">执行记录尚未产生检索计划。</div>}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
