import { useEffect, useMemo, useState } from "react";
import type { ContractReview, ContractReviewDetail, DeliveryVersion, Material, RiskItem } from "./types";
import { api, ApiRequestError } from "./api";

type RiskLevel = RiskItem["level"];
const RISK_LEVEL_ORDER: RiskLevel[] = ["高危", "中等", "提示"];
const RISK_LEVEL_CLASS: Record<RiskLevel, string> = {
  "高危": "risk-high",
  "中等": "risk-medium",
  "提示": "risk-low",
};

interface Props {
  caseId: string;
  archived: boolean;
  refreshToken: number;
}

export default function ContractReviewPanel({ caseId, archived, refreshToken }: Props) {
  const [reviews, setReviews] = useState<ContractReview[]>([]);
  const [detail, setDetail] = useState<ContractReviewDetail | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<null | "create" | "generate" | "save" | "confirm" | "export">(null);
  const [adding, setAdding] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftType, setDraftType] = useState("服务合同");
  const [draftFocus, setDraftFocus] = useState("违约责任、争议解决");
  const [draftMaterialId, setDraftMaterialId] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [dirty, setDirty] = useState(false);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [deliveries, setDeliveries] = useState<DeliveryVersion[]>([]);
  const [exportIssues, setExportIssues] = useState<{ issues: { code: string; message: string; handle?: string | null }[]; format: "markdown" | "docx" } | null>(null);

  useEffect(() => {
    if (!caseId) return;
    let current = true;
    api.listReviews(caseId).then((r) => { if (current) setReviews(r.items); });
    api.listMaterials(caseId).then((r) => { if (current) setMaterials(r.items); });
    api.listDeliveries(caseId)
      .then((result) => { if (current) setDeliveries(result.items.filter((d) => d.kind === "contract_review")); })
      .catch(() => { if (current) setDeliveries([]); });
    return () => { current = false; };
  }, [caseId, refreshToken]);

  useEffect(() => {
    setDetail(null);
    setDraftBody("");
    setDraftTitle("");
    setDirty(false);
    setError("");
    setNotice("");
    setExportIssues(null);
  }, [caseId, refreshToken]);

  useEffect(() => {
    if (!detail) return;
    setDraftBody(detail.current_version?.body || "");
    setDirty(false);
  }, [detail?.review.id]);

  async function handleSelectReview(reviewId: string) {
    setNotice("");
    setError("");
    try {
      setDetail(await api.getReview(reviewId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载合同审查失败");
    }
  }

  async function handleCreate() {
    if (archived || !draftMaterialId.trim()) return;
    setAction("create");
    setError("");
    setAdding(true);
    try {
      const created = await api.createReview(
        caseId,
        draftMaterialId,
        draftTitle.trim() || "合同审查",
        draftType.trim(),
        draftFocus.trim(),
      );
      setReviews((prev) => [created.review, ...prev]);
      setDetail(created);
      setDraftTitle("");
      setNotice("已创建合同审查，点击「生成合同审查」让模型检索法条并生成风险清单");
    } catch (cause) {
      setError(cause instanceof ApiRequestError ? cause.message : "创建失败");
    } finally {
      setAction(null);
      setAdding(false);
    }
  }

  async function handleGenerate() {
    if (!detail || archived) return;
    setAction("generate");
    setError("");
    try {
      const task = await api.generateReview(
        detail.review.id,
        `review-${detail.review.id}`,
      );
      await new Promise<void>((resolve) => {
        api.streamTask(
          task.id,
          (current) => {
            if (current.status === "failed") {
              setError(`合同审查失败：${current.progress_detail || "未知错误"}`);
            }
          },
          () => resolve(),
        );
      });
      setDetail(await api.getReview(detail.review.id));
      setNotice("合同审查已生成 — 请复核风险清单与法条引用，确认后形成不可变版本");
    } catch (cause) {
      setError(cause instanceof ApiRequestError ? cause.message : "生成失败");
    } finally {
      setAction(null);
    }
  }

  async function handleSaveVersion() {
    if (!detail || archived) return;
    setAction("save");
    setError("");
    try {
      const updated = await api.saveReviewVersion(
        detail.review.id,
        draftBody,
        detail.current_version?.id,
      );
      setDetail(updated);
      setDirty(false);
      setNotice(`已保存为新版本 v${updated.review.current_version_number}`);
    } catch (cause) {
      setError(cause instanceof ApiRequestError ? cause.message : "保存失败");
    } finally {
      setAction(null);
    }
  }

  async function handleConfirm() {
    if (!detail || archived) return;
    setAction("confirm");
    setError("");
    try {
      const updated = await api.confirmReview(detail.review.id);
      setDetail(updated);
      setNotice("合同审查已确认 — 法条引用完整，可导出交付版本");
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.issues && cause.issues.length > 0) {
        const codes = [...new Set(cause.issues.map((i) => i.code))].join("、");
        setError(`引用校验未通过：${codes}。请补齐法条 [[handle]] 或重新生成后再确认。`);
      } else {
        setError(cause instanceof Error ? cause.message : "确认失败");
      }
    } finally {
      setAction(null);
    }
  }

  async function handleExport(format: "markdown" | "docx", keepIssues: boolean) {
    if (!detail || archived) return;
    setAction("export");
    setError("");
    try {
      const delivery = await api.createReviewExport(detail.review.id, format, keepIssues);
      setExportIssues(null);
      setDeliveries((current) => [delivery, ...current]);
      setNotice(
        delivery.status === "official"
          ? `已导出正式交付版本（${delivery.format === "markdown" ? "Markdown" : "DOCX"}）`
          : `已导出草稿交付版本（${delivery.format === "markdown" ? "Markdown" : "DOCX"}）· ${delivery.issues.length} 个未核实问题`
      );
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.issues && cause.issues.length > 0) {
        setExportIssues({ issues: cause.issues, format });
      } else {
        setError(cause instanceof Error ? cause.message : "导出失败");
      }
    } finally {
      setAction(null);
    }
  }

  const current = detail?.current_version;
  const risksByLevel = useMemo(() => {
    const grouped: Record<RiskLevel, RiskItem[]> = { "高危": [], "中等": [], "提示": [] };
    for (const risk of current?.risks || []) grouped[risk.level].push(risk);
    return grouped;
  }, [current]);

  return (
    <section className="panel contract-review-panel" aria-label="合同审查">
      <div className="panel-side">
        <div className="panel-side-heading">
          <h3>合同审查</h3>
          <span>{reviews.length} 个审查 · 法条检索 + LLM 风险清单</span>
        </div>
        {adding ? (
          <div className="empty-state compact">创建中…</div>
        ) : (
          <>
            <div className="contract-create-form">
              <input
                type="text"
                placeholder="标题（默认：合同审查）"
                value={draftTitle}
                disabled={archived}
                onChange={(e) => setDraftTitle(e.target.value)}
              />
              <input
                type="text"
                placeholder="合同类型（如：服务合同 / 买卖合同）"
                value={draftType}
                disabled={archived}
                onChange={(e) => setDraftType(e.target.value)}
              />
              <textarea
                placeholder="重点关注条款（如：违约责任、争议解决）"
                value={draftFocus}
                disabled={archived}
                onChange={(e) => setDraftFocus(e.target.value)}
                rows={2}
              />
              <select
                value={draftMaterialId}
                disabled={archived}
                onChange={(e) => setDraftMaterialId(e.target.value)}
              >
                <option value="">选择材料…</option>
                {materials.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.original_name}
                  </option>
                ))}
              </select>
              <button
                className="button primary"
                type="button"
                disabled={archived || !draftMaterialId.trim() || action !== null}
                onClick={() => void handleCreate()}
              >
                {action === "create" ? "创建中…" : "创建合同审查"}
              </button>
            </div>
            <div className="panel-list">
              {reviews.map((review) => {
                const isSelected = detail?.review.id === review.id;
                return (
                  <button
                    key={review.id}
                    type="button"
                    className={`panel-list-item ${isSelected ? "selected" : ""}`}
                    onClick={() => void handleSelectReview(review.id)}
                  >
                    <div className="panel-list-item-title">{review.title}</div>
                    <div className="panel-list-item-meta">
                      v{review.current_version_number ?? "—"} ·{" "}
                      {review.current_sources.length || 0} 法条来源 · {formatDate(review.updated_at)}
                    </div>
                  </button>
                );
              })}
              {reviews.length === 0 && (
                <div className="empty-state compact">暂无合同审查；创建一个开始审查吧。</div>
              )}
            </div>
          </>
        )}
      </div>
      <div className="panel-body">
        {!detail ? (
          <div className="empty-state">选择左侧合同审查，或先创建一个。</div>
        ) : !current ? (
          <div className="panel-group">
            <h4>尚无版本 — 点击「生成合同审查」开始</h4>
            <button
              className="button primary"
              type="button"
              disabled={archived || action !== null}
              onClick={() => void handleGenerate()}
            >
              {action === "generate" ? "生成中…" : "生成合同审查（法条检索 + LLM）"}
            </button>
          </div>
        ) : (
          <>
            <header className="panel-section-heading">
              <h4>{detail.review.title}</h4>
              <span>
                合同类型：{detail.review.contract_type || "（未指定）"} · 重点：
                {detail.review.focus_points || "（未指定）"}
              </span>
            </header>
            {notice && <div className="notice success">{notice}</div>}
            {error && <div className="notice error" role="alert">{error}</div>}
            {exportIssues && (
              <div className="summary-issues delivery-issues" role="alert">
                <strong>导出前校验发现问题（{exportIssues.format === "markdown" ? "Markdown" : "DOCX"}）</strong>
                {exportIssues.issues.map((issue, index) => (
                  <p key={`${issue.code}-${issue.handle ?? index}`}>
                    <code>{issue.code}</code>：{issue.message}
                  </p>
                ))}
                <div className="delivery-issue-actions">
                  <span>可以选择忽略问题，导出草稿交付版本</span>
                  <div>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={action !== null}
                      onClick={() => { setExportIssues(null); void handleExport(exportIssues.format, false); }}
                    >
                      取消
                    </button>
                    <button
                      className="button"
                      type="button"
                      disabled={action !== null}
                      onClick={() => void handleExport(exportIssues.format, true)}
                    >
                      强制导出为草稿
                    </button>
                  </div>
                </div>
              </div>
            )}
            {RISK_LEVEL_ORDER.some((l) => risksByLevel[l].length > 0) ? (
              RISK_LEVEL_ORDER
                .filter((l) => risksByLevel[l].length > 0)
                .map((level) => <RiskSection key={level} level={level} risks={risksByLevel[level]} />)
            ) : (
              <div className="panel-group">
                <h4>结构化风险清单为空 — 可能尚未生成或模型返回格式不标准</h4>
              </div>
            )}
            <section className="review-editor">
              <h4>审查正文（可手工编辑）</h4>
              <textarea
                className="review-body"
                value={draftBody}
                disabled={archived}
                onChange={(e) => {
                  setDraftBody(e.target.value);
                  setDirty(true);
                }}
                rows={14}
              />
              <div className="review-actions">
                <span>
                  {dirty ? "有未保存修改" : "正文已是当前版本"} ·
                  法条引用沿用 [[handle]] 形式
                </span>
                <div>
                  <button
                    className="button"
                    type="button"
                    disabled={archived || action !== null}
                    onClick={() => void handleGenerate()}
                  >
                    {action === "generate" ? "生成中…" : "重新生成"}
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    disabled={archived || !dirty || action !== null}
                    onClick={() => void handleSaveVersion()}
                  >
                    {action === "save" ? "保存中…" : "保存为新版本"}
                  </button>
                  <button
                    className="button primary"
                    type="button"
                    disabled={archived || action !== null || current.status === "confirmed"}
                    onClick={() => void handleConfirm()}
                  >
                    {action === "confirm" ? "确认中…" : "确认版本"}
                  </button>
                  <button
                    className="button"
                    type="button"
                    disabled={archived || action !== null || current.status !== "confirmed"}
                    onClick={() => void handleExport("markdown", false)}
                  >
                    {action === "export" ? "导出中…" : "导出 Markdown"}
                  </button>
                  <button
                    className="button"
                    type="button"
                    disabled={archived || action !== null || current.status !== "confirmed"}
                    onClick={() => void handleExport("docx", false)}
                  >
                    导出 DOCX
                  </button>
                </div>
              </div>
            </section>
            {detail.sources.length > 0 && (
              <section className="review-sources">
                <h4>本次法条来源（{detail.sources.length}）</h4>
                <div className="review-source-list">
                  {detail.sources.map((source) => (
                    <article key={source.handle} className="review-source-item">
                      <span className={`source-handle ${RISK_LEVEL_CLASS["提示"]}`}>
                        [[{source.handle}]]
                      </span>
                      <strong>《{source.law_name || source.title}》</strong>
                      {source.article != null ? <span className="source-article">第 {source.article} 条</span> : null}
                      <p>{(source.text || "").slice(0, 200)}</p>
                    </article>
                  ))}
                </div>
              </section>
            )}
            <section className="delivery-list" aria-labelledby="review-delivery-list-title">
              <div className="result-section-heading">
                <h4 id="review-delivery-list-title">合同审查交付版本</h4>
                <span>{deliveries.length} 个交付文件 · 正式交付不可变</span>
              </div>
              {deliveries.length === 0 ? (
                <div className="empty-state compact">尚无导出；确认当前版本后可导出 Markdown 或 DOCX</div>
              ) : (
                <div className="delivery-items">
                  {deliveries.map((item) => (
                    <article className={`delivery-item ${item.status}`} key={item.id}>
                      <div className="delivery-item-heading">
                        <span className={`delivery-format ${item.format}`}>{item.format === "markdown" ? "MD" : "DOCX"}</span>
                        <strong>{item.summary_title}</strong>
                        {item.status === "official" ? (
                          <span className="delivery-status official">正式交付</span>
                        ) : (
                          <span className="delivery-status draft">草稿 · {item.issues.length} 个未核实</span>
                        )}
                      </div>
                      <p>
                        v{item.version_number} · {new Date(item.exported_at).toLocaleString("zh-CN")} ·{" "}
                        {(item.size_bytes / 1024).toFixed(1)} KB
                      </p>
                      <a
                        className="button secondary"
                        href={`/api/deliveries/${item.id}/download`}
                        download
                      >
                        下载 {item.format === "markdown" ? "Markdown" : "DOCX"}
                      </a>
                    </article>
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </section>
  );
}

function RiskSection({ level, risks }: { level: RiskLevel; risks: RiskItem[] }) {
  return (
    <section className={`risk-section ${RISK_LEVEL_CLASS[level]}`}>
      <h4>
        <span className={`risk-level-badge ${RISK_LEVEL_CLASS[level]}`}>{level}风险</span>
        <span className="risk-count">{risks.length}</span>
      </h4>
      <div className="risk-cards">
        {risks.map((risk, idx) => (
          <article key={idx} className={`risk-card ${RISK_LEVEL_CLASS[level]}`}>
            <div className="risk-excerpt">条款：{risk.excerpt || "（未标注）"}</div>
            {risk.risk_issue && <div className="risk-issue">风险说明：{risk.risk_issue}</div>}
            {risk.suggestion && (
              <div className="risk-suggestion">
                <strong>建议：</strong>{risk.suggestion}
              </div>
            )}
            {risk.legal_basis && <div className="risk-basis">法律依据：{risk.legal_basis}</div>}
          </article>
        ))}
      </div>
    </section>
  );
}

function formatDate(iso: string) {
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}
