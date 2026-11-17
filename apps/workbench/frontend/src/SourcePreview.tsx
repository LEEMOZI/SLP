import type { ReactNode } from "react";

export function locatorText(locator: Record<string, unknown>) {
  const labels: Record<string, string> = {
    article: "条文",
    page: "页码",
    year: "年份",
    rowid: "文书定位",
    paragraph: "段落",
    table: "表格",
    row: "行",
    line_start: "起始行",
    line_end: "结束行",
    char_start: "起始字符",
    char_end: "结束字符",
  };
  return Object.entries(locator)
    .map(([key, value]) => `${labels[key] || key} ${String(value)}`)
    .join(" · ") || "无定位信息";
}

interface SourcePreviewProps {
  title: string;
  sourceLabel: string;
  locator: Record<string, unknown>;
  text: string;
  handle?: string;
  contentLabel?: string;
  cached?: boolean;
  sourceError?: string | null;
  hitReason?: string;
  matchType?: string;
  partialMatch?: boolean;
  score?: number | null;
  details?: unknown;
  actions?: ReactNode;
  onClose: () => void;
}

export default function SourcePreview({
  title,
  sourceLabel,
  locator,
  text,
  handle,
  contentLabel = "来源原文",
  cached = false,
  sourceError,
  hitReason,
  matchType,
  partialMatch = false,
  score,
  details,
  actions,
  onClose,
}: SourcePreviewProps) {
  return (
    <section className="evidence-detail source-preview" aria-labelledby="source-preview-title">
      <div className="evidence-detail-heading">
        <div>
          <span className="eyebrow">SOURCE VERIFICATION</span>
          <h4 id="source-preview-title">{handle ? `${handle} · ` : ""}{title}</h4>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭来源原文">×</button>
      </div>
      <div className="evidence-detail-meta">
        <span>{sourceLabel}</span>
        <span>{locatorText(locator)}</span>
        {cached && <span>不可变原文缓存</span>}
      </div>
      {hitReason && (
        <div className="hit-reason">
          <strong>命中原因</strong>
          <p>{hitReason}</p>
          <span>{matchType || "来源命中"}{partialMatch ? " · 部分匹配" : " · 完整匹配"}{score !== null && score !== undefined ? ` · 分数 ${score}` : ""}</span>
        </div>
      )}
      {sourceError && <p className="coverage-warning">{sourceError}</p>}
      <div className="document-content">
        <strong>{contentLabel}</strong>
        <pre>{text || "来源未返回正文"}</pre>
      </div>
      {details !== undefined && (
        <details className="coverage-details">
          <summary>来源引用与元数据</summary>
          <pre>{JSON.stringify(details, null, 2)}</pre>
        </details>
      )}
      {actions && <div className="source-preview-actions">{actions}</div>}
    </section>
  );
}
