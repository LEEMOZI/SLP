# SLP 法律工作台（独立 Web 实例）

基于 FastAPI、React 与 SQLite 的独立工作台，不修改或接管既有 SLP MCP 服务。当前包含 FR-01 服务就绪检查、FR-02 案件管理、FR-03 材料上传与解析、FR-04 统一法规与类案检索、FR-05 统一任务中心、FR-06 证据查看与研究摘要、FR-07 保存导出与交付版本。

## 本地运行

材料解析和检索由独立的 Capability Adapter sidecar 提供。工作台只通过结构化 HTTP 契约调用它，不导入或修改生产 MCP 服务。先在 worktree 根目录启动 sidecar：

```bash
SLP_CAPABILITY_ADAPTER_TOKEN=local-internal-token \
.venv/bin/python -m uvicorn apps.capability_adapter.app:app --host 127.0.0.1 --port 8790
```

再使用相同内部 token 启动工作台：

```bash
SLP_CAPABILITY_ADAPTER_TOKEN=local-internal-token \
.venv/bin/python -m uvicorn apps.workbench.backend.app:app --host 127.0.0.1 --port 8780
```

前端位于 `apps/workbench/frontend`：

```bash
npm install
npm run build
```

构建后的页面由工作台提供。sidecar 地址默认为 `http://127.0.0.1:8790`，可通过 `SLP_CAPABILITY_ADAPTER_URL` 修改。默认数据库为 `~/.slp-workbench/dev/workbench.db`，可通过 `SLP_WORKBENCH_DATA_DIR` 修改；原件保存在该目录的 `files/cases/` 下。自定义数据目录时，还应将 sidecar 的 `SLP_WORKBENCH_MATERIAL_ROOT` 设置为同一目录下的 `files/cases`。裁判文书只读数据库目录默认为 `MCP/judgment-search/data/judgments`，可通过 `SLP_JUDGMENT_DB_DIR` 修改。

案件、任务、材料版本、证据和原文缓存共用工作台数据库，分别使用独立表结构；迁移版本记录在 `schema_migrations`。上传原件使用排他创建，重复内容不会被合并或覆盖。

## 材料与检索上限

P0 默认上限经隔离基准冻结，可用同名环境变量显式调整，但两个进程的材料大小和数量配置必须一致：

| 配置 | 默认值 |
|---|---|
| `SLP_CAPABILITY_MAX_FILE_BYTES` | 20 MiB |
| `SLP_CAPABILITY_MAX_FILES_PER_CASE` | 100 份 |
| `SLP_CAPABILITY_MAX_OCR_PAGES` | 单文件 20 页 |
| `SLP_CAPABILITY_MAX_TEXT_CHARS` | 单材料 500,000 字符 |

支持 PDF、DOCX、TXT、Markdown。PDF 保留真实页码，DOCX 保留段落或表格行，TXT/Markdown 保留行号和字符区间。加密、损坏、OCR 不可用、单页失败及文本截断会记录在版本覆盖信息或段落错误中；已有文本不会因此被删除。隔离基准中，20 页混合 PDF 的真实 OCR 曾观察到约 876 MiB RSS 增量。真实联调中，已预热 Chroma 模型的 sidecar 处理 1 页纯图片 PDF，RSS 从约 2,108 MiB 增至 2,769 MiB（增量约 660 MiB）。运行 OCR 时必须评估模型与 OCR 的组合峰值并限制并发。

## 材料与检索 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/capabilities` | 解析格式、OCR 状态、实际上限和来源状态 |
| POST | `/api/cases/{case_id}/materials` | multipart 上传 `file`；`confirm_duplicate=true` 确认独立上传；支持 `Idempotency-Key` |
| GET | `/api/cases/{case_id}/materials` | 当前案件材料列表 |
| GET | `/api/materials/{material_id}` | 材料及全部解析版本 |
| GET | `/api/materials/{material_id}/original` | 以附件形式下载不可变原件 |
| GET | `/api/materials/{material_id}/versions/{version_id}/segments` | 游标读取解析段及真实定位 |
| POST | `/api/cases/{case_id}/searches` | 创建自动、法规或类案检索；支持 `Idempotency-Key` |
| GET | `/api/cases/{case_id}/searches` | 当前案件检索历史 |
| GET | `/api/searches/{task_id}/attempts/{attempt_id}` | 指定执行的规划、来源覆盖和分组证据 |
| GET | `/api/evidence/{evidence_id}` | 证据详情；裁判文书首次读取后保存不可变原文缓存 |

相同内容的材料默认返回结构化 `duplicate_material` 409，仅提醒、不自动合并；用户确认后仍创建独立材料。上传和检索的幂等键若已用于不同内容，返回 `idempotency_conflict` 409。

