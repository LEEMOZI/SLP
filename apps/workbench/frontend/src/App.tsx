import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "./api";
import MaterialsPanel from "./MaterialsPanel";
import ResearchPanel from "./ResearchPanel";
import SearchPanel from "./SearchPanel";
import TaskCenter from "./TaskCenter";
import type { CaseItem, ReadinessReport, SourceReference, TaskItem } from "./types";

type WorkspaceTab = "materials" | "search" | "research" | "tasks";

function formatDate(value: string | null) {
  if (!value) return "尚未打开";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export default function App() {
  const [cases, setCases] = useState<CaseItem[]>([]);
  const [selectedCase, setSelectedCase] = useState<CaseItem | null>(null);
  const [readiness, setReadiness] = useState<ReadinessReport | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [loadingCases, setLoadingCases] = useState(true);
  const [checkingHealth, setCheckingHealth] = useState(true);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newCaseName, setNewCaseName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [view, setView] = useState<"workspace" | "tasks">("workspace");
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("materials");
  const [retryTask, setRetryTask] = useState<TaskItem | null>(null);
  const [activeSummaryId, setActiveSummaryId] = useState<string | null>(null);
  const [stagedSources, setStagedSources] = useState<SourceReference[]>([]);
  const [researchRefreshToken, setResearchRefreshToken] = useState(0);
  const [addingResearch, setAddingResearch] = useState(false);

  const loadCases = useCallback(async () => {
    setLoadingCases(true);
    try {
      const result = await api.listCases(includeArchived);
      setCases(result.items);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "案件列表加载失败");
    } finally {
      setLoadingCases(false);
    }
  }, [includeArchived]);

  const loadReadiness = useCallback(async () => {
    setCheckingHealth(true);
    try {
      setReadiness(await api.readiness());
      setError("");
    } catch (cause) {
      setReadiness(null);
      setError(cause instanceof Error ? cause.message : "服务就绪检查失败");
    } finally {
      setCheckingHealth(false);
    }
  }, []);

  useEffect(() => {
    void loadCases();
  }, [loadCases]);

  useEffect(() => {
    void loadReadiness();
  }, [loadReadiness]);

  useEffect(() => {
    setActiveSummaryId(null);
    setStagedSources([]);
    setResearchRefreshToken(0);
  }, [selectedCase?.id]);

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    if (!newCaseName.trim()) return;
    setSubmitting(true);
    try {
      const created = await api.createCase(newCaseName);
      const opened = await api.openCase(created.id);
      setNewCaseName("");
      setCreateDialogOpen(false);
      setSelectedCase(opened);
      setNotice(`案件“${created.name}”已创建`);
      setError("");
      await loadCases();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "案件创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleOpen(caseId: string) {
    try {
      const opened = await api.openCase(caseId);
      setSelectedCase(opened);
      setError("");
      await loadCases();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "案件打开失败");
    }
  }

  async function handleArchive() {
    if (!selectedCase || !window.confirm(`确认归档案件“${selectedCase.name}”？`)) return;
    try {
      const archived = await api.archiveCase(selectedCase.id);
      setSelectedCase(null);
      setNotice(`案件“${archived.name}”已归档`);
      setError("");
      await loadCases();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "案件归档失败");
    }
  }

  const handleToggleResearchSource = useCallback((source: SourceReference) => {
    setStagedSources((current) => {
      const exists = current.some((item) => item.kind === source.kind && item.id === source.id);
      return exists
        ? current.filter((item) => item.kind !== source.kind || item.id !== source.id)
        : [...current, source];
    });
  }, []);

  const handleAddResearchSources = useCallback((sources: SourceReference[]) => {
    if (!selectedCase || selectedCase.archived_at || sources.length === 0 || addingResearch) return;
    const uniqueSources = sources.filter((source, index) => sources.findIndex((item) => item.kind === source.kind && item.id === source.id) === index);
    setWorkspaceTab("research");
    setRetryTask(null);
    setAddingResearch(true);
    void (async () => {
      try {
        let summaryId = activeSummaryId;
        if (!summaryId) {
          const existing = await api.listSummaries(selectedCase.id);
          summaryId = existing.items[0]?.id || null;
        }
        if (summaryId) {
          await api.updateSummarySources(summaryId, { add: uniqueSources, remove: [] });
        } else {
          const created = await api.createSummary(selectedCase.id, {
            title: "研究摘要",
            question: "",
            sources: uniqueSources,
          });
          summaryId = created.summary.id;
        }
        setActiveSummaryId(summaryId);
        const addedKeys = new Set(uniqueSources.map((source) => `${source.kind}:${source.id}`));
        setStagedSources((current) => current.filter((source) => !addedKeys.has(`${source.kind}:${source.id}`)));
        setResearchRefreshToken((value) => value + 1);
        setNotice(`已将 ${uniqueSources.length} 个来源加入当前研究摘要`);
        setError("");
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "来源加入研究失败");
      } finally {
        setAddingResearch(false);
      }
    })();
  }, [selectedCase, activeSummaryId, addingResearch]);

  const handleRetryTask = useCallback((task: TaskItem) => {
    const target = cases.find((item) => item.id === task.case_id);
    if (!target) {
      setError("无法找到任务所属案件");
      return;
    }
    setSelectedCase(target);
    setView("workspace");
    setWorkspaceTab(task.task_type === "parse" ? "materials" : task.task_type === "summarize" ? "research" : "search");
    setRetryTask(task);
  }, [cases]);

  const clearRetryRequest = useCallback(() => setRetryTask(null), []);
  const activeCount = cases.filter((item) => !item.archived_at).length;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">SLP</div>
          <div>
            <h1>法律工作台</h1>
            <p>案件、证据与研究的统一工作空间</p>
          </div>
        </div>
        <nav className="app-nav" aria-label="工作台视图">
          <button type="button" className={view === "workspace" ? "active" : ""} onClick={() => setView("workspace")}>案件工作区</button>
          <button type="button" className={view === "tasks" ? "active" : ""} onClick={() => setView("tasks")}>任务中心</button>
        </nav>
        <div className="environment">
          <span className={`status-dot ${readiness?.status === "ready" ? "healthy" : "warning"}`} />
          <span>Mac Studio 本机环境</span>
          <span className="environment-divider" />
          <strong>{readiness ? `${readiness.available_count}/${readiness.total_count} 项服务可用` : "正在检查服务"}</strong>
        </div>
      </header>

      <main className="main-content">
        {(error || notice) && (
          <div className={`message ${error ? "error" : "success"}`} role={error ? "alert" : "status"}>
            <span>{error || notice}</span>
            <button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="关闭提示">×</button>
          </div>
        )}

        <section className="readiness-section" aria-labelledby="readiness-title">
          <div className="section-heading">
            <div>
              <span className="eyebrow">SYSTEM READINESS</span>
              <h2 id="readiness-title">服务就绪检查</h2>
              <p>检查不会影响现有服务；部分服务不可用时，仍可管理已有案件。</p>
            </div>
            <button className="button secondary" type="button" onClick={() => void loadReadiness()} disabled={checkingHealth}>
              {checkingHealth ? "正在检查…" : "重新检查"}
            </button>
          </div>
          <div className="service-grid">
            {readiness?.services.map((service) => (
              <article className="service-card" key={service.name}>
                <div className="service-card-top">
                  <span className={`status-dot ${service.status === "healthy" ? "healthy" : "unavailable"}`} />
                  <span className={`service-status ${service.status}`}>{service.status === "healthy" ? "正常" : "不可用"}</span>
                </div>
                <h3>{service.label}</h3>
                <p title={service.detail}>{service.detail}</p>
                <small>{service.name} · {service.latency_ms} ms</small>
              </article>
            )) || (
              <div className="readiness-unavailable">
                {checkingHealth ? "正在连接本机服务…" : "暂未获取服务状态，案件管理功能仍可使用。"}
              </div>
            )}
          </div>
        </section>

        {view === "tasks" ? (
          <TaskCenter cases={cases} onRetryTask={handleRetryTask} />
        ) : (
        <div className="workspace-layout">
          <aside className="case-panel" aria-labelledby="case-list-title">
            <div className="case-panel-heading">
              <div>
                <span className="eyebrow">CASE FILES</span>
                <h2 id="case-list-title">我的案件</h2>
              </div>
              <span className="count-badge">{includeArchived ? cases.length : activeCount}</span>
            </div>
            <button className="button primary full-width" type="button" onClick={() => setCreateDialogOpen(true)}>
              <span aria-hidden="true">＋</span> 新建案件
            </button>
            <label className="archive-filter">
              <input
                type="checkbox"
                checked={includeArchived}
                onChange={(event) => setIncludeArchived(event.target.checked)}
              />
              显示已归档案件
            </label>
            <div className="case-list">
              {loadingCases ? (
                <div className="empty-state compact">正在加载案件…</div>
              ) : cases.length === 0 ? (
                <div className="empty-state compact">
                  <strong>{includeArchived ? "暂无案件" : "暂无进行中的案件"}</strong>
                  <span>创建案件后，可在这里继续工作。</span>
                </div>
              ) : cases.map((item) => (
                <button
                  className={`case-list-item ${selectedCase?.id === item.id ? "selected" : ""} ${item.archived_at ? "archived" : ""}`}
                  type="button"
                  key={item.id}
                  onClick={() => {
                    setRetryTask(null);
                    if (item.archived_at) setSelectedCase(item);
                    else void handleOpen(item.id);
                  }}
                >
                  <span className="case-list-title">{item.name}</span>
                  <span className="case-list-date">{item.archived_at ? "已归档" : `更新于 ${formatDate(item.updated_at)}`}</span>
                </button>
              ))}
            </div>
          </aside>

          <section className="case-workspace" aria-label="案件详情">
            {selectedCase ? (
              <>
                <div className="workspace-heading">
                  <div>
                    <span className="eyebrow">CURRENT CASE</span>
                    <div className="case-title-row">
                      <h2>{selectedCase.name}</h2>
                      {selectedCase.archived_at && <span className="archived-badge">已归档</span>}
                    </div>
                    <p>案件编号 {selectedCase.id}</p>
                  </div>
                  {!selectedCase.archived_at && (
                    <button className="button danger-quiet" type="button" onClick={() => void handleArchive()}>归档案件</button>
                  )}
                </div>
                <div className="case-metadata">
                  <div><span>创建时间</span><strong>{formatDate(selectedCase.created_at)}</strong></div>
                  <div><span>最近打开</span><strong>{formatDate(selectedCase.last_opened_at)}</strong></div>
                  <div><span>最近更新</span><strong>{formatDate(selectedCase.updated_at)}</strong></div>
                </div>
                <div className="workspace-tabs" role="tablist" aria-label="案件功能">
                  <button type="button" role="tab" aria-selected={workspaceTab === "materials"} className={workspaceTab === "materials" ? "active" : ""} onClick={() => { setWorkspaceTab("materials"); setRetryTask(null); }}>材料</button>
                  <button type="button" role="tab" aria-selected={workspaceTab === "search"} className={workspaceTab === "search" ? "active" : ""} onClick={() => { setWorkspaceTab("search"); setRetryTask(null); }}>检索</button>
                  <button type="button" role="tab" aria-selected={workspaceTab === "research"} className={workspaceTab === "research" ? "active" : ""} onClick={() => { setWorkspaceTab("research"); setRetryTask(null); }}>研究</button>
                  <button type="button" role="tab" aria-selected={workspaceTab === "tasks"} className={workspaceTab === "tasks" ? "active" : ""} onClick={() => { setWorkspaceTab("tasks"); setRetryTask(null); }}>任务</button>
                </div>
                <div className="workspace-tab-content" role="tabpanel">
                  {workspaceTab === "materials" && (
                    <MaterialsPanel
                      caseId={selectedCase.id}
                      archived={Boolean(selectedCase.archived_at)}
                      retryTask={retryTask?.task_type === "parse" ? retryTask : null}
                      onRetryHandled={clearRetryRequest}
                      addingResearch={addingResearch}
                      onAddResearchSource={(source) => handleAddResearchSources([source])}
                    />
                  )}
                  {workspaceTab === "search" && (
                    <SearchPanel
                      caseId={selectedCase.id}
                      archived={Boolean(selectedCase.archived_at)}
                      retryTask={retryTask?.task_type === "search" ? retryTask : null}
                      onRetryHandled={clearRetryRequest}
                      stagedSources={stagedSources}
                      addingResearch={addingResearch}
                      onToggleResearchSource={handleToggleResearchSource}
                      onAddResearchSources={handleAddResearchSources}
                    />
                  )}
                  {workspaceTab === "research" && (
                    <ResearchPanel
                      caseId={selectedCase.id}
                      archived={Boolean(selectedCase.archived_at)}
                      activeSummaryId={activeSummaryId}
                      onActiveSummaryChange={setActiveSummaryId}
                      refreshToken={researchRefreshToken}
                      retryTask={retryTask?.task_type === "summarize" ? retryTask : null}
                      onRetryHandled={clearRetryRequest}
                    />
                  )}
                  {workspaceTab === "tasks" && <TaskCenter cases={cases} caseId={selectedCase.id} onRetryTask={handleRetryTask} />}
                </div>
              </>
            ) : (
              <div className="welcome-state">
                <span className="eyebrow">YOUR LEGAL WORKSPACE</span>
                <h2>从一个案件开始</h2>
                <p>选择左侧案件继续工作，或创建新案件。所有案件信息保存在工作台独立数据库中。</p>
                <button className="button primary" type="button" onClick={() => setCreateDialogOpen(true)}>创建第一个案件</button>
              </div>
            )}
          </section>
        </div>
        )}
      </main>

      {createDialogOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !submitting) setCreateDialogOpen(false);
        }}>
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="create-case-title">
            <div className="dialog-heading">
              <div>
                <span className="eyebrow">NEW CASE</span>
                <h2 id="create-case-title">创建新案件</h2>
              </div>
              <button className="icon-button" type="button" onClick={() => setCreateDialogOpen(false)} disabled={submitting} aria-label="关闭">×</button>
            </div>
            <form onSubmit={(event) => void handleCreate(event)}>
              <label className="field-label" htmlFor="case-name">案件名称</label>
              <input
                id="case-name"
                className="text-input"
                value={newCaseName}
                onChange={(event) => setNewCaseName(event.target.value)}
                placeholder="例如：某合同纠纷一审"
                maxLength={160}
                autoFocus
                disabled={submitting}
              />
              <p className="field-help">名称去除首尾空格后不可与现有案件重复，最多 160 个字符。</p>
              <div className="dialog-actions">
                <button className="button secondary" type="button" onClick={() => setCreateDialogOpen(false)} disabled={submitting}>取消</button>
                <button className="button primary" type="submit" disabled={submitting || !newCaseName.trim()}>
                  {submitting ? "正在创建…" : "创建案件"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
