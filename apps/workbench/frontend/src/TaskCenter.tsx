import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  CaseItem,
  DemoOutcome,
  TaskAttempt,
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

const taskTypeLabels: Record<string, string> = {
  demo: "隔离演示",
  search: "统一检索",
  parse: "材料解析",
  generation: "内容生成",
};

const demoOutcomes: Array<{ value: DemoOutcome; label: string }> = [
  { value: "completed", label: "完成" },
  { value: "partial", label: "部分完成" },
  { value: "failed", label: "失败" },
  { value: "cancellable", label: "可取消" },
];

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatElapsed(start: string, end: string | null, active: boolean, fallback: string) {
  const startTime = new Date(start).getTime();
  const endTime = end ? new Date(end).getTime() : active ? Date.now() : new Date(fallback).getTime();
  const seconds = Math.max(0, Math.floor((endTime - startTime) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function taskElapsed(task: TaskItem) {
  if (task.status === "recovery_pending") return "待确认";
  return formatElapsed(
    task.started_at || task.created_at,
    task.finished_at,
    task.status === "queued" || task.status === "running",
    task.updated_at,
  );
}

function attemptElapsed(attempt: TaskAttempt) {
  return formatElapsed(
    attempt.started_at || attempt.created_at,
    attempt.finished_at,
    attempt.status === "queued" || attempt.status === "running",
    attempt.created_at,
  );
}

interface TaskCenterProps {
  cases: CaseItem[];
  caseId?: string;
  onRetryTask: (task: TaskItem) => void;
}

export default function TaskCenter({ cases, caseId, onRetryTask }: TaskCenterProps) {
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [caseFilter, setCaseFilter] = useState("all");
  const [demoCaseId, setDemoCaseId] = useState("");
  const [demoEnabled, setDemoEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [actionId, setActionId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [retryTask, setRetryTask] = useState<TaskItem | null>(null);
  const [scopeText, setScopeText] = useState("");
  const [retryError, setRetryError] = useState("");

  const effectiveCaseId = caseId || (caseFilter === "all" ? undefined : caseFilter);
  const activeCases = cases.filter((item) => !item.archived_at);
  const effectiveDemoCaseId = caseId || (caseFilter !== "all" ? caseFilter : demoCaseId || activeCases[0]?.id || "");
  const hasActiveTasks = tasks.some((task) => task.status === "queued" || task.status === "running");

  const loadTasks = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    try {
      const result = await api.listTasks(effectiveCaseId);
      setTasks(result.items);
      setDemoEnabled(result.demo_enabled);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "任务列表加载失败");
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [effectiveCaseId]);

  useEffect(() => {
    setSelectedTaskId(null);
    setDetail(null);
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    if (!hasActiveTasks) return;
    const timer = window.setInterval(() => void loadTasks(false), 1000);
    return () => window.clearInterval(timer);
  }, [hasActiveTasks, loadTasks]);

  useEffect(() => {
    if (!selectedTaskId) {
      setDetail(null);
      return;
    }
    let current = true;
    api.getTask(selectedTaskId)
      .then((result) => {
        if (current) setDetail(result);
      })
      .catch((cause) => {
        if (current) setError(cause instanceof Error ? cause.message : "任务详情加载失败");
      });
    return () => { current = false; };
  }, [selectedTaskId, tasks]);

  async function handleCancel(task: TaskItem) {
    if (!window.confirm(`确认取消“${task.title}”？已产生的部分结果会保留，底层计算可能不会立即停止。`)) return;
    setActionId(task.id);
    try {
      await api.cancelTask(task.id);
      setNotice("已请求取消，任务将在当前步骤结束后停止");
      setError("");
      await loadTasks(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "任务取消失败");
    } finally {
      setActionId(null);
    }
  }

  async function handleRetry(event: FormEvent) {
    event.preventDefault();
    if (!retryTask) return;
    let scope: Record<string, unknown> | undefined;
    if (scopeText.trim()) {
      try {
        const parsed = JSON.parse(scopeText);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("scope 必须是 JSON 对象");
        }
        scope = parsed;
      } catch (cause) {
        setRetryError(cause instanceof Error ? cause.message : "scope 不是有效的 JSON");
        return;
      }
    }
    setActionId(retryTask.id);
    try {
      await api.retryTask(retryTask.id, scope);
      setSelectedTaskId(retryTask.id);
      setRetryTask(null);
      setScopeText("");
      setRetryError("");
      setNotice("已创建新的执行记录，旧结果与错误仍保留在历史中");
      setError("");
      await loadTasks(false);
    } catch (cause) {
      setRetryError(cause instanceof Error ? cause.message : "任务重试失败");
    } finally {
      setActionId(null);
    }
  }

  async function handleCreateDemo(outcome: DemoOutcome) {
    if (!effectiveDemoCaseId) return;
    setActionId(`demo-${outcome}`);
    try {
      const created = await api.createDemoTask(effectiveDemoCaseId, outcome);
      setSelectedTaskId(created.id);
      setNotice("隔离演示任务已创建；正式环境不会显示此入口");
      setError("");
      await loadTasks(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "演示任务创建失败");
    } finally {
      setActionId(null);
    }
  }

  return (
    <section className={`task-center ${caseId ? "embedded" : ""}`} aria-labelledby={caseId ? "case-tasks-title" : "global-tasks-title"}>
      <div className="task-center-heading">
        <div>
          <span className="eyebrow">TASK CENTER</span>
          <h2 id={caseId ? "case-tasks-title" : "global-tasks-title"}>{caseId ? "案件任务" : "统一任务中心"}</h2>
          <p>查看执行阶段、已用时和每次执行记录；无法确认的百分比不会显示。</p>
        </div>
        {!caseId && (
          <label className="task-case-filter">
            <span>案件范围</span>
            <select value={caseFilter} onChange={(event) => setCaseFilter(event.target.value)}>
              <option value="all">全部案件</option>
              {cases.map((item) => <option value={item.id} key={item.id}>{item.name}{item.archived_at ? "（已归档）" : ""}</option>)}
            </select>
          </label>
        )}
      </div>

      {(error || notice) && (
        <div className={`message ${error ? "error" : "success"}`} role={error ? "alert" : "status"}>
          <span>{error || notice}</span>
          <button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="关闭任务提示">×</button>
        </div>
      )}

      {demoEnabled && (
        <div className="demo-task-panel">
          <div>
            <strong>隔离联调工具</strong>
            <span>仅由 SLP_WORKBENCH_ENABLE_TASK_DEMO=1 启用</span>
          </div>
          {!caseId && caseFilter === "all" && (
            <select aria-label="演示任务案件" value={effectiveDemoCaseId} onChange={(event) => setDemoCaseId(event.target.value)}>
              {activeCases.length === 0 && <option value="">暂无进行中案件</option>}
              {activeCases.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select>
          )}
          <div className="demo-task-actions">
            {demoOutcomes.map((outcome) => (
              <button
                className="button secondary"
                type="button"
                key={outcome.value}
                disabled={!effectiveDemoCaseId || actionId !== null}
                onClick={() => void handleCreateDemo(outcome.value)}
              >
                {actionId === `demo-${outcome.value}` ? "创建中…" : outcome.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="task-list">
        {loading ? (
          <div className="empty-state compact">正在加载任务…</div>
        ) : tasks.length === 0 ? (
          <div className="task-empty">
            <strong>暂无任务</strong>
            <span>检索、解析或生成能力启动后，执行状态会显示在这里。</span>
          </div>
        ) : tasks.map((task) => (
          <article className="task-card" key={task.id}>
            <div className="task-card-top">
              <div className="task-title-group">
                <span className="task-type">{taskTypeLabels[task.task_type] || task.task_type}</span>
                <h3>{task.title}</h3>
              </div>
              <span className={`task-status ${task.status}`}>{statusLabels[task.status]}</span>
            </div>
            <div className="task-meta">
              <span>所属案件：<strong>{task.case_name}</strong></span>
              <span>第 {task.attempt_number} 次执行</span>
              <span>已用时 {taskElapsed(task)}</span>
              {task.model_role && <span>模型角色：{task.model_role}</span>}
            </div>
            <div className="task-stage">
              <strong>{task.current_stage}</strong>
              <span>{task.progress_detail}</span>
            </div>
            {task.progress !== null && (
              <div className="task-progress" role="progressbar" aria-valuenow={task.progress} aria-valuemin={0} aria-valuemax={100} aria-label={`${task.title}进度`}>
                <div className="task-progress-track"><i style={{ width: `${task.progress}%` }} /></div>
                <span>{task.progress}%</span>
              </div>
            )}
            {task.error && <p className="task-error">{task.error}</p>}
            <div className="task-actions">
              <button className="button secondary" type="button" onClick={() => setSelectedTaskId(selectedTaskId === task.id ? null : task.id)}>
                {selectedTaskId === task.id ? "收起历史" : "执行历史"}
              </button>
              {(task.status === "queued" || task.status === "running") && (
                <button className="button danger-quiet" type="button" disabled={task.cancel_requested || actionId !== null} onClick={() => void handleCancel(task)}>
                  {task.cancel_requested ? "等待取消" : actionId === task.id ? "正在请求…" : "取消任务"}
                </button>
              )}
              {["failed", "partial", "cancelled", "recovery_pending"].includes(task.status) && !cases.some((item) => item.id === task.case_id && item.archived_at) && (
                <button className="button primary" type="button" disabled={actionId !== null} onClick={() => {
                  if (task.task_type === "parse" || task.task_type === "search") {
                    onRetryTask(task);
                  } else {
                    setRetryTask(task);
                    setScopeText("");
                    setRetryError("");
                  }
                }}>
                  重试任务
                </button>
              )}
            </div>

            {selectedTaskId === task.id && (
              <div className="task-history">
                {detail?.id === task.id ? (
                  <>
                    <details className="snapshot-details">
                      <summary>当前输入快照</summary>
                      <pre>{JSON.stringify(detail.input_snapshot, null, 2)}</pre>
                    </details>
                    {detail.result && <pre className="task-result">{JSON.stringify(detail.result, null, 2)}</pre>}
                    <h4>执行记录</h4>
                    {detail.attempts.map((attempt) => (
                      <div className="attempt-row" key={attempt.id}>
                        <div className="attempt-heading">
                          <strong>第 {attempt.attempt_number} 次</strong>
                          <span className={`task-status ${attempt.status}`}>{statusLabels[attempt.status]}</span>
                          <span>{attemptElapsed(attempt)}</span>
                        </div>
                        <p>{attempt.current_stage} · {attempt.progress_detail}</p>
                        <small>创建于 {formatDate(attempt.created_at)}{attempt.finished_at ? ` · 结束于 ${formatDate(attempt.finished_at)}` : ""}</small>
                        {attempt.error && <p className="task-error">{attempt.error}</p>}
                      </div>
                    ))}
                  </>
                ) : <div className="empty-state compact">正在加载执行历史…</div>}
              </div>
            )}
          </article>
        ))}
      </div>

      {retryTask && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && actionId === null) setRetryTask(null);
        }}>
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="retry-task-title">
            <div className="dialog-heading">
              <div>
                <span className="eyebrow">NEW ATTEMPT</span>
                <h2 id="retry-task-title">重试任务</h2>
              </div>
              <button className="icon-button" type="button" onClick={() => setRetryTask(null)} disabled={actionId !== null} aria-label="关闭重试">×</button>
            </div>
            <p className="retry-description">默认沿用原输入快照。每次重试都会新增执行记录，旧结果和错误不会被覆盖。</p>
            <form onSubmit={(event) => void handleRetry(event)}>
              <label className="field-label" htmlFor="retry-scope">修改执行范围（可选）</label>
              <textarea
                id="retry-scope"
                className="scope-input"
                value={scopeText}
                onChange={(event) => setScopeText(event.target.value)}
                placeholder={'留空沿用原输入；或输入 JSON 对象，例如 {"year": 2025}'}
                disabled={actionId !== null}
              />
              <p className="field-help">填写后会更新本次执行的 scope，不影响历史执行记录。</p>
              {retryError && <p className="task-error" role="alert">{retryError}</p>}
              <div className="dialog-actions">
                <button className="button secondary" type="button" onClick={() => setRetryTask(null)} disabled={actionId !== null}>取消</button>
                <button className="button primary" type="submit" disabled={actionId !== null}>{actionId === retryTask.id ? "正在创建…" : "创建新执行"}</button>
              </div>
            </form>
          </section>
        </div>
      )}
    </section>
  );
}