检索模式为 `auto`、`law`、`cases`。自动模式使用规则路由；类案以裁判文书为主、指导案例为补充，业务参考只在明确指引或学习意图时触发。材料版本仅用于规则提取法名和争议词，不建立材料向量库，也不虚构法院、年份或案由过滤条件。响应保留实际提交的检索词、AND 到 OR 等降级记录、命中原因、部分匹配及来源覆盖。单源失败不影响其他来源证据；零命中成功、部分来源失败和全来源失败具有不同任务状态。

## 研究摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cases/{case_id}/summaries` | 创建空白摘要或带初始来源的摘要 |
| GET | `/api/cases/{case_id}/summaries` | 当前案件摘要列表 |
| GET | `/api/summaries/{summary_id}` | 当前来源、全部版本、引用问题和覆盖信息 |
| PUT | `/api/summaries/{summary_id}/sources` | 显式添加来源或按句柄移除来源 |
| POST | `/api/summaries/{summary_id}/versions` | 保存人工版本，可空正文保存为草稿 |
| POST | `/api/summaries/{summary_id}/generate` | 创建异步摘要任务；支持 `Idempotency-Key` |
| POST | `/api/summaries/{summary_id}/confirm` | 校验并确认当前版本 |

证据和材料段落可跨同一案件的不同 Search Attempt 显式组合，新 Attempt 不会自动加入既有摘要。来源按加入顺序获得稳定句柄 `S1`、`S2`，正文引用格式为 `[[S1]]`；跨案件、未知或失效来源会被拒绝。移除仍被历史版本引用的来源时，API 返回 `source_removal_requires_confirmation` 409 和受影响版本；确认移除后，历史版本仍保留不可变来源快照供核验，但引用已移除来源的当前版本不能确认。

版本状态为 `draft`、`edited`、`confirmed`。保存编辑和成功生成都会追加版本，不覆盖历史正文；生成失败、取消或重启后的未知结果不会追加版本。空白正文可以保存为草稿，但空正文、未知引用、跨案件引用、失效引用或未加入当前选择的引用会阻止确认。归档案件可读取摘要和原文，不能修改来源、保存、生成或确认。

默认生成器为 `builtin-extractive`，只摘录已冻结的真实来源，不调用 LM Studio 或其他模型，`actual_model` 为 `null`，结果明确标注“未使用模型，不构成法律结论”。真实 OpenAI-compatible 模型只能由 sidecar 通过独立环境配置显式启用：

```bash
SLP_SUMMARY_PROVIDER=openai-compatible \
SLP_SUMMARY_OPENAI_BASE_URL=https://model.example.com/v1 \
SLP_SUMMARY_OPENAI_MODEL=your-model \
SLP_SUMMARY_OPENAI_API_KEY=your-key \
SLP_SUMMARY_OPENAI_TIMEOUT_S=180 \
.venv/bin/python -m uvicorn apps.capability_adapter.app:app --host 127.0.0.1 --port 8790
```

三个 OpenAI 连接配置必须完整；请求失败、响应无效或引用伪造都会使任务失败，不会静默降级为规则摘录。生成结果记录请求生成器、实际生成器、实际模型、任务和 Attempt。

## 交付导出（FR-07）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/summaries/{summary_id}/exports` | 导出当前版本为 Markdown 或 DOCX；请求体 `{"format": "markdown"|"docx", "keep_issues": bool}` |
| GET | `/api/cases/{case_id}/deliveries` | 案件交付版本列表（归档案件可读） |
| GET | `/api/deliveries/{delivery_id}/download` | 下载交付文件，文件名含标题、版本、格式与状态 |

仅 `confirmed` 状态的当前版本可导出，否则返回 422。导出前合并两项校验：版本冻结引用校验（与确认一致）和事实可追溯性校验——正文中抽取的案号、日期、金额（正则与归一化复制自 `MCP/law-search/core/delivery_gate.py`）必须出现在正文实际引用来源的冻结文本中。校验发现问题默认阻断并返回 `summary_validation_failed` 与逐条位置（行号 + 摘录）；显式传 `keep_issues: true` 时导出为 `draft` 状态，文件与交付列表均醒目标记。无问题时导出为 `official`。

每次导出生成新记录和新文件（`storage_key = deliveries/{id}.{ext}`），记录版本号、sha256、大小、抽取事实、校验问题、生成器、实际模型与任务，导出后不可变：正文后续修改或重新导出不会改动历史交付文件。导出文件包含交付信息页（研究问题、版本状态、生成器、校验结论、导出时间）、正文（`[[S1]]` 引用保留原样）和来源清单附录。归档案件禁止导出，但保留列表与下载。

