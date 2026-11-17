import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { ApiRequestError, api, createRequestKey } from "./api";
import type {
  Capabilities,
  Material,
  MaterialDetail,
  MaterialSegment,
  MaterialStatus,
  SourceReference,
  TaskItem,
} from "./types";

const statusLabels: Record<MaterialStatus, string> = {
  queued: "等待解析",
  running: "解析中",
  success: "解析完成",
  partial: "部分完成",
  failed: "解析失败",
  cancelled: "已取消",
  recovery_pending: "状态待恢复",
};

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function locatorText(locator: Record<string, unknown>) {
  if (typeof locator.page === "number") return `第 ${locator.page} 页`;
  if (typeof locator.paragraph === "number") return `第 ${locator.paragraph} 段`;
  if (typeof locator.table === "number" && typeof locator.row === "number") {
    return `表 ${locator.table} · 第 ${locator.row} 行`;
  }
  if (typeof locator.line_start === "number") {
    const end = typeof locator.line_end === "number" ? locator.line_end : locator.line_start;
    const range = end === locator.line_start ? `第 ${end} 行` : `第 ${locator.line_start}–${end} 行`;
    if (typeof locator.char_start === "number" && typeof locator.char_end === "number") {
      return `${range} · 字符 ${locator.char_start}–${locator.char_end}`;
    }
    return range;
  }
  return Object.entries(locator).map(([key, value]) => `${key}: ${String(value)}`).join(" · ") || "无定位信息";
}

function coverageWarnings(coverage: Record<string, unknown>) {
  const warnings: string[] = [];
  if (coverage.text_truncated === true) warnings.push("文本达到解析上限，超出部分已截断");
  if (Array.isArray(coverage.failed_pages) && coverage.failed_pages.length > 0) {
    warnings.push(`失败页：${coverage.failed_pages.join("、")}`);
  }
  if (Array.isArray(coverage.ocr_uncovered) && coverage.ocr_uncovered.length > 0) {
    warnings.push(`有 ${coverage.ocr_uncovered.length} 页未获得 OCR 文本，请查看逐段原因`);
  }
  return warnings;
}

interface MaterialsPanelProps {
  caseId: string;
  archived: boolean;
  retryTask: TaskItem | null;
  onRetryHandled: () => void;
  addingResearch: boolean;
  onAddResearchSource: (source: SourceReference) => void;
}

