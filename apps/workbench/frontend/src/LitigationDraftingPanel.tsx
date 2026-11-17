import { useEffect, useState } from "react";
import type {
  DeliveryVersion,
  LitigationDocType,
  LitigationDocument,
  LitigationDocumentDetail,
} from "./types";
import { api, ApiRequestError } from "./api";

const DOC_TYPE_LABELS: Record<LitigationDocType, string> = {
  complaint: "民事起诉状",
  defense: "民事答辩状",
  representation: "代理词",
  legal_opinion: "法律意见书",
};

interface Props {
  caseId: string;
  archived: boolean;
  refreshToken: number;
}

export default function LitigationDraftingPanel({ caseId, archived, refreshToken }: Props) {
  const [docs, setDocs] = useState<LitigationDocument[]>([]);
  const [detail, setDetail] = useState<LitigationDocumentDetail | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<null | "create" | "generate" | "save" | "confirm" | "export">(null);
  const [draftBody, setDraftBody] = useState("");
  const [dirty, setDirty] = useState(false);
  const [deliveries, setDeliveries] = useState<DeliveryVersion[]>([]);
  const [exportIssues, setExportIssues] = useState<{ issues: { code: string; message: string; handle?: string | null }[]; format: "markdown" | "docx" } | null>(null);

  // 创建表单
  const [draftType, setDraftType] = useState<LitigationDocType>("complaint");
  const [draftTitle, setDraftTitle] = useState("");
  const [draftParties, setDraftParties] = useState("");
  const [draftClaims, setDraftClaims] = useState("");
  const [draftFacts, setDraftFacts] = useState("");
  const [draftFocus, setDraftFocus] = useState("");

  useEffect(() => {
    if (!caseId) return;
    let current = true;
    api.listLitigationDocs(caseId).then((r) => { if (current) setDocs(r.items); });
    api.listDeliveries(caseId)
      .then((result) => { if (current) setDeliveries(result.items.filter((d) => d.kind === "litigation_document")); })
      .catch(() => { if (current) setDeliveries([]); });
    return () => { current = false; };
  }, [caseId, refreshToken]);

  useEffect(() => {
    setDetail(null);
    setDraftBody("");
    setDirty(false);
    setError("");
    setNotice("");
    setExportIssues(null);
  }, [caseId, refreshToken]);

  useEffect(() => {
    if (!detail) return;
    setDraftBody(detail.current_version?.body || "");
    setDirty(false);
  }, [detail?.document.id]);

  async function handleSelectDoc(docId: string) {
    setNotice("");
    setError("");
    try {
      setDetail(await api.getLitigationDoc(docId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载诉讼文书失败");
    }
  }

  async function handleCreate() {
    if (archived || !draftFacts.trim()) return;
    setAction("create");
    setError("");
    try {
      const created = await api.createLitigationDoc(caseId, {
        doc_type: draftType,
        title: draftTitle.trim() || DOC_TYPE_LABELS[draftType],
        parties: draftParties.trim(),
        claims: draftClaims.trim(),
        facts: draftFacts.trim(),
        focus_points: draftFocus.trim(),
      });
      setDocs((prev) => [created.document, ...prev]);
      setDetail(created);
      setDraftTitle("");
      setDraftParties("");
      setDraftClaims("");
      setDraftFacts("");
      setDraftFocus("");
      setNotice("已创建诉讼文书，点击「生成文书」让模型检索法条并起草");
    } catch (cause) {
      setError(cause instanceof ApiRequestError ? cause.message : "创建失败");
    } finally {
      setAction(null);
    }
  }

  async function handleGenerate() {
    if (!detail || archived) return;
    setAction("generate");
    setError("");
    try {
      const task = await api.generateLitigation(detail.document.id, `litigation-${detail.document.id}`);
      for (let i = 0; i < 100; i++) {
        const current = await api.getTask(task.id);
        if (current.status === "completed") break;
        if (current.status === "failed") {
          setError(`文书生成失败：${current.progress_detail || "未知错误"}`);
          setAction(null);
          return;
        }
        await new Promise((r) => setTimeout(r, 300));
      }
      setDetail(await api.getLitigationDoc(detail.document.id));
      setNotice("诉讼文书已生成 — 请复核法条引用与文书结构，确认后形成不可变版本");
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
      const updated = await api.saveLitigationVersion(
        detail.document.id,
        draftBody,
        detail.current_version?.id,
      );
      setDetail(updated);
      setDirty(false);
      setNotice(`已保存为新版本 v${updated.document.current_version_number}`);
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
      const updated = await api.confirmLitigation(detail.document.id);
      setDetail(updated);
      setNotice("诉讼文书已确认 — 法条引用完整，可导出交付版本");
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
      const delivery = await api.createLitigationExport(detail.document.id, format, keepIssues);
      setExportIssues(null);
      setDeliveries((current) => [delivery, ...current]);
      setNotice(
        delivery.status === "official"
          ? `已导出正式交付版本（${format === "markdown" ? "Markdown" : "DOCX"}）`
          : `已导出草稿交付版本（${format === "markdown" ? "Markdown" : "DOCX"}）· ${delivery.issues.length} 个未核实问题`
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

  return (
    <section className="panel litigation-panel" aria-label="诉讼文书起草">
      <div className="panel-side">
        <div className="panel-side-heading">
          <h3>诉讼文书起草</h3>
          <span>{docs.length} 份文书 · 法条检索 + LLM 起草</span>
        </div>
        <div className="contract-create-form">
          <select
            value={draftType}
            disabled={archived}
            onChange={(e) => setDraftType(e.target.value as LitigationDocType)}
          >
            {Object.entries(DOC_TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
          <input
            type="text"
            placeholder="标题（默认：文书类型名）"
            value={draftTitle}
            disabled={archived}
            onChange={(e) => setDraftTitle(e.target.value)}
          />
          <textarea
            placeholder="当事人信息（原告/被告姓名、住址等）"
            value={draftParties}
            disabled={archived}
            onChange={(e) => setDraftParties(e.target.value)}
            rows={2}
          />
          <textarea
            placeholder="诉讼请求（逐条列明）"
            value={draftClaims}
            disabled={archived}
            onChange={(e) => setDraftClaims(e.target.value)}
            rows={2}
          />
          <textarea
            placeholder="案件事实（按时间顺序叙述）*"
            value={draftFacts}
            disabled={archived}
            onChange={(e) => setDraftFacts(e.target.value)}
            rows={4}
          />
          <input
            type="text"
            placeholder="重点关注（可选）"
            value={draftFocus}
            disabled={archived}
            onChange={(e) => setDraftFocus(e.target.value)}
          />
          <button
            className="button primary"
            type="button"
            disabled={archived || !draftFacts.trim() || action !== null}
            onClick={() => void handleCreate()}
          >
            {action === "create" ? "创建中…" : "创建诉讼文书"}
          </button>
        </div>
        <div className="panel-list">
          {docs.map((doc) => {
            const isSelected = detail?.document.id === doc.id;
            return (
              <button
                key={doc.id}
                type="button"
                className={`panel-list-item ${isSelected ? "selected" : ""}`}
                onClick={() => void handleSelectDoc(doc.id)}
              >
                <div className="panel-list-item-title">
                  {DOC_TYPE_LABELS[doc.doc_type] || doc.doc_type}：{doc.title}
                </div>
                <div className="panel-list-item-meta">
                  v{doc.current_version_number ?? "—"} ·{" "}
                  {doc.current_sources.length || 0} 法条来源 · {formatDate(doc.updated_at)}
                </div>
              </button>
            );
          })}
          {docs.length === 0 && (
            <div className="empty-state compact">暂无诉讼文书；创建一个开始起草吧。</div>
          )}
        </div>
      </div>
      <div className="panel-body">
        {!detail ? (
          <div className="empty-state">选择左侧诉讼文书，或先创建一个。</div>
        ) : !current ? (
          <div className="panel-group">
            <h4>尚无版本 — 点击「生成文书」开始</h4>
            <p className="panel-hint">
              文书类型：{DOC_TYPE_LABELS[detail.document.doc_type]} ·
              当事人：{detail.document.parties || "（未提供）"} ·
              诉求：{detail.document.claims ? "已填写" : "（未提供）"}
            </p>
            <button
              className="button primary"
              type="button"
              disabled={archived || action !== null}
              onClick={() => void handleGenerate()}
            >
              {action === "generate" ? "生成中…" : "生成诉讼文书（法条检索 + LLM）"}
            </button>
          </div>
        ) : (
          <>
            <header className="panel-section-heading">
              <h4>{detail.document.title}</h4>
              <span>
                {DOC_TYPE_LABELS[detail.document.doc_type]} · 重点：
                {detail.document.focus_points || "（未指定）"}
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
            <section className="review-editor">
              <h4>文书正文（可手工编辑）</h4>
              <textarea
                className="review-body"
                value={draftBody}
                disabled={archived}
                onChange={(e) => {
                  setDraftBody(e.target.value);
                  setDirty(true);
                }}
                rows={18}
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
                      <span className="source-handle">
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
            <section className="delivery-list" aria-labelledby="litigation-delivery-list-title">
              <div className="result-section-heading">
                <h4 id="litigation-delivery-list-title">诉讼文书交付版本</h4>
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

function formatDate(iso: string) {
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}