## 任务 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks` | 全局任务列表；可选 `case_id` 查询参数 |
| GET | `/api/tasks/{task_id}` | 任务当前状态、输入快照及全部执行历史 |
| POST | `/api/tasks/{task_id}/cancel` | 请求协作式取消，仅 `queued`、`running` 可用 |
| POST | `/api/tasks/{task_id}/retry` | 创建新执行记录；请求体 `{}` 沿用快照，`{"scope": {...}}` 修改本次范围 |

任务状态为 `queued`、`running`、`completed`、`partial`、`failed`、`cancelled`、`recovery_pending`。`progress` 为可空整数；只有执行器能真实计算进度时才返回 0–100，否则为 `null`。

取消只设置执行请求的协作标记，不承诺底层计算或远端 OCR 立即停止。执行器在检查点确认取消；若此前已保存部分结果，则保留结果并标记 `partial`，否则标记 `cancelled`。材料段和分来源证据先通过 `report_partial()` 落库，再检查取消。重试不会删除旧结果或错误，而是在 `task_attempts` 中追加记录；材料同时追加解析版本，检索证据按 attempt 隔离。`summarize` 重试只接受空请求体 `{}`，始终复用首次执行冻结的问题、来源全文、基线和生成器，不允许通过 `scope` 修改范围。

服务启动时，将上次遗留的 `queued`、`running` 任务及其当前执行记录标记为 `recovery_pending`，不会伪造成功；运行中的材料版本也进入待恢复状态并保留已有文本。用户可基于输入快照创建新执行。页面关闭不影响服务端任务记录。

正式环境没有通用的 `POST /api/tasks` 创建入口。解析和检索通过内部 `TaskService` 创建任务；创建时可传 `client_request_id`，请求快照会稳定规范化并计算 SHA-256。同键同内容返回既有任务且不重复调度，同键不同内容返回冲突。

## 内部执行器接入

应用实例通过 `app.state.task_service` 暴露任务服务。执行器接收 `TaskExecutionContext` 和本次执行的输入快照，使用检查点响应取消，并返回 `TaskOutcome`：

```python
from apps.workbench.backend.tasks import TaskOutcome


async def handler(context, input_snapshot):
    await context.report(
        current_stage="检索法规",
        progress_detail="正在执行已确认的检索词",
    )
    result = {"items": []}
    await context.checkpoint()
    return TaskOutcome(
        current_stage="已完成",
        progress_detail="检索完成",
        result=result,
    )


app.state.task_service.register_handler("search", handler)
await app.state.task_service.create_task(
    case_id=case_id,
    task_type="search",
    title="统一检索",
    input_snapshot={"query": "合同解除"},
    model_role=None,
)
```

长时间循环应在每轮调用 `await context.checkpoint()`。只有具备确定总量和已完成量时才向 `report` 传入 `progress`；中间结果可通过 `result` 保存，以便取消或异常时保留。

## 隔离演示联调

演示执行器和 `POST /api/demo/tasks` 仅在 `SLP_WORKBENCH_ENABLE_TASK_DEMO=1` 时注册。必须使用独立 `/tmp` 数据库，不得对正式开发库开启：

```bash
SLP_WORKBENCH_DATA_DIR=/tmp/slp-workbench-fr05 \
SLP_WORKBENCH_ENABLE_TASK_DEMO=1 \
.venv/bin/python -m uvicorn apps.workbench.backend.app:app --host 127.0.0.1 --port 8781
```

隔离页面会出现完成、部分完成、失败和可取消任务的创建按钮。正式实例不显示这些按钮，也不会生成演示数据。

## 验证

```bash
.venv/bin/python -m pytest apps/capability_adapter/tests apps/workbench/tests -q
cd apps/workbench/frontend && npm run build
```

工作台测试使用临时 SQLite 数据库和 fake gateway，sidecar 测试使用临时材料目录及裁判文书 FTS 数据库，不读写正式工作台数据。测试覆盖任务终态、幂等、无百分比语义、协作取消、重试历史、归档和案件隔离、原件与真实定位、重复材料、部分解析、来源路由、零命中、单源失败、证据隔离、原文缓存、服务重启恢复、v3→v4 迁移、摘要版本与引用校验，以及真实模型失败不降级。

浏览器验收应使用独立 `/tmp` 数据目录和独立端口，覆盖上传 boundary、重复确认、解析版本与段落定位、三种检索模式、材料版本与过滤器、实际检索词、来源部分失败、旧 Attempt、证据原文、跨 Attempt 研究选择、摘要编辑与确认、生成失败重试、归档只读、刷新恢复及窄屏布局。真实 OCR 验收还应记录进程 RSS。Chroma `PersistentClient` 启动会进入 SQLite 事务与迁移管理，不能视为严格只读；真实数据并发验收应使用 APFS copy-on-write 克隆等隔离副本，并核对原始库文件大小和修改时间未变化。不得连接 LM Studio 或修改、重启生产 8765、8766、8767 服务。