export default function MaterialsPanel({ caseId, archived, retryTask, onRetryHandled, addingResearch, onAddResearchSource }: MaterialsPanelProps) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [capabilityError, setCapabilityError] = useState("");
  const [materials, setMaterials] = useState<Material[]>([]);
  const [selectedMaterialId, setSelectedMaterialId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MaterialDetail | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [segments, setSegments] = useState<MaterialSegment[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploadKey, setUploadKey] = useState("");
  const [duplicates, setDuplicates] = useState<Material[]>([]);
  const [loading, setLoading] = useState(true);
  const [segmentsLoading, setSegmentsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [parseRetry, setParseRetry] = useState<TaskItem | null>(null);
  const [retrying, setRetrying] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const detailRequestRef = useRef(0);
  const segmentRequestRef = useRef(0);

  const loadMaterials = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const result = await api.listMaterials(caseId);
      setMaterials(result.items);
      setError("");
      return result.items;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "材料列表加载失败");
      return [];
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [caseId]);

  const loadDetail = useCallback(async (materialId: string) => {
    const requestId = ++detailRequestRef.current;
    try {
      const result = await api.getMaterial(materialId);
      if (requestId === detailRequestRef.current) setDetail(result);
    } catch (cause) {
      if (requestId === detailRequestRef.current) {
        setError(cause instanceof Error ? cause.message : "材料详情加载失败");
      }
    }
  }, []);

  const loadSegments = useCallback(async (
    materialId: string,
    versionId: string,
    cursor = 0,
    append = false,
    showLoading = true,
  ) => {
    const requestId = ++segmentRequestRef.current;
    if (showLoading) setSegmentsLoading(true);
    try {
      const result = await api.listMaterialSegments(materialId, versionId, cursor);
      if (requestId !== segmentRequestRef.current) return;
      setSegments((current) => append ? [...current, ...result.items] : result.items);
      setNextCursor(result.next_cursor);
    } catch (cause) {
      if (requestId === segmentRequestRef.current) {
        setError(cause instanceof Error ? cause.message : "解析段落加载失败");
      }
    } finally {
      if (showLoading && requestId === segmentRequestRef.current) setSegmentsLoading(false);
    }
  }, []);

  useEffect(() => {
    api.capabilities()
      .then(setCapabilities)
      .catch((cause) => setCapabilityError(cause instanceof Error ? cause.message : "能力信息暂不可用"));
  }, []);

  useEffect(() => {
    setSelectedMaterialId(null);
    setDetail(null);
    setSelectedVersionId(null);
    setSegments([]);
    setFile(null);
    setDuplicates([]);
    setParseRetry(null);
    void loadMaterials(true);
  }, [caseId, loadMaterials]);

  useEffect(() => {
    if (!selectedMaterialId) {
      setDetail(null);
      return;
    }
    void loadDetail(selectedMaterialId);
  }, [selectedMaterialId, loadDetail]);

  useEffect(() => {
    if (!retryTask || retryTask.task_type !== "parse" || retryTask.case_id !== caseId) return;
    const materialId = retryTask.input_snapshot.material_id;
    if (typeof materialId === "string") setSelectedMaterialId(materialId);
    setParseRetry(retryTask);
    onRetryHandled();
  }, [retryTask, caseId, onRetryHandled]);

  const selectedVersion = useMemo(() => {
    if (!detail) return null;
    return detail.versions.find((version) => version.id === selectedVersionId) || null;
  }, [detail, selectedVersionId]);

  useEffect(() => {
    if (!detail) {
      setSelectedVersionId(null);
      return;
    }
    if (!detail.versions.some((version) => version.id === selectedVersionId)) {
      setSelectedVersionId(detail.material.current_version_id || detail.versions[0]?.id || null);
    }
  }, [detail, selectedVersionId]);

  useEffect(() => {
    setSegments([]);
    setNextCursor(null);
    if (selectedMaterialId && selectedVersionId) {
      void loadSegments(selectedMaterialId, selectedVersionId);
    } else {
      segmentRequestRef.current += 1;
    }
  }, [selectedMaterialId, selectedVersionId, loadSegments]);

  const hasActiveMaterials = materials.some((material) => material.status === "queued" || material.status === "running");
  useEffect(() => {
    if (!hasActiveMaterials && selectedVersion?.status !== "running") return;
    const timer = window.setInterval(() => {
      void loadMaterials(false);
      if (selectedMaterialId) void loadDetail(selectedMaterialId);
      if (selectedMaterialId && selectedVersionId) {
        void loadSegments(selectedMaterialId, selectedVersionId, 0, false, false);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hasActiveMaterials, selectedVersion?.status, selectedMaterialId, selectedVersionId, loadMaterials, loadDetail, loadSegments]);

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] || null;
    setFile(selected);
    setUploadKey(selected ? createRequestKey() : "");
    setDuplicates([]);
    setError("");
    setNotice("");
  }

  async function performUpload(confirmDuplicate: boolean) {
    if (!file || !uploadKey || archived) return;
    setUploading(true);
    setError("");
    try {
      const result = await api.uploadMaterial(caseId, file, confirmDuplicate, uploadKey);
      setNotice(`材料“${result.material.original_name}”已保存，解析任务已创建`);
      setDuplicates([]);
      setFile(null);
      setUploadKey("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      setSelectedMaterialId(result.material.id);
      await loadMaterials(false);
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.code === "duplicate_material") {
        setDuplicates(cause.duplicates);
        setError(cause.message);
      } else {
        setDuplicates([]);
        setError(cause instanceof Error ? cause.message : "材料上传失败");
      }
    } finally {
      setUploading(false);
    }
  }

  function handleUpload(event: FormEvent) {
    event.preventDefault();
    void performUpload(false);
  }

  async function handleRetry() {
    if (!parseRetry || archived) return;
    setRetrying(true);
    try {
      await api.retryTask(parseRetry.id);
      setNotice("已创建新的解析执行，旧版本与段落仍保留");
      setParseRetry(null);
      setError("");
      await loadMaterials(false);
      if (selectedMaterialId) await loadDetail(selectedMaterialId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "解析重试失败");
    } finally {
      setRetrying(false);
    }
  }

  return (
    <section className="materials-panel" aria-labelledby="materials-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">CASE MATERIALS</span>
          <h3 id="materials-title">案件材料</h3>
          <p>原件不可变保存；每次解析生成独立版本，部分文本和失败原因均可追溯。</p>
        </div>
        <span className="count-badge">{materials.length}</span>
      </div>

      {(error || notice) && (
        <div className={`message ${error ? "error" : "success"}`} role={error ? "alert" : "status"}>
          <span>{error || notice}</span>
          <button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="关闭材料提示">×</button>
        </div>
      )}

      {!archived && (
        <form className="upload-panel" onSubmit={handleUpload}>
          <div className="upload-copy">
            <strong>上传新材料</strong>
            <span>
              {capabilities
                ? `支持 ${capabilities.parser.formats.join(" / ").toUpperCase()} · 单文件 ${formatSize(capabilities.parser.limits.max_file_bytes)} · 单案件 ${capabilities.parser.limits.max_files_per_case} 份 · OCR ${capabilities.parser.ocr_available ? "可用" : "不可用"}`
                : capabilityError || "正在读取解析能力与上限…"}
            </span>
            {capabilities && <span>单文件最多 {capabilities.parser.limits.max_ocr_pages} 个 OCR 页、{capabilities.parser.limits.max_text_chars.toLocaleString()} 个解析字符</span>}
          </div>
          <div className="upload-controls">
            <input ref={fileInputRef} type="file" accept=".pdf,.docx,.txt,.md" onChange={handleFileChange} disabled={uploading} aria-label="选择案件材料" />
            <button className="button primary" type="submit" disabled={!file || uploading}>
              {uploading ? "正在上传…" : "上传并解析"}
            </button>
          </div>
          {file && <p className="field-help">待上传：{file.name} · {formatSize(file.size)}</p>}
          {duplicates.length > 0 && (
            <div className="duplicate-warning">
              <strong>发现相同内容的材料</strong>
              <span>{duplicates.map((item) => item.original_name).join("、")}</span>
              <p>系统不会自动合并。确认后仍将创建独立材料、原件和解析任务。</p>
              <button className="button secondary" type="button" disabled={uploading} onClick={() => void performUpload(true)}>
                确认作为独立材料上传
              </button>
            </div>
          )}
        </form>
      )}

      <div className="materials-layout">
        <div className="material-list" aria-label="材料列表">
          {loading ? <div className="empty-state compact">正在加载材料…</div> : materials.length === 0 ? (
            <div className="empty-state compact"><strong>暂无材料</strong><span>上传 PDF、DOCX、TXT 或 Markdown 文件开始解析。</span></div>
          ) : materials.map((material) => (
            <button
              type="button"
              className={`material-list-item ${selectedMaterialId === material.id ? "selected" : ""}`}
              key={material.id}
              onClick={() => setSelectedMaterialId(material.id)}
            >
              <span className="material-format">{material.format.toUpperCase()}</span>
              <span className="material-list-copy"><strong>{material.original_name}</strong><small>{formatSize(material.size_bytes)} · {formatDate(material.created_at)}</small></span>
              <span className={`task-status ${material.status === "success" ? "completed" : material.status}`}>{statusLabels[material.status]}</span>
            </button>
          ))}
        </div>

        <div className="material-detail">
          {!detail ? (
            <div className="empty-state compact"><strong>选择材料查看解析结果</strong><span>可核对原件、解析版本、真实页码或段落行号。</span></div>
          ) : (
            <>
              <div className="material-detail-heading">
                <div><h4>{detail.material.original_name}</h4><p>{formatSize(detail.material.size_bytes)} · SHA-256 {detail.material.sha256}</p></div>
                <a className="button secondary" href={`/api/materials/${detail.material.id}/original`} download>下载原件</a>
              </div>
              {detail.versions.length === 0 ? <div className="empty-state compact">解析版本尚未创建。</div> : (
                <>
                  <div className="version-toolbar">
                    <label>解析版本
                      <select value={selectedVersionId || ""} onChange={(event) => setSelectedVersionId(event.target.value)}>
                        {detail.versions.map((version, index) => <option value={version.id} key={version.id}>版本 {detail.versions.length - index} · {statusLabels[version.status]} · {formatDate(version.created_at)}</option>)}
                      </select>
                    </label>
                    {selectedVersion && <span>{selectedVersion.parser_version || "解析器待上报"} · {selectedVersion.segment_count} 段 · {selectedVersion.char_count.toLocaleString()} 字符</span>}
                  </div>
                  {selectedVersion && (
                    <div className="parse-version-summary">
                      <span>定位：{selectedVersion.locator_type || "待上报"}</span>
                      <span>页数：{selectedVersion.page_count ?? "—"}</span>
                      <span>Attempt：{selectedVersion.attempt_id}</span>
                    </div>
                  )}
                  {selectedVersion?.error && <p className="task-error">{selectedVersion.error.code ? `${selectedVersion.error.code} · ` : ""}{selectedVersion.error.message || "解析失败"}</p>}
                  {selectedVersion && coverageWarnings(selectedVersion.coverage).map((warning) => <p className="coverage-warning" key={warning}>{warning}</p>)}
                  {selectedVersion && Object.keys(selectedVersion.coverage).length > 0 && (
                    <details className="coverage-details"><summary>完整覆盖信息</summary><pre>{JSON.stringify(selectedVersion.coverage, null, 2)}</pre></details>
                  )}
                  <div className="segment-list">
                    {segmentsLoading && segments.length === 0 ? <div className="empty-state compact">正在读取解析段落…</div> : segments.length === 0 ? (
                      <div className="empty-state compact">当前版本暂无可显示段落。</div>
                    ) : segments.map((segment) => (
                      <article className={`segment-card ${segment.status !== "ok" ? "segment-warning" : ""}`} key={segment.id}>
                        <div className="segment-meta"><strong>{locatorText(segment.locator)}</strong><span>#{segment.sequence} · {segment.extraction_method || segment.status}</span></div>
                        {segment.text ? <p>{segment.text}</p> : <p className="segment-empty">未提取到文本</p>}
                        {segment.error && <div className="segment-error">{segment.error.code ? `${segment.error.code} · ` : ""}{segment.error.message || "该段解析失败"}</div>}
                        {!archived && segment.text.trim() && ["ok", "partial"].includes(segment.status) && ["success", "partial"].includes(selectedVersion?.status || "") && (
                          <div className="segment-actions">
                            <button className="button secondary" type="button" disabled={addingResearch} onClick={() => onAddResearchSource({ kind: "material_segment", id: segment.id })}>
                              {addingResearch ? "正在加入…" : "加入研究"}
                            </button>
                          </div>
                        )}
                      </article>
                    ))}
                    {nextCursor !== null && selectedMaterialId && selectedVersionId && (
                      <button className="button secondary full-width" type="button" disabled={segmentsLoading} onClick={() => void loadSegments(selectedMaterialId, selectedVersionId, nextCursor, true)}>
                        {segmentsLoading ? "正在加载…" : "加载更多段落"}
                      </button>
                    )}
                  </div>
                </>
              )}
            </>
          )}
        </div>
      </div>

      {parseRetry && !archived && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !retrying) setParseRetry(null);
        }}>
          <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="retry-parse-title">
            <div className="dialog-heading"><div><span className="eyebrow">NEW PARSE VERSION</span><h2 id="retry-parse-title">重新解析材料</h2></div><button className="icon-button" type="button" onClick={() => setParseRetry(null)} disabled={retrying} aria-label="关闭重试">×</button></div>
            <p className="retry-description">将对同一不可变原件创建新解析版本。已有版本、已解析段落和错误记录不会被覆盖。</p>
            <p className="retry-material-name">{materials.find((item) => item.id === parseRetry.input_snapshot.material_id)?.original_name || detail?.material.original_name || "案件材料"}</p>
            <div className="dialog-actions"><button className="button secondary" type="button" onClick={() => setParseRetry(null)} disabled={retrying}>取消</button><button className="button primary" type="button" onClick={() => void handleRetry()} disabled={retrying}>{retrying ? "正在创建…" : "创建新解析版本"}</button></div>
          </section>
        </div>
      )}
    </section>
  );
}
