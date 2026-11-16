import { useEffect, useState } from "react";
import { api } from "./api";
import type { EvidenceDetail, EvidenceItem, SearchAttemptView } from "./types";

const sourceLabels: Record<string, string> = {
  law: "法规",
  judgments: "裁判文书",
  guiding_cases: "指导案例",
  reference: "业务参考",
};

const groupLabels: Record<string, string> = {
  law: "法规依据",
  cases: "类案证据",
  reference: "业务参考",
};

const statusLabels: Record<string, string> = {
  running: "检索中",
  success: "成功",
  partial: "部分覆盖",
  failed: "失败",
};

function locatorText(locator: Record<string, unknown>) {
  const labels: Record<string, string> = {
    article: "条文",
    page: "页码",
    year: "年份",
    rowid: "文书定位",
    paragraph: "段落",
    line_start: "起始行",
    line_end: "结束行",
  };
  return Object.entries(locator)
    .map(([key, value]) => `${labels[key] || key} ${String(value)}`)
    .join(" · ") || "无定位信息";
}

export default function EvidencePanel({ attempt }: { attempt: SearchAttemptView }) {
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvidenceDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setSelectedEvidenceId(null);
    setDetail(null);
    setError("");
  }, [attempt.attempt_id]);

  useEffect(() => {
    if (!selectedEvidenceId) {
      setDetail(null);
      return;
    }
    let current = true;
    setLoading(true);
    setError("");
    api.getEvidence(selectedEvidenceId)
      .then((result) => {
        if (current) setDetail(result);
      })
      .catch((cause) => {
        if (current) setError(cause instanceof Error ? cause.message : "证据详情加载失败");
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => { current = false; };
  }, [selectedEvidenceId]);

  const sources = Object.values(attempt.sources || {});
  const evidenceCount = Object.values(attempt.evidence || {}).reduce((total, items) => total + items.length, 0);

  return (
    <div className="evidence-panel">
      <section className="search-plan" aria-labelledby="search-plan-title">
        <div className="result-section-heading"><h4 id="search-plan-title">检索计划与来源覆盖</h4><span>{evidenceCount} 条证据</span></div>
        {attempt.plan?.routing_reason ? (
          <>
            <p className="routing-reason">{attempt.plan.routing_reason}</p>
            <div className="query-block"><span>基础检索词</span><strong>{attempt.plan.base_query}</strong></div>
            {attempt.plan.query !== attempt.plan.base_query && <div className="query-block"><span>材料增强后的规划检索词</span><strong>{attempt.plan.query}</strong></div>}
            {attempt.plan.material_enrichment?.some((item) => item.terms.length > 0) && (
              <div className="enrichment-list">
                {attempt.plan.material_enrichment.map((item) => (
                  <div key={item.material_version_id}><span>材料版本 {item.material_version_id}</span><strong>{item.terms.length ? `提取：${item.terms.join("、")}` : "未提取增强词"}</strong><small>使用 {item.segment_ids.length} 个定位段，仅辅助构造检索词</small></div>
                ))}
              </div>
            )}
            {attempt.plan.uncovered_sources?.length > 0 && <p className="coverage-warning">本次未覆盖：{attempt.plan.uncovered_sources.map((source) => sourceLabels[source] || source).join("、")}</p>}
          </>
        ) : <div className="empty-state compact">检索计划正在生成，完成后会显示实际检索词。</div>}

        <div className="source-grid">
          {sources.map((source) => (
            <article className={`source-card ${source.status}`} key={source.source}>
              <div className="source-card-heading"><strong>{sourceLabels[source.source] || source.source}</strong><span className={`task-status ${source.status === "success" ? "completed" : source.status}`}>{statusLabels[source.status] || source.status}</span></div>
              <p>保存 {source.evidence_count} 条证据</p>
              {source.query && <div className="source-query"><span>来源检索词</span><strong>{source.query}</strong></div>}
              {source.executed_queries && source.executed_queries.length > 0 && (
                <div className="executed-queries"><span>实际提交</span>{source.executed_queries.map((item, index) => <code key={`${index}-${item.query || "query"}`}>{item.query || "—"}{item.kind ? ` · ${item.kind}` : ""}{item.status ? ` · ${item.status}` : ""}</code>)}</div>
              )}
              {source.filters && Object.values(source.filters).some((value) => value !== null && value !== "" && value !== undefined) && (
                <div className="source-filters"><span>实际过滤</span><code>{JSON.stringify(source.filters)}</code></div>
              )}
              {source.coverage && Object.keys(source.coverage).length > 0 && (
                <details className="coverage-details"><summary>来源覆盖详情</summary><pre>{JSON.stringify(source.coverage, null, 2)}</pre></details>
              )}
              {source.error && <p className="task-error">{source.error.code ? `${source.error.code} · ` : ""}{source.error.message || "来源检索失败"}</p>}
            </article>
          ))}
        </div>
        {attempt.error && <p className="task-error">{attempt.error}</p>}
      </section>

      <section className="evidence-results" aria-labelledby="evidence-results-title">
        <div className="result-section-heading"><h4 id="evidence-results-title">证据结果</h4><span>按本次 Attempt 隔离保存</span></div>
        {evidenceCount === 0 ? (
          <div className="empty-state compact"><strong>{sources.some((source) => source.status === "running") ? "正在检索来源" : "本次没有可用证据"}</strong><span>零命中与来源失败分别展示，不会生成推测性摘要。</span></div>
        ) : (["law", "cases", "reference"] as const).map((group) => {
          const items = attempt.evidence[group] || [];
          if (items.length === 0) return null;
          return (
            <div className="evidence-group" key={group}>
              <h5>{groupLabels[group]}<span>{items.length}</span></h5>
              <div className="evidence-card-list">
                {items.map((item) => <EvidenceCard item={item} selected={selectedEvidenceId === item.id} onSelect={() => setSelectedEvidenceId(item.id)} key={item.id} />)}
              </div>
            </div>
          );
        })}
      </section>

      {error && <p className="task-error" role="alert">{error}</p>}
      {loading && <div className="empty-state compact">正在读取证据原文…</div>}
      {detail && selectedEvidenceId === detail.evidence.id && (
        <section className="evidence-detail" aria-labelledby="evidence-detail-title">
          <div className="evidence-detail-heading"><div><span className="eyebrow">SOURCE VERIFICATION</span><h4 id="evidence-detail-title">{detail.evidence.title}</h4></div><button className="icon-button" type="button" onClick={() => setSelectedEvidenceId(null)} aria-label="关闭证据详情">×</button></div>
          <div className="evidence-detail-meta"><span>{detail.evidence.source_label}</span><span>{locatorText(detail.evidence.locator)}</span>{detail.document?.cached && <span>不可变原文缓存</span>}</div>
          <div className="hit-reason"><strong>命中原因</strong><p>{detail.evidence.hit_reason}</p><span>{detail.evidence.match_type}{detail.evidence.partial_match ? " · 部分匹配" : " · 完整匹配"}{detail.evidence.score !== null ? ` · 分数 ${detail.evidence.score}` : ""}</span></div>
          {detail.source_error && <p className="coverage-warning">{detail.source_error}；当前摘录与定位仍可核验。</p>}
          <div className="document-content"><strong>{detail.document ? "来源原文" : "已保存内容"}</strong><pre>{detail.document?.content || detail.evidence.content || detail.evidence.excerpt || "来源未返回正文"}</pre></div>
          <details className="coverage-details"><summary>来源引用与元数据</summary><pre>{JSON.stringify({ source_ref: detail.evidence.source_ref, metadata: detail.evidence.metadata, document: detail.document?.metadata || null }, null, 2)}</pre></details>
        </section>
      )}
    </div>
  );
}

function EvidenceCard({ item, selected, onSelect }: { item: EvidenceItem; selected: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={`evidence-card ${selected ? "selected" : ""}`} onClick={onSelect}>
      <div className="evidence-card-top"><span>#{item.rank} · {sourceLabels[item.source] || item.source}</span>{item.partial_match && <span className="partial-badge">部分匹配</span>}</div>
      <strong>{item.title}</strong>
      <small>{item.source_label}</small>
      <p>{item.excerpt || item.content || "无摘录，打开查看来源信息"}</p>
      <div className="evidence-hit"><span>命中：{item.hit_reason}</span><span>{locatorText(item.locator)}</span></div>
      <span className="evidence-open">查看定位与原文</span>
    </button>
  );
}
