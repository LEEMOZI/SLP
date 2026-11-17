import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiRequestError, api, createRequestKey } from "./api";
import SourcePreview, { locatorText } from "./SourcePreview";
import type {
  AffectedCitation,
  Capabilities,
  CitationIssue,
  DeliveryVersion,
  EvidenceDetail,
  ResearchSummary,
  SourceReference,
  SummaryDetail,
  SummarySource,
  SummaryVersion,
  TaskItem,
} from "./types";

const versionStatusLabels = {
  draft: "生成草稿",
  edited: "人工编辑",
  confirmed: "已人工复核",
};

const taskStatusLabels: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  recovery_pending: "状态待恢复",
};

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function citationHandles(body: string) {
  return Array.from(new Set(Array.from(body.matchAll(/\[\[([^\[\]\s]{1,32})\]\]/g), (match) => match[1])));
}

function generatorLabel(version: SummaryVersion | null) {
  if (!version?.generator_provider) return "人工编辑";
  if (version.generator_provider === "builtin-extractive") return "规则摘录 · 未使用模型";
  return `真实模型 · ${version.actual_model || "模型标识缺失"}`;
}

function coverageWarnings(coverage: Record<string, unknown>) {
  return ["warnings", "generation_warnings"].flatMap((key) => {
    const value = coverage[key];
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  });
}

interface ResearchPanelProps {
  caseId: string;
  archived: boolean;
  activeSummaryId: string | null;
  onActiveSummaryChange: (summaryId: string | null) => void;
  refreshToken: number;
  retryTask: TaskItem | null;
  onRetryHandled: () => void;
}

