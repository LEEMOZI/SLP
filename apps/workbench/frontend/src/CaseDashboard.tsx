import { useEffect, useState } from "react";
import type { CaseDashboardData, RecentTask } from "./types";
import { api } from "./api";

interface Props {
  caseId: string;
  refreshToken: number;
}

export default function CaseDashboard({ caseId, refreshToken }: Props) {
  const [data, setData] = useState<CaseDashboardData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!caseId) return;
    let current = true;
    api.caseDashboard(caseId)
      .then((r) => { if (current) { setData(r); setError(""); } })
      .catch((e) => { if (current) setError(e instanceof Error ? e.message : "加载失败"); });
    return () => { current = false; };
  }, [caseId, refreshToken]);

  if (error) return <div className="notice error" role="alert">{error}</div>;
  if (!data) return <div className="empty-state">加载中…</div>;

  const { stats } = data;

  return (
    <section className="panel dashboard-panel" aria-label="案件概览">
      <header className="panel-section-heading">
        <h4>案件概览</h4>
        <span>{data.case.name}</span>
      </header>

      <div className="dashboard-grid">
        <StatCard label="材料" value={stats.materials} icon="📄" />
        <StatCard label="检索" value={stats.searches} icon="🔍" />
        <StatCard
          label="研究摘要"
          value={stats.summaries}
          sub={stats.summaries_confirmed > 0 ? `${stats.summaries_confirmed} 已确认` : undefined}
          icon="📝"
        />
        <StatCard
          label="合同审查"
          value={stats.reviews}
          sub={stats.reviews_confirmed > 0 ? `${stats.reviews_confirmed} 已确认` : undefined}
          icon="📋"
        />
        <StatCard
          label="诉讼文书"
          value={stats.litigation_docs}
          sub={stats.litigation_confirmed > 0 ? `${stats.litigation_confirmed} 有版本` : undefined}
          icon="⚖️"
        />
        <StatCard label="交付" value={stats.deliveries} icon="📦" />
      </div>

      <div className="dashboard-tasks">
        <h4>任务状态</h4>
        <div className="task-status-bar">
          <div className="task-segment completed" style={{ flex: stats.tasks_completed || 1 }} title={`${stats.tasks_completed} 已完成`} />
          <div className="task-segment running" style={{ flex: stats.tasks_running || 0.5 }} title={`${stats.tasks_running} 进行中`} />
          <div className="task-segment failed" style={{ flex: stats.tasks_failed || 0.5 }} title={`${stats.tasks_failed} 失败`} />
        </div>
        <div className="task-status-legend">
          <span><i className="dot completed" /> 完成 {stats.tasks_completed}</span>
          <span><i className="dot running" /> 进行中 {stats.tasks_running}</span>
          <span><i className="dot failed" /> 失败 {stats.tasks_failed}</span>
          <span className="task-total">共 {stats.tasks_total} 个任务</span>
        </div>
      </div>

      {Object.keys(data.task_types).length > 0 && (
        <div className="dashboard-task-types">
          <h4>任务类型分布</h4>
          <div className="task-type-bar">
            {Object.entries(data.task_types).map(([type, count]) => (
              <div
                key={type}
                className={`task-type-segment type-${type}`}
                style={{ flex: count }}
                title={`${type}: ${count}`}
              />
            ))}
          </div>
          <div className="task-type-legend">
            {Object.entries(data.task_types).map(([type, count]) => (
              <span key={type}><i className={`dot type-${type}`} /> {type} {count}</span>
            ))}
          </div>
        </div>
      )}

      {data.recent_tasks.length > 0 && (
        <div className="dashboard-recent">
          <h4>最近活动</h4>
          <ul className="recent-timeline">
            {data.recent_tasks.map((task) => (
              <RecentTaskItem key={task.id} task={task} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function StatCard({ label, value, sub, icon }: { label: string; value: number; sub?: string; icon: string }) {
  return (
    <div className="stat-card">
      <span className="stat-icon" aria-hidden>{icon}</span>
      <div className="stat-body">
        <strong className="stat-value">{value}</strong>
        <span className="stat-label">{label}</span>
        {sub && <span className="stat-sub">{sub}</span>}
      </div>
    </div>
  );
}

const STATUS_ICONS: Record<string, string> = {
  completed: "✓",
  running: "▶",
  queued: "⏳",
  failed: "✗",
  partial: "◐",
  cancelled: "⊘",
};

function RecentTaskItem({ task }: { task: RecentTask }) {
  const icon = STATUS_ICONS[task.status] || "•";
  const time = new Date(task.updated_at).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
  return (
    <li className={`recent-item status-${task.status}`}>
      <span className="recent-status-icon" aria-hidden>{icon}</span>
      <span className="recent-title">{task.title}</span>
      <span className="recent-time">{time}</span>
    </li>
  );
}