export default function ResearchPanel({
  caseId,
  archived,
  activeSummaryId,
  onActiveSummaryChange,
  refreshToken,
  retryTask,
  onRetryHandled,
}: ResearchPanelProps) {
  const [summaries, setSummaries] = useState<ResearchSummary[]>([]);
  const [deliveries, setDeliveries] = useState<DeliveryVersion[]>([]);
  const [exportIssues, setExportIssues] = useState<{ issues: CitationIssue[]; format: "markdown" | "docx" } | null>(null);
  const [detail, setDetail] = useState<SummaryDetail | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editorBody, setEditorBody] = useState("");
  const [dirty, setDirty] = useState(false);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [issues, setIssues] = useState<CitationIssue[]>([]);
  const [validationRequested, setValidationRequested] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<"create" | "save" | "generate" | "confirm" | "export" | "remove" | "retry" | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("研究摘要");
  const [newQuestion, setNewQuestion] = useState("");
  const [pendingRemoval, setPendingRemoval] = useState<{ handle: string; affected: AffectedCitation[] } | null>(null);
  const [retrySummaryTask, setRetrySummaryTask] = useState<TaskItem | null>(null);
  const [generationTask, setGenerationTask] = useState<TaskItem | null>(null);
  const [watchedTaskId, setWatchedTaskId] = useState<string | null>(null);
  const [watchVersion, setWatchVersion] = useState(0);
  const [previewSource, setPreviewSource] = useState<SummarySource | null>(null);
  const [previewEvidence, setPreviewEvidence] = useState<EvidenceDetail | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const detailRequestRef = useRef(0);
  const previewRequestRef = useRef(0);
  const generateKeyRef = useRef<string | null>(null);

  const applyDetail = useCallback((next: SummaryDetail) => {
    setDetail(next);
    setIssues(next.issues);
    setSummaries((current) => {
      const exists = current.some((item) => item.id === next.summary.id);
      const items = exists
        ? current.map((item) => item.id === next.summary.id ? next.summary : item)
        : [next.summary, ...current];
      return items.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    });
  }, []);

  useEffect(() => {
    api.capabilities().then(setCapabilities).catch(() => setCapabilities(null));
  }, []);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setDetail(null);
    setError("");
    setNotice("");
    setIssues([]);
    setGenerationTask(null);
    setWatchedTaskId(null);
    setPreviewSource(null);
    api.listSummaries(caseId)
      .then((result) => {
        if (!current) return;
        setSummaries(result.items);
        const requestedExists = activeSummaryId && result.items.some((item) => item.id === activeSummaryId);
        const target = requestedExists ? activeSummaryId : result.items[0]?.id || null;
        if (target !== activeSummaryId) onActiveSummaryChange(target);
        if (!target) setLoading(false);
      })
      .catch((cause) => {
        if (current) {
          setError(cause instanceof Error ? cause.message : "研究摘要加载失败");
          setLoading(false);
        }
      });
    return () => { current = false; };
  }, [caseId, refreshToken]);

  useEffect(() => {
    let current = true;
    api.listDeliveries(caseId)
      .then((result) => { if (current) setDeliveries(result.items); })
      .catch(() => { if (current) setDeliveries([]); });
    return () => { current = false; };
  }, [caseId, refreshToken]);

  useEffect(() => {
    const requestId = ++detailRequestRef.current;
    setGenerationTask(null);
    setWatchedTaskId(null);
    setPreviewSource(null);
    setPreviewEvidence(null);
    if (!activeSummaryId) {
      setDetail(null);
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    api.getSummary(activeSummaryId)
      .then((result) => {
        if (requestId !== detailRequestRef.current) return;
        applyDetail(result);
        setWatchedTaskId(result.summary.latest_task_id);
        setError("");
      })
      .catch((cause) => {
        if (requestId === detailRequestRef.current) setError(cause instanceof Error ? cause.message : "摘要详情加载失败");
      })
      .finally(() => {
        if (requestId === detailRequestRef.current) {
          setDetailLoading(false);
          setLoading(false);
        }
      });
  }, [activeSummaryId, caseId, refreshToken, applyDetail]);

  useEffect(() => {
    setEditorBody(detail?.current_version?.body || "");
    setDirty(false);
    setSelectedVersionId(detail?.current_version?.id || null);
    setValidationRequested(false);
    setExportIssues(null);
    generateKeyRef.current = null;
  }, [detail?.summary.id, detail?.current_version?.id]);

  useEffect(() => {
    if (!retryTask || retryTask.task_type !== "summarize" || retryTask.case_id !== caseId) return;
    const summaryId = retryTask.input_snapshot.summary_id;
    if (typeof summaryId === "string") onActiveSummaryChange(summaryId);
    setRetrySummaryTask(retryTask);
    onRetryHandled();
  }, [retryTask, caseId, onActiveSummaryChange, onRetryHandled]);

  useEffect(() => {
    if (!watchedTaskId || !activeSummaryId) return;
    let current = true;
    let timer: number | undefined;
    async function poll() {
      try {
        const task = await api.getTask(watchedTaskId as string);
        if (!current) return;
        setGenerationTask(task);
        if (task.status === "queued" || task.status === "running") {
          timer = window.setTimeout(() => void poll(), 1000);
          return;
        }
        const fresh = await api.getSummary(activeSummaryId as string);
        if (!current) return;
        applyDetail(fresh);
        if (task.status === "completed") setNotice("生成完成，已追加新草稿版本；请核对引用后人工确认");
        if (task.status === "failed") setError(task.error || "摘要生成失败，当前正文与来源未改变");
      } catch (cause) {
        if (current) setError(cause instanceof Error ? cause.message : "生成任务状态读取失败");
      }
    }
    void poll();
    return () => {
      current = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [watchedTaskId, watchVersion, activeSummaryId, applyDetail]);

  const selectedVersion = useMemo(() => {
    if (!detail) return null;
    return detail.versions.find((version) => version.id === selectedVersionId) || detail.current_version;
  }, [detail, selectedVersionId]);
  const viewingCurrent = Boolean(selectedVersion && selectedVersion.id === detail?.current_version?.id) || (!selectedVersion && !detail?.current_version);
  const displayedBody = viewingCurrent ? editorBody : selectedVersion?.body || "";
  const citationMap = useMemo(() => {
    const map: Record<string, SummarySource> = {};
    if (selectedVersion) Object.assign(map, selectedVersion.citations);
    if (viewingCurrent) {
      for (const source of detail?.sources || []) map[source.handle] = source;
    }
    return map;
  }, [selectedVersion, viewingCurrent, detail?.sources]);
  const handles = citationHandles(displayedBody);
  const warnings = coverageWarnings(detail?.coverage || {});
  const generating = generationTask?.status === "queued" || generationTask?.status === "running";

  function updateBody(body: string) {
    setEditorBody(body);
    setDirty(true);
    setValidationRequested(false);
  }

  function insertText(text: string) {
    if (!viewingCurrent || archived) return;
    const textarea = editorRef.current;
    const start = textarea?.selectionStart ?? editorBody.length;
    const end = textarea?.selectionEnd ?? start;
    updateBody(`${editorBody.slice(0, start)}${text}${editorBody.slice(end)}`);
    window.requestAnimationFrame(() => {
      textarea?.focus();
      textarea?.setSelectionRange(start + text.length, start + text.length);
    });
  }

  async function openSource(source: SummarySource) {
    const requestId = ++previewRequestRef.current;
    setPreviewSource(source);
    setPreviewEvidence(null);
    setPreviewError("");
    if (source.kind !== "evidence") return;
    setPreviewLoading(true);
    try {
      const evidence = await api.getEvidence(source.source_id);
      if (requestId === previewRequestRef.current) setPreviewEvidence(evidence);
    } catch (cause) {
      if (requestId === previewRequestRef.current) {
        setPreviewError(`${cause instanceof Error ? cause.message : "证据原文读取失败"}；以下展示该版本冻结的来源文本。`);
      }
    } finally {
      if (requestId === previewRequestRef.current) setPreviewLoading(false);
    }
  }

  function openCitation(handle: string) {
    const source = citationMap[handle];
    if (!source) {
      setPreviewSource(null);
      setPreviewEvidence(null);
      setPreviewError(`引用 ${handle} 没有来源快照，无法打开原文。`);
      return;
    }
    void openSource(source);
  }

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    if (!newTitle.trim() || archived) return;
    setAction("create");
    setError("");
    try {
      const created = await api.createSummary(caseId, {
        title: newTitle.trim(),
        question: newQuestion.trim(),
        sources: [],
      });
      applyDetail(created);
      onActiveSummaryChange(created.summary.id);
      setCreateOpen(false);
      setNewTitle("研究摘要");
      setNewQuestion("");
      setNotice("空白研究摘要已创建，可加入来源或手工编写");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "研究摘要创建失败");
    } finally {
      setAction(null);
    }
  }

  async function handleSave() {
    if (!detail || !viewingCurrent || archived) return;
    setAction("save");
    setError("");
    try {
      const saved = await api.saveSummaryVersion(detail.summary.id, editorBody, detail.current_version?.id || null);
      applyDetail(saved);
      setNotice("已追加人工编辑版本，旧版本与来源快照保持不变");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "摘要保存失败");
    } finally {
      setAction(null);
    }
  }

  async function handleGenerate() {
    if (!detail || detail.sources.length === 0 || archived) return;
    setAction("generate");
    setError("");
    setNotice("");
    if (!generateKeyRef.current) generateKeyRef.current = createRequestKey();
    try {
      const task = await api.generateSummary(detail.summary.id, generateKeyRef.current);
      generateKeyRef.current = null;
      setGenerationTask(task);
      setWatchedTaskId(task.id);
      setWatchVersion((value) => value + 1);
      setNotice("生成任务已创建；只有完整成功并通过引用校验后才会追加版本");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "摘要生成任务创建失败");
    } finally {
      setAction(null);
    }
  }

  async function handleExport(format: "markdown" | "docx", keepIssues: boolean) {
    if (!detail || archived) return;
    setAction("export");
    setError("");
    try {
      const delivery = await api.createExport(detail.summary.id, format, keepIssues);
      setExportIssues(null);
      setDeliveries((current) => [delivery, ...current]);
      setNotice(
        delivery.status === "official"
          ? `已导出正式交付版本（${delivery.format === "markdown" ? "Markdown" : "DOCX"}）；交付文件不可变，后续修改请重新导出`
          : `已导出草稿交付版本，文件与交付列表已醒目标记 ${delivery.issues.length} 个未核实问题`
      );
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.issues.length > 0) {
        setExportIssues({ issues: cause.issues, format });
        setError("");
      } else {
        setError(cause instanceof Error ? cause.message : "导出失败");
      }
    } finally {
      setAction(null);
    }
  }

  async function handleConfirm() {
    if (!detail?.current_version || dirty || !viewingCurrent || archived) return;
    setAction("confirm");
    setValidationRequested(true);
    setError("");
    try {
      const confirmed = await api.confirmSummary(detail.summary.id);
      applyDetail(confirmed);
      setNotice("当前版本已标记为人工复核；这不构成最终法律结论");
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.issues.length > 0) setIssues(cause.issues);
      setError(cause instanceof Error ? cause.message : "摘要确认失败");
    } finally {
      setAction(null);
    }
  }

  async function removeSource(handle: string, confirmed = false) {
    if (!detail || archived) return;
    setAction("remove");
    setError("");
    try {
      const updated = await api.updateSummarySources(detail.summary.id, {
        add: [],
        remove: [handle],
        confirm_remove: confirmed,
      });
      applyDetail(updated);
      setPendingRemoval(null);
      setNotice("来源已从当前研究选择移除；历史版本仍保留原文快照");
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.code === "source_removal_requires_confirmation") {
        setPendingRemoval({ handle, affected: cause.affected });
      } else {
        setError(cause instanceof Error ? cause.message : "来源移除失败");
      }
    } finally {
      setAction(null);
    }
  }

  async function handleRetry() {
    if (!retrySummaryTask || archived) return;
    setAction("retry");
    setError("");
    try {
      const task = await api.retryTask(retrySummaryTask.id);
      setRetrySummaryTask(null);
      setGenerationTask(task);
      setWatchedTaskId(task.id);
      setWatchVersion((value) => value + 1);
      setNotice("已使用原始冻结快照创建新 Attempt，旧错误与版本仍保留");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "摘要生成重试失败");
    } finally {
      setAction(null);
    }
  }

  function copyHistoricalVersion() {
    if (!selectedVersion || !detail?.current_version || viewingCurrent || archived) return;
    setSelectedVersionId(detail.current_version.id);
    setEditorBody(selectedVersion.body);
    setDirty(true);
    setNotice("历史正文已复制到当前编辑器；保存后会追加新版本");
  }

  return (
    <section className="research-panel" aria-labelledby="research-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">RESEARCH SUMMARIES</span>
          <h3 id="research-title">证据查看与研究摘要</h3>
          <p>组合不同 Attempt 的证据和材料段落；引用始终可回到真实来源。</p>
        </div>
        {!archived && <button className="button primary" type="button" onClick={() => setCreateOpen(true)}>新建摘要</button>}
      </div>

      <div className="research-boundary">
        <strong>{capabilities?.summary?.default_provider === "openai-compatible" ? "已显式启用真实模型" : "默认规则摘录，不调用模型"}</strong>
        <span>规则结果仅供整理，模型结果也必须人工复核；确认不代表最终法律结论。</span>
      </div>

      {(error || notice) && (
        <div className={`message ${error ? "error" : "success"}`} role={error ? "alert" : "status"}>
          <span>{error || notice}</span>
          <button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="关闭研究提示">×</button>
        </div>
      )}

      <div className="research-layout">
        <aside className="summary-list" aria-label="研究摘要列表">
          <div className="result-section-heading"><h4>研究摘要</h4><span>{summaries.length}</span></div>
          {loading ? <div className="empty-state compact">正在加载摘要…</div> : summaries.length === 0 ? (
            <div className="empty-state compact"><strong>暂无研究摘要</strong><span>从证据或材料加入时会自动创建，也可空白新建。</span></div>
          ) : summaries.map((summary) => (
            <button type="button" className={`summary-list-item ${activeSummaryId === summary.id ? "selected" : ""}`} key={summary.id} onClick={() => onActiveSummaryChange(summary.id)}>
              <strong>{summary.title}</strong>
              <span>{summary.current_version_number ? `第 ${summary.current_version_number} 版` : "尚未保存正文"} · {summary.current_sources.length} 个来源</span>
              <small>{formatDate(summary.updated_at)}</small>
            </button>
          ))}
        </aside>

        <div className="research-workspace">
          {detailLoading && !detail ? <div className="empty-state compact">正在读取摘要与版本…</div> : !detail ? (
            <div className="empty-state compact"><strong>选择或创建研究摘要</strong><span>没有来源时也可手工编写，自动生成至少需要一条来源。</span></div>
          ) : (
            <>
              <div className="summary-heading">
                <div><h4>{detail.summary.title}</h4><p>{detail.summary.question || "未填写研究问题，可在生成前通过新建摘要明确问题。"}</p></div>
                {detail.current_version && <span className={`summary-status ${detail.current_version.status}`}>{versionStatusLabels[detail.current_version.status]}</span>}
              </div>

              {generationTask && (
                <div className={`summary-task-state ${generationTask.status}`}>
                  <span className={`task-status ${generationTask.status}`}>{taskStatusLabels[generationTask.status] || generationTask.status}</span>
                  <div><strong>{generationTask.current_stage}</strong><span>第 {generationTask.attempt_number} 次执行 · {generationTask.progress_detail}</span></div>
                </div>
              )}

              <section className="research-sources" aria-labelledby="research-sources-title">
                <div className="result-section-heading"><h4 id="research-sources-title">当前研究来源</h4><span>{detail.sources.length} 个 · 稳定句柄不会因重试改变</span></div>
                {detail.sources.length === 0 ? <div className="empty-state compact">从检索证据或材料段落加入来源；也可直接手工编写。</div> : (
                  <div className="research-source-list">
                    {detail.sources.map((source) => (
                      <article className="research-source-card" key={source.handle}>
                        <div className="research-source-heading"><strong>{source.handle}</strong><span>{source.kind === "evidence" ? "检索证据" : "材料段落"}</span>{source.origin_status === "partial" && <span className="partial-badge">部分覆盖</span>}</div>
                        <h5>{source.title}</h5>
                        <p>{source.source_label} · {locatorText(source.locator)}</p>
                        {source.attempt_id && <small>Attempt {source.attempt_id}</small>}
                        <div className="research-source-actions">
                          <button className="button secondary" type="button" onClick={() => void openSource(source)}>查看原文</button>
                          {!archived && <button className="button secondary" type="button" disabled={!viewingCurrent} onClick={() => insertText(`[[${source.handle}]]`)}>插入引用</button>}
                          {!archived && <button className="button danger-quiet" type="button" disabled={action !== null} onClick={() => void removeSource(source.handle)}>移除</button>}
                        </div>
                      </article>
                    ))}
                  </div>
                )}
                {warnings.map((warning) => <p className="coverage-warning" key={warning}>{warning}</p>)}
              </section>

              <div className="research-editor-layout">
                <section className="summary-editor" aria-labelledby="summary-editor-title">
                  <div className="result-section-heading"><h4 id="summary-editor-title">正文与版本</h4><span>{viewingCurrent ? generatorLabel(detail.current_version) : "历史版本只读"}</span></div>
                  <div className="summary-version-list">
                    {detail.versions.length === 0 ? <span>尚无保存版本</span> : detail.versions.map((version) => (
                      <button type="button" className={selectedVersion?.id === version.id ? "selected" : ""} key={version.id} onClick={() => { setSelectedVersionId(version.id); setPreviewSource(null); setPreviewError(""); }}>
                        <strong>第 {version.version_number} 版</strong>
                        <span>{versionStatusLabels[version.status]} · {formatDate(version.saved_at)}</span>
                        <small>{generatorLabel(version)}</small>
                      </button>
                    ))}
                  </div>

                  {!viewingCurrent && !archived && <button className="button secondary" type="button" onClick={copyHistoricalVersion} disabled={!detail.current_version}>复制到当前编辑器</button>}
                  {!viewingCurrent && !detail.current_version && <p className="field-help">历史版本不可直接覆盖，请先保存一个当前版本。</p>}

                  {viewingCurrent && !archived && (
                    <div className="citation-tools">
                      <span>插入：</span>
                      {detail.sources.map((source) => <button type="button" key={source.handle} onClick={() => insertText(`[[${source.handle}]]`)}>[[{source.handle}]]</button>)}
                      <button type="button" onClick={() => insertText("\n\n人工说明：")}>添加人工说明</button>
                    </div>
                  )}

                  <textarea
                    ref={editorRef}
                    className="summary-body-input"
                    value={displayedBody}
                    onChange={(event) => updateBody(event.target.value)}
                    readOnly={!viewingCurrent || archived}
                    maxLength={200000}
                    placeholder="可手工编写研究摘要，并使用 [[S1]] 引用已选来源。空白正文可以保存为草稿，但不能确认。"
                    aria-label="研究摘要正文"
                  />

                  {handles.length > 0 && (
                    <div className="citation-list">
                      <span>正文引用</span>
                      {handles.map((handle) => {
                        const source = citationMap[handle];
                        return (
                          <span className={`citation-chip ${source ? "" : "invalid"}`} key={handle}>
                            <button type="button" onClick={() => openCitation(handle)}>[[{handle}]]</button>
                            {viewingCurrent && !archived && <button type="button" onClick={() => updateBody(editorBody.split(`[[${handle}]]`).join(""))} aria-label={`删除引用 ${handle}`}>×</button>}
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {issues.length > 0 && (detail.current_version || validationRequested) && viewingCurrent && (
                    <div className="summary-issues" role="alert"><strong>确认前需要处理</strong>{issues.map((issue, index) => <p key={`${issue.code}-${issue.handle}-${index}`}>{issue.message}</p>)}</div>
                  )}

                  {exportIssues && viewingCurrent && (
                    <div className="summary-issues delivery-issues" role="alert">
                      <strong>导出前校验发现问题（{exportIssues.format === "markdown" ? "Markdown" : "DOCX"}）</strong>
                      {exportIssues.issues.map((issue, index) => <p key={`${issue.code}-${issue.handle}-${index}`}>{issue.message}</p>)}
                      <div className="delivery-issue-actions">
                        <span>可保留问题导出草稿，文件与交付列表将醒目标记为草稿。</span>
                        <button className="button danger-quiet" type="button" disabled={action !== null} onClick={() => void handleExport(exportIssues.format, true)}>{action === "export" ? "正在导出…" : "保留问题并导出草稿"}</button>
                      </div>
                    </div>
                  )}

                  {!archived && (
                    <div className="summary-editor-actions">
                      <span>{dirty ? "有未保存修改；确认前请先保存为新版本。" : "保存与重新生成都会追加版本，不覆盖历史。"}</span>
                      <div>
                        <button className="button secondary" type="button" disabled={!viewingCurrent || action !== null || generating} onClick={() => void handleSave()}>{action === "save" ? "正在保存…" : "保存为新版本"}</button>
                        <button className="button secondary" type="button" disabled={detail.sources.length === 0 || dirty || action !== null || generating} onClick={() => void handleGenerate()}>{action === "generate" ? "正在创建…" : generating ? "正在生成…" : "重新生成"}</button>
                        <button className="button primary" type="button" disabled={!detail.current_version || !viewingCurrent || dirty || action !== null || generating || detail.current_version.status === "confirmed"} onClick={() => void handleConfirm()}>{action === "confirm" ? "正在校验…" : detail.current_version?.status === "confirmed" ? "已人工复核" : "确认当前版本"}</button>
                      </div>
                    </div>
                  )}
                  {!archived && (
                    <div className="summary-export-actions">
                      <span>{detail.current_version?.status === "confirmed" && !dirty ? "导出生成不可变交付文件，记录校验结论、任务与实际模型。" : "仅已人工复核且无未保存修改的当前版本可导出；请先确认版本。"}</span>
                      <div>
                        <button className="button secondary" type="button" disabled={!detail.current_version || !viewingCurrent || dirty || action !== null || detail.current_version.status !== "confirmed"} onClick={() => void handleExport("markdown", false)}>{action === "export" ? "正在导出…" : "导出 Markdown"}</button>
                        <button className="button secondary" type="button" disabled={!detail.current_version || !viewingCurrent || dirty || action !== null || detail.current_version.status !== "confirmed"} onClick={() => void handleExport("docx", false)}>导出 DOCX</button>
                      </div>
                    </div>
                  )}
                </section>

                <aside className="research-source-preview" aria-label="引用原文核验">
                  {previewLoading ? <div className="empty-state compact">正在读取真实证据原文…</div> : previewSource ? (
                    <SourcePreview
                      title={previewSource.title}
                      handle={previewSource.handle}
                      sourceLabel={previewSource.source_label}
                      locator={previewEvidence?.evidence.locator || previewSource.locator}
                      text={previewEvidence ? previewEvidence.document?.content || previewEvidence.evidence.content || previewEvidence.evidence.excerpt : previewSource.text}
                      contentLabel={previewEvidence ? "实时核验来源原文" : previewSource.kind === "material_segment" ? "不可变材料段落快照" : "版本来源快照"}
                      cached={previewEvidence?.document?.cached}
                      sourceError={previewError || (previewEvidence?.source_error ? `${previewEvidence.source_error}；已保存内容仍可核验。` : null)}
                      hitReason={previewEvidence?.evidence.hit_reason}
                      matchType={previewEvidence?.evidence.match_type}
                      partialMatch={previewEvidence?.evidence.partial_match}
                      score={previewEvidence?.evidence.score}
                      details={{ source_ref: previewSource.source_ref, attempt_id: previewSource.attempt_id, parse_version_id: previewSource.parse_version_id }}
                      onClose={() => { setPreviewSource(null); setPreviewEvidence(null); setPreviewError(""); }}
                    />
                  ) : previewError ? <div className="summary-issues" role="alert">{previewError}</div> : (
                    <div className="empty-state compact"><strong>点击引用或来源查看原文</strong><span>未知引用只显示错误，不会伪造原文或高亮。</span></div>
                  )}
                </aside>
              </div>

              <section className="delivery-list" aria-labelledby="delivery-list-title">
                <div className="result-section-heading"><h4 id="delivery-list-title">交付版本</h4><span>{deliveries.length} 次导出 · 交付文件不可变</span></div>
                {deliveries.length === 0 ? <div className="empty-state compact">尚无导出；确认当前版本后可导出 Markdown 或 DOCX。</div> : (
                  <div className="delivery-items">
                    {deliveries.map((item) => (
                      <article className={`delivery-item ${item.status}`} key={item.id}>
                        <div className="delivery-item-heading">
                          <span className={`delivery-format ${item.format}`}>{item.format === "markdown" ? "Markdown" : "DOCX"}</span>
                          <strong>{item.summary_title} · 第 {item.version_number} 版</strong>
                          {item.status === "official"
                            ? <span className="delivery-status official">正式交付</span>
                            : <span className="delivery-status draft">草稿 · {item.issues.length} 个未核实问题</span>}
                        </div>
                        <p>{formatDate(item.exported_at)} · {(item.size_bytes / 1024).toFixed(1)} KB · sha256 {item.sha256.slice(0, 12)}…</p>
                        <a className="button secondary" href={`/api/deliveries/${item.id}/download`} download>下载</a>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </div>

      {createOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && action === null) setCreateOpen(false); }}>
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="create-summary-title">
            <div className="dialog-heading"><div><span className="eyebrow">NEW RESEARCH SUMMARY</span><h2 id="create-summary-title">新建研究摘要</h2></div><button className="icon-button" type="button" onClick={() => setCreateOpen(false)} aria-label="关闭新建摘要">×</button></div>
            <form onSubmit={(event) => void handleCreate(event)}>
              <label className="field-label" htmlFor="summary-title-input">摘要标题</label>
              <input id="summary-title-input" className="text-input" value={newTitle} onChange={(event) => setNewTitle(event.target.value)} maxLength={160} required />
              <label className="field-label summary-question-label" htmlFor="summary-question-input">研究问题（可选）</label>
              <textarea id="summary-question-input" className="search-question" value={newQuestion} onChange={(event) => setNewQuestion(event.target.value)} maxLength={2000} placeholder="例如：合同解除后的违约责任和赔偿范围是什么？" />
              <p className="field-help">创建后可从任意检索 Attempt 和材料段落显式加入来源。</p>
              <div className="dialog-actions"><button className="button secondary" type="button" onClick={() => setCreateOpen(false)}>取消</button><button className="button primary" type="submit" disabled={!newTitle.trim() || action !== null}>{action === "create" ? "正在创建…" : "创建摘要"}</button></div>
            </form>
          </section>
        </div>
      )}

      {pendingRemoval && (
        <div className="dialog-backdrop" role="presentation">
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="remove-source-title">
            <div className="dialog-heading"><div><span className="eyebrow">CITATION IMPACT</span><h2 id="remove-source-title">确认移除 {pendingRemoval.handle}</h2></div></div>
            <p className="retry-description">当前第 {pendingRemoval.affected[0]?.version_number || "—"} 版正文仍引用该来源。移除后可保留无效草稿，但修正引用前不能确认；历史版本快照不会删除。</p>
            <div className="dialog-actions"><button className="button secondary" type="button" onClick={() => setPendingRemoval(null)} disabled={action !== null}>取消</button><button className="button danger-quiet" type="button" onClick={() => void removeSource(pendingRemoval.handle, true)} disabled={action !== null}>{action === "remove" ? "正在移除…" : "确认移除来源"}</button></div>
          </section>
        </div>
      )}

      {retrySummaryTask && (
        <div className="dialog-backdrop" role="presentation">
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="retry-summary-title">
            <div className="dialog-heading"><div><span className="eyebrow">NEW SUMMARY ATTEMPT</span><h2 id="retry-summary-title">重试摘要生成</h2></div><button className="icon-button" type="button" onClick={() => setRetrySummaryTask(null)} aria-label="关闭摘要重试">×</button></div>
            <p className="retry-description">将严格沿用原任务冻结的问题、来源和基线版本，不允许修改输入快照。重试新增 Attempt，旧错误和历史摘要不会被覆盖。</p>
            <div className="dialog-actions"><button className="button secondary" type="button" onClick={() => setRetrySummaryTask(null)} disabled={action !== null}>取消</button><button className="button primary" type="button" onClick={() => void handleRetry()} disabled={action !== null}>{action === "retry" ? "正在创建…" : "按原快照重试"}</button></div>
          </section>
        </div>
      )}
    </section>
  );
}
