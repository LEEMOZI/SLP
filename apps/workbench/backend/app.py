import os
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .cases import (
    Case,
    CaseArchivedError,
    CaseConflictError,
    CaseCreate,
    CaseList,
    CaseRepository,
    utc_now,
)
from .delivery import (
    DeliveryList,
    DeliveryNotFoundError,
    DeliveryRepository,
    DeliveryStorage,
    DeliveryVersion,
    ExportCreate,
    check_traceability,
    download_filename,
    extract_facts,
    render_docx,
    render_markdown,
    render_review_docx,
    render_review_markdown,
    summarize_facts,
)
from .health import HealthChecker, ReadinessReport
from .integrations import CapabilityError, CapabilityGateway
from .materials import (
    DuplicateMaterialError,
    MaterialDetail,
    MaterialIdempotencyConflictError,
    MaterialList,
    MaterialNotFoundError,
    MaterialParseHandler,
    MaterialRepository,
    MaterialStorage,
    MaterialUploadResult,
    MaterialValidationError,
    SegmentList,
)
from .searches import (
    EvidenceDetail,
    EvidenceNotFoundError,
    EvidenceRepository,
    SearchAttemptView,
    SearchCreate,
    SearchHandler,
    SearchHistory,
    SearchNotFoundError,
    SearchValidationError,
)
from .summaries import (
    SummaryConfirmationError,
    SummaryCreate,
    SummaryDetail,
    SummaryHandler,
    SummaryList,
    SummaryNotFoundError,
    SummaryRepository,
    SummarySourceRemovalError,
    SummarySourcesUpdate,
    SummaryValidationError,
    SummaryVersionCreate,
)
from .contract_reviews import (
    ContractReviewConfirmationError,
    ContractReviewCreate,
    ContractReviewDetail,
    ContractReviewList,
    ContractReviewNotFoundError,
    ContractReviewRepository,
    ContractReviewSourcesUpdate,
    ContractReviewValidationError,
    ContractReviewVersionCreate,
    LawSource,
    ReviewHandler,
    validate_version as validate_review_version,
)
from .litigation_documents import (
    LitigationDocumentConfirmationError,
    LitigationDocumentCreate,
    LitigationDocumentDetail,
    LitigationDocumentList,
    LitigationDocumentNotFoundError,
    LitigationDocumentRepository,
    LitigationDocumentValidationError,
    LitigationDocumentVersionCreate,
    LitigationHandler,
    validate_version as validate_litigation_version,
)
from .task_demo import DemoTaskCreate, register_demo_handler
from .task_service import TaskService
from .tasks import (
    Task,
    TaskCaseNotFoundError,
    TaskDetail,
    TaskIdempotencyConflictError,
    TaskList,
    TaskNotFoundError,
    TaskRepository,
    TaskRetry,
    TaskStateError,
    new_id,
)


def create_app(
    repository=None,
    health_checker=None,
    task_service=None,
    enable_task_demo=None,
    capability_gateway=None,
    data_dir=None,
) -> FastAPI:
    if data_dir is None:
        data_dir = (
            repository.db_path.parent
            if repository is not None
            else Path(os.getenv("SLP_WORKBENCH_DATA_DIR", "~/.slp-workbench/dev")).expanduser()
        )
    data_dir = Path(data_dir)
    repository = repository or CaseRepository(data_dir / "workbench.db")
    health_checker = health_checker or HealthChecker.from_env()
    capability_gateway = capability_gateway or CapabilityGateway()
    material_repository = MaterialRepository(repository)
    material_storage = MaterialStorage(data_dir)
    if enable_task_demo is None:
        enable_task_demo = os.getenv("SLP_WORKBENCH_ENABLE_TASK_DEMO") == "1"
    if task_service is None:
        task_service = TaskService(TaskRepository(repository))
    task_service.register_handler(
        "parse", MaterialParseHandler(material_repository, material_storage, capability_gateway)
    )
    evidence_repository = EvidenceRepository(repository)
    search_handler = SearchHandler(
        material_repository,
        evidence_repository,
        task_service.repository,
        capability_gateway,
    )
    task_service.register_handler("search", search_handler)
    summary_repository = SummaryRepository(repository)
    summary_handler = SummaryHandler(
        summary_repository,
        task_service.repository,
        capability_gateway,
    )
    task_service.register_handler("summarize", summary_handler)
    contract_review_repository = ContractReviewRepository(repository)
    review_handler = ReviewHandler(
        contract_review_repository,
        task_service.repository,
        capability_gateway,
    )
    task_service.register_handler("review", review_handler)
    litigation_document_repository = LitigationDocumentRepository(repository)
    litigation_handler = LitigationHandler(
        litigation_document_repository,
        task_service.repository,
        capability_gateway,
    )
    task_service.register_handler("litigation", litigation_handler)
    delivery_repository = DeliveryRepository(repository)
    delivery_storage = DeliveryStorage(data_dir)
    if enable_task_demo:
        register_demo_handler(task_service)

    @asynccontextmanager
    async def lifespan(_):
        repository.initialize()
        task_service.repository.initialize()
        task_service.recover_interrupted()
        material_repository.recover_interrupted()
        try:
            yield
        finally:
            await task_service.shutdown()
            await capability_gateway.close()

    app = FastAPI(title="SLP 法律工作台", version="0.1.0", lifespan=lifespan)
    app.state.task_service = task_service
    app.state.material_repository = material_repository
    app.state.evidence_repository = evidence_repository
    app.state.search_handler = search_handler
    app.state.summary_repository = summary_repository
    app.state.summary_handler = summary_handler
    app.state.capability_gateway = capability_gateway

    def raise_task_error(error):
        if isinstance(error, (TaskNotFoundError, TaskCaseNotFoundError)):
            detail = "任务不存在" if isinstance(error, TaskNotFoundError) else "案件不存在"
            raise HTTPException(status_code=404, detail=detail) from error
        if isinstance(error, CaseArchivedError):
            raise HTTPException(status_code=409, detail="案件已归档，不能创建或重试任务") from error
        if isinstance(error, TaskIdempotencyConflictError):
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_conflict", "message": "请求键已用于不同内容"},
            ) from error
        raise HTTPException(status_code=409, detail=str(error)) from error

    def raise_summary_error(error):
        if isinstance(error, SummaryNotFoundError):
            raise HTTPException(status_code=404, detail="研究摘要不存在") from error
        if isinstance(error, TaskCaseNotFoundError):
            raise HTTPException(status_code=404, detail="案件不存在") from error
        if isinstance(error, CaseArchivedError):
            raise HTTPException(status_code=409, detail="案件已归档，研究摘要为只读") from error
        if isinstance(error, SummarySourceRemovalError):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "source_removal_requires_confirmation",
                    "message": "移除来源会影响当前摘要引用，请确认后继续",
                    "affected": [item.model_dump() for item in error.affected],
                },
            ) from error
        if isinstance(error, SummaryConfirmationError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "summary_validation_failed",
                    "message": "摘要仍存在引用或内容问题",
                    "issues": [item.model_dump() for item in error.issues],
                },
            ) from error
        if isinstance(error, SummaryValidationError):
            raise HTTPException(status_code=422, detail=str(error)) from error
        raise_task_error(error)

    def raise_review_error(error):
        if isinstance(error, ContractReviewNotFoundError):
            raise HTTPException(status_code=404, detail="合同审查不存在") from error
        if isinstance(error, TaskCaseNotFoundError):
            raise HTTPException(status_code=404, detail="案件不存在") from error
        if isinstance(error, CaseArchivedError):
            raise HTTPException(status_code=409, detail="案件已归档，合同审查为只读") from error
        if isinstance(error, ContractReviewConfirmationError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "contract_review_validation_failed",
                    "message": "合同审查仍存在引用或内容问题",
                    "issues": [item.model_dump() for item in error.issues],
                },
            ) from error
        if isinstance(error, ContractReviewValidationError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "contract_review_validation_failed",
                    "message": str(error),
                },
            ) from error
        raise_task_error(error)

    def raise_litigation_error(error):
        if isinstance(error, LitigationDocumentNotFoundError):
            raise HTTPException(status_code=404, detail="诉讼文书不存在") from error
        if isinstance(error, TaskCaseNotFoundError):
            raise HTTPException(status_code=404, detail="案件不存在") from error
        if isinstance(error, CaseArchivedError):
            raise HTTPException(status_code=409, detail="案件已归档，诉讼文书为只读") from error
        if isinstance(error, LitigationDocumentConfirmationError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "litigation_validation_failed",
                    "message": "诉讼文书仍存在引用或内容问题",
                    "issues": [item.model_dump() for item in error.issues],
                },
            ) from error
        if isinstance(error, LitigationDocumentValidationError):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "litigation_validation_failed",
                    "message": str(error),
                },
            ) from error
        raise_task_error(error)

    @app.get("/api/readiness", response_model=ReadinessReport)
    async def readiness():
        return await health_checker.check()

    @app.get("/api/capabilities")
    async def capabilities():
        try:
            return await capability_gateway.capabilities()
        except CapabilityError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/api/cases/{case_id}/materials",
        response_model=MaterialUploadResult,
        status_code=201,
    )
    async def upload_material(
        case_id: str,
        file: UploadFile = File(...),
        confirm_duplicate: bool = Form(default=False),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise HTTPException(status_code=422, detail="Idempotency-Key 无效")
        try:
            material, _ = await material_storage.store(
                material_repository,
                case_id,
                file,
                confirm_duplicate,
                idempotency_key,
            )
            task = await task_service.create_task(
                case_id=case_id,
                task_type="parse",
                title=f"解析材料：{material.original_name}",
                input_snapshot={"material_id": material.id},
                client_request_id=idempotency_key,
            )
            material = material_repository.attach_task(material.id, task.id)
            return MaterialUploadResult(material=material, task=task)
        except DuplicateMaterialError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_material",
                    "message": "相同内容的材料已存在；确认后可作为独立材料上传",
                    "duplicates": [item.model_dump() for item in error.duplicates],
                },
            ) from error
        except MaterialIdempotencyConflictError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_conflict", "message": "请求键已用于不同文件"},
            ) from error
        except MaterialValidationError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code, "message": error.message},
            ) from error
        except (TaskCaseNotFoundError, CaseArchivedError, TaskStateError) as error:
            raise_task_error(error)

    @app.get("/api/cases/{case_id}/materials", response_model=MaterialList)
    def list_materials(case_id: str):
        try:
            material_repository.require_case(case_id)
        except TaskCaseNotFoundError as error:
            raise_task_error(error)
        items = material_repository.list(case_id)
        return MaterialList(items=items, total=len(items))

    @app.get("/api/materials/{material_id}", response_model=MaterialDetail)
    def get_material(material_id: str):
        try:
            return MaterialDetail(
                material=material_repository.get(material_id),
                versions=material_repository.list_versions(material_id),
            )
        except MaterialNotFoundError as error:
            raise HTTPException(status_code=404, detail="材料不存在") from error

    @app.get("/api/materials/{material_id}/original")
    def download_material(material_id: str):
        try:
            material = material_repository.get(material_id)
            path = material_storage.original_path(material_repository, material_id)
        except (MaterialNotFoundError, OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="材料原件不存在") from error
        return FileResponse(
            path,
            filename=material.original_name,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get(
        "/api/materials/{material_id}/versions/{version_id}/segments",
        response_model=SegmentList,
    )
    def list_material_segments(
        material_id: str,
        version_id: str,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        try:
            return material_repository.list_segments(
                material_id, version_id, cursor=cursor, limit=limit
            )
        except MaterialNotFoundError as error:
            raise HTTPException(status_code=404, detail="材料解析版本不存在") from error

    @app.post("/api/cases/{case_id}/searches", response_model=Task, status_code=201)
    async def create_search(
        case_id: str,
        payload: SearchCreate,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise HTTPException(status_code=422, detail="Idempotency-Key 无效")
        try:
            snapshot = search_handler.prepare(case_id, payload)
            return await task_service.create_task(
                case_id=case_id,
                task_type="search",
                title="统一法规与类案检索",
                input_snapshot=snapshot,
                client_request_id=idempotency_key,
            )
        except SearchValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (
            TaskCaseNotFoundError,
            CaseArchivedError,
            TaskStateError,
            TaskIdempotencyConflictError,
        ) as error:
            raise_task_error(error)

    @app.get("/api/cases/{case_id}/searches", response_model=SearchHistory)
    def list_searches(case_id: str):
        try:
            return search_handler.history(case_id)
        except TaskCaseNotFoundError as error:
            raise_task_error(error)

    @app.get("/api/searches/{task_id}/attempts/{attempt_id}", response_model=SearchAttemptView)
    def get_search_attempt(task_id: str, attempt_id: str):
        try:
            return search_handler.attempt_view(task_id, attempt_id)
        except (TaskNotFoundError, SearchNotFoundError) as error:
            raise HTTPException(status_code=404, detail="检索执行记录不存在") from error

    @app.get("/api/evidence/{evidence_id}", response_model=EvidenceDetail)
    async def get_evidence(evidence_id: str):
        try:
            return await search_handler.evidence_detail(evidence_id)
        except EvidenceNotFoundError as error:
            raise HTTPException(status_code=404, detail="证据不存在") from error

    @app.post(
        "/api/cases/{case_id}/summaries",
        response_model=SummaryDetail,
        status_code=201,
    )
    def create_summary(case_id: str, payload: SummaryCreate):
        try:
            summary = summary_repository.create(case_id, payload)
            return summary_repository.detail(summary.id)
        except (
            TaskCaseNotFoundError,
            CaseArchivedError,
            SummaryValidationError,
        ) as error:
            raise_summary_error(error)

    @app.get("/api/cases/{case_id}/summaries", response_model=SummaryList)
    def list_summaries(case_id: str):
        try:
            items = summary_repository.list(case_id)
            return SummaryList(items=items, total=len(items))
        except TaskCaseNotFoundError as error:
            raise_summary_error(error)

    @app.get("/api/summaries/{summary_id}", response_model=SummaryDetail)
    def get_summary(summary_id: str):
        try:
            return summary_repository.detail(summary_id)
        except SummaryNotFoundError as error:
            raise_summary_error(error)

    @app.put("/api/summaries/{summary_id}/sources", response_model=SummaryDetail)
    def update_summary_sources(summary_id: str, payload: SummarySourcesUpdate):
        try:
            return summary_repository.update_sources(summary_id, payload)
        except (
            SummaryNotFoundError,
            CaseArchivedError,
            SummaryValidationError,
            SummarySourceRemovalError,
        ) as error:
            raise_summary_error(error)

    @app.post(
        "/api/summaries/{summary_id}/versions",
        response_model=SummaryDetail,
        status_code=201,
    )
    def save_summary_version(summary_id: str, payload: SummaryVersionCreate):
        try:
            return summary_repository.save_manual_version(summary_id, payload)
        except (
            SummaryNotFoundError,
            CaseArchivedError,
            SummaryValidationError,
        ) as error:
            raise_summary_error(error)

    @app.post("/api/summaries/{summary_id}/generate", response_model=Task, status_code=201)
    async def generate_summary(
        summary_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise HTTPException(status_code=422, detail="Idempotency-Key 无效")
        try:
            summary = summary_repository.get(summary_id)
            summary_repository.require_case(summary.case_id, writable=True)
            if idempotency_key is not None:
                existing = task_service.repository.find_by_client_request(
                    summary.case_id, "summarize", idempotency_key
                )
                if existing is not None:
                    if existing.input_snapshot.get("summary_id") != summary_id:
                        raise TaskIdempotencyConflictError(idempotency_key)
                    summary_repository.attach_task(summary_id, existing.id)
                    return existing
            snapshot = summary_handler.prepare(summary_id)
            task = await task_service.create_task(
                case_id=summary.case_id,
                task_type="summarize",
                title=f"生成研究摘要：{summary.title}",
                model_role="research_summary",
                input_snapshot=snapshot,
                client_request_id=idempotency_key,
            )
            summary_repository.attach_task(summary_id, task.id)
            return task
        except (
            SummaryNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            SummaryValidationError,
            TaskStateError,
            TaskIdempotencyConflictError,
        ) as error:
            raise_summary_error(error)

    @app.post("/api/summaries/{summary_id}/confirm", response_model=SummaryDetail)
    def confirm_summary(summary_id: str):
        try:
            return summary_repository.confirm(summary_id)
        except (
            SummaryNotFoundError,
            CaseArchivedError,
            SummaryConfirmationError,
        ) as error:
            raise_summary_error(error)

    @app.post(
        "/api/cases/{case_id}/reviews",
        response_model=ContractReviewDetail,
        status_code=201,
    )
    def create_review(case_id: str, payload: ContractReviewCreate):
        try:
            contract_review_repository.require_case(case_id, writable=True)
            review = contract_review_repository.create(
                case_id,
                payload.material_id,
                title=payload.title,
                contract_type=payload.contract_type,
                focus_points=payload.focus_points,
            )
            return contract_review_repository.detail(review.id)
        except (
            TaskCaseNotFoundError,
            CaseArchivedError,
            ContractReviewValidationError,
        ) as error:
            raise_review_error(error)

    @app.get(
        "/api/cases/{case_id}/reviews", response_model=ContractReviewList
    )
    def list_reviews(case_id: str):
        try:
            contract_review_repository.require_case(case_id)
        except TaskCaseNotFoundError as error:
            raise_review_error(error)
        items = contract_review_repository.list(case_id)
        return ContractReviewList(items=items, total=len(items))

    @app.get(
        "/api/reviews/{review_id}", response_model=ContractReviewDetail
    )
    def get_review(review_id: str):
        try:
            return contract_review_repository.detail(review_id)
        except ContractReviewNotFoundError as error:
            raise_review_error(error)

    @app.post(
        "/api/reviews/{review_id}/versions",
        response_model=ContractReviewDetail,
        status_code=201,
    )
    def save_review_version(review_id: str, payload: ContractReviewVersionCreate):
        try:
            contract_review_repository.require_case(
                contract_review_repository.get(review_id).case_id, writable=True
            )
            version = contract_review_repository.save_manual_version(
                review_id,
                body=payload.body,
                base_version_id=payload.base_version_id,
            )
            return contract_review_repository.detail(review_id)
        except ContractReviewNotFoundError as error:
            raise_review_error(error)
        except (TaskCaseNotFoundError, CaseArchivedError) as error:
            raise_review_error(error)

    @app.post("/api/reviews/{review_id}/confirm", response_model=ContractReviewDetail)
    def confirm_review(review_id: str):
        try:
            review = contract_review_repository.get(review_id)
            contract_review_repository.require_case(review.case_id, writable=True)
            return contract_review_repository.confirm(review_id)
        except (
            ContractReviewNotFoundError,
            CaseArchivedError,
            ContractReviewConfirmationError,
        ) as error:
            raise_review_error(error)

    @app.post(
        "/api/reviews/{review_id}/generate", response_model=Task, status_code=201
    )
    async def generate_review(review_id: str, client_request_id: Optional[str] = Header(default=None)):
        try:
            review = contract_review_repository.get(review_id)
            contract_review_repository.require_case(review.case_id, writable=True)
            snapshot = review_handler.prepare(review_id)
            task = await task_service.create_task(
                case_id=review.case_id,
                task_type="review",
                title=f"合同审查：{review.title}",
                input_snapshot=snapshot,
                model_role="contract_review",
                client_request_id=client_request_id,
            )
            return task
        except (
            ContractReviewNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            ContractReviewValidationError,
            TaskStateError,
            TaskIdempotencyConflictError,
        ) as error:
            raise_review_error(error)

    # ---- 诉讼文书起草路由 ----

    @app.post(
        "/api/cases/{case_id}/litigation-docs",
        response_model=LitigationDocumentDetail,
        status_code=201,
    )
    def create_litigation_doc(case_id: str, payload: LitigationDocumentCreate):
        try:
            litigation_document_repository.require_case(case_id, writable=True)
            doc = litigation_document_repository.create(
                case_id,
                doc_type=payload.doc_type,
                title=payload.title,
                parties=payload.parties,
                claims=payload.claims,
                facts=payload.facts,
                focus_points=payload.focus_points,
            )
            return litigation_document_repository.detail(doc.id)
        except (
            TaskCaseNotFoundError,
            CaseArchivedError,
            LitigationDocumentValidationError,
        ) as error:
            raise_litigation_error(error)

    @app.get(
        "/api/cases/{case_id}/litigation-docs",
        response_model=LitigationDocumentList,
    )
    def list_litigation_docs(case_id: str):
        try:
            litigation_document_repository.require_case(case_id)
        except TaskCaseNotFoundError as error:
            raise_litigation_error(error)
        items = litigation_document_repository.list(case_id)
        return LitigationDocumentList(items=items, total=len(items))

    @app.get(
        "/api/litigation-docs/{doc_id}",
        response_model=LitigationDocumentDetail,
    )
    def get_litigation_doc(doc_id: str):
        try:
            return litigation_document_repository.detail(doc_id)
        except LitigationDocumentNotFoundError as error:
            raise_litigation_error(error)

    @app.post(
        "/api/litigation-docs/{doc_id}/versions",
        response_model=LitigationDocumentDetail,
        status_code=201,
    )
    def save_litigation_version(doc_id: str, payload: LitigationDocumentVersionCreate):
        try:
            doc = litigation_document_repository.get(doc_id)
            litigation_document_repository.require_case(doc.case_id, writable=True)
            litigation_document_repository.save_manual_version(
                doc_id,
                body=payload.body,
                base_version_id=payload.base_version_id,
            )
            return litigation_document_repository.detail(doc_id)
        except LitigationDocumentNotFoundError as error:
            raise_litigation_error(error)
        except (TaskCaseNotFoundError, CaseArchivedError) as error:
            raise_litigation_error(error)

    @app.post(
        "/api/litigation-docs/{doc_id}/generate",
        response_model=Task,
        status_code=201,
    )
    async def generate_litigation_doc(
        doc_id: str,
        client_request_id: Optional[str] = Header(default=None),
    ):
        try:
            doc = litigation_document_repository.get(doc_id)
            litigation_document_repository.require_case(doc.case_id, writable=True)
            snapshot = litigation_handler.prepare(doc_id)
            task = await task_service.create_task(
                case_id=doc.case_id,
                task_type="litigation",
                title=f"诉讼文书起草：{doc.title}",
                input_snapshot=snapshot,
                model_role="litigation_drafting",
                client_request_id=client_request_id,
            )
            return task
        except (
            LitigationDocumentNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            LitigationDocumentValidationError,
            TaskStateError,
            TaskIdempotencyConflictError,
        ) as error:
            raise_litigation_error(error)

    @app.post(
        "/api/litigation-docs/{doc_id}/confirm",
        response_model=LitigationDocumentDetail,
    )
    def confirm_litigation_doc(doc_id: str):
        try:
            doc = litigation_document_repository.get(doc_id)
            litigation_document_repository.require_case(doc.case_id, writable=True)
            return litigation_document_repository.confirm(doc_id)
        except (
            LitigationDocumentNotFoundError,
            CaseArchivedError,
            LitigationDocumentConfirmationError,
        ) as error:
            raise_litigation_error(error)

    @app.post(
        "/api/litigation-docs/{doc_id}/exports",
        response_model=DeliveryVersion,
        status_code=201,
    )
    def export_litigation_doc(doc_id: str, payload: ExportCreate):
        try:
            doc = litigation_document_repository.get(doc_id)
            litigation_document_repository.require_case(doc.case_id, writable=True)
            detail = litigation_document_repository.detail(doc_id)
            version = detail.current_version
            if version is None or version.status != "confirmed":
                raise LitigationDocumentValidationError(
                    "当前版本未确认，不能导出；请先确认版本"
                )
            issues = validate_litigation_version(doc, version)
            if issues and not payload.keep_issues:
                raise LitigationDocumentConfirmationError(issues)
            status = "draft" if issues else "official"
            exported_at = utc_now()
            if payload.format == "markdown":
                content = render_review_markdown(
                    doc.title,
                    doc.doc_type,
                    doc.focus_points,
                    version,
                    status,
                    issues,
                    exported_at,
                ).encode("utf-8")
            else:
                content = render_review_docx(
                    doc.title,
                    doc.doc_type,
                    doc.focus_points,
                    version,
                    status,
                    issues,
                    exported_at,
                )
            delivery_id = new_id()
            storage = delivery_storage.save(delivery_id, payload.format, content)
            return delivery_repository.create(
                delivery_id=delivery_id,
                case_id=doc.case_id,
                summary_id=None,
                review_id=None,
                kind="litigation_document",
                version_id=version.id,
                summary_title=doc.title,
                version_number=version.version_number,
                fmt=payload.format,
                status=status,
                storage=storage,
                facts={},
                issues=issues,
                exported_at=exported_at,
                generator_provider=version.generator_provider,
                actual_model=version.actual_model,
                task_id=version.task_id,
            )
        except (
            LitigationDocumentNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            LitigationDocumentValidationError,
            LitigationDocumentConfirmationError,
        ) as error:
            raise_litigation_error(error)

    @app.post(
        "/api/summaries/{summary_id}/exports",
        response_model=DeliveryVersion,
        status_code=201,
    )
    def export_summary(summary_id: str, payload: ExportCreate):
        try:
            summary = summary_repository.get(summary_id)
            summary_repository.require_case(summary.case_id, writable=True)
            detail = summary_repository.detail(summary_id)
            version = detail.current_version
            if version is None or version.status != "confirmed":
                raise SummaryValidationError("当前版本未确认，不能导出；请先确认版本")
            issues = list(summary_repository.validate_version(detail.summary, version))
            issues.extend(check_traceability(version.body, version.citations))
            if issues and not payload.keep_issues:
                raise SummaryConfirmationError(issues)
            status = "draft" if issues else "official"
            exported_at = utc_now()
            if payload.format == "markdown":
                content = render_markdown(
                    summary.title,
                    summary.question,
                    version,
                    status,
                    issues,
                    exported_at,
                ).encode("utf-8")
            else:
                content = render_docx(
                    summary.title,
                    summary.question,
                    version,
                    status,
                    issues,
                    exported_at,
                )
            delivery_id = new_id()
            storage = delivery_storage.save(delivery_id, payload.format, content)
            return delivery_repository.create(
                delivery_id=delivery_id,
                case_id=summary.case_id,
                summary_id=summary.id,
                review_id=None,
                kind="summary",
                version_id=version.id,
                summary_title=summary.title,
                version_number=version.version_number,
                fmt=payload.format,
                status=status,
                storage=storage,
                facts=summarize_facts(extract_facts(version.body)),
                issues=issues,
                exported_at=exported_at,
                generator_provider=version.generator_provider,
                actual_model=version.actual_model,
                task_id=version.task_id,
            )
        except (
            SummaryNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            SummaryValidationError,
            SummaryConfirmationError,
        ) as error:
            raise_summary_error(error)

    @app.post(
        "/api/reviews/{review_id}/exports",
        response_model=DeliveryVersion,
        status_code=201,
    )
    def export_review(review_id: str, payload: ExportCreate):
        try:
            review = contract_review_repository.get(review_id)
            contract_review_repository.require_case(review.case_id, writable=True)
            detail = contract_review_repository.detail(review_id)
            version = detail.current_version
            if version is None or version.status != "confirmed":
                raise ContractReviewValidationError(
                    "当前版本未确认，不能导出；请先确认版本"
                )
            # 合同审查的 review citations 不含 Evidence 引用，用 validate_version 做法条引用校验
            issues = validate_review_version(review, version)
            if issues and not payload.keep_issues:
                raise ContractReviewConfirmationError(issues)
            status = "draft" if issues else "official"
            exported_at = utc_now()
            if payload.format == "markdown":
                content = render_review_markdown(
                    review.title,
                    review.contract_type,
                    review.focus_points,
                    version,
                    status,
                    issues,
                    exported_at,
                ).encode("utf-8")
            else:
                content = render_review_docx(
                    review.title,
                    review.contract_type,
                    review.focus_points,
                    version,
                    status,
                    issues,
                    exported_at,
                )
            delivery_id = new_id()
            storage = delivery_storage.save(delivery_id, payload.format, content)
            return delivery_repository.create(
                delivery_id=delivery_id,
                case_id=review.case_id,
                summary_id=None,
                review_id=review.id,
                kind="contract_review",
                version_id=version.id,
                summary_title=review.title,
                version_number=version.version_number,
                fmt=payload.format,
                status=status,
                storage=storage,
                facts={},  # 合同审查暂不做自动事实抽取
                issues=issues,
                exported_at=exported_at,
                generator_provider=version.generator_provider,
                actual_model=version.actual_model,
                task_id=version.task_id,
            )
        except (
            ContractReviewNotFoundError,
            TaskCaseNotFoundError,
            CaseArchivedError,
            ContractReviewValidationError,
            ContractReviewConfirmationError,
        ) as error:
            raise_review_error(error)

    @app.get("/api/cases/{case_id}/deliveries", response_model=DeliveryList)
    def list_deliveries(case_id: str):
        try:
            summary_repository.require_case(case_id)
        except TaskCaseNotFoundError as error:
            raise_summary_error(error)
        items = delivery_repository.list_by_case(case_id)
        return DeliveryList(items=items, total=len(items))

    @app.get("/api/deliveries/{delivery_id}/download")
    def download_delivery(delivery_id: str):
        try:
            delivery = delivery_repository.get(delivery_id)
            path = delivery_storage.path_for(delivery.storage_key)
        except (DeliveryNotFoundError, OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="交付文件不存在") from error
        return FileResponse(
            path,
            filename=download_filename(delivery),
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/cases", response_model=CaseList)
    def list_cases(include_archived: bool = Query(default=False)):
        items = repository.list(include_archived=include_archived)
        return CaseList(items=items, total=len(items))

    @app.post("/api/cases", response_model=Case, status_code=201)
    def create_case(payload: CaseCreate):
        try:
            return repository.create(payload.name)
        except CaseConflictError as error:
            raise HTTPException(status_code=409, detail="同名案件已存在，请使用其他名称") from error

    @app.get("/api/cases/{case_id}", response_model=Case)
    def get_case(case_id: str):
        case = repository.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        return case

    @app.post("/api/cases/{case_id}/open", response_model=Case)
    def open_case(case_id: str):
        try:
            case = repository.open(case_id)
        except CaseArchivedError as error:
            raise HTTPException(status_code=409, detail="案件已归档，不能作为当前工作案件打开") from error
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        return case

    @app.post("/api/cases/{case_id}/archive", response_model=Case)
    def archive_case(case_id: str):
        case = repository.archive(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        return case

    @app.get("/api/cases/{case_id}/dashboard")
    def case_dashboard(case_id: str):
        """案件概览：聚合材料、检索、摘要、审查、文书、任务、交付的统计。"""
        case = repository.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        with repository.connect() as conn:
            def _count(table: str, extra: str = "") -> int:
                sql = f"SELECT COUNT(*) AS c FROM {table} WHERE case_id = ?"
                if extra:
                    sql += f" AND {extra}"
                row = conn.execute(sql, (case_id,)).fetchone()
                return row["c"] if row else 0

            materials = _count("materials")
            searches = _count("searches")
            summaries = _count("research_summaries")
            summaries_confirmed = _count("research_summaries", "status = 'confirmed'")
            reviews = _count("contract_reviews")
            reviews_confirmed = _count("contract_reviews", "current_status = 'confirmed'")
            litigation_docs = _count("litigation_documents")
            litigation_confirmed = _count(
                "litigation_documents",
                "current_version_id IS NOT NULL",
            )
            tasks_total = _count("tasks")
            tasks_running = _count("tasks", "status IN ('queued', 'running')")
            tasks_completed = _count("tasks", "status = 'completed'")
            tasks_failed = _count("tasks", "status = 'failed'")
            deliveries = _count("delivery_versions")

            # 任务类型分布
            task_types: dict[str, int] = {}
            for row in conn.execute(
                "SELECT task_type, COUNT(*) AS c FROM tasks WHERE case_id = ? GROUP BY task_type",
                (case_id,),
            ):
                task_types[row["task_type"]] = row["c"]

            # 最近 5 条任务
            recent_tasks = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "task_type": r["task_type"],
                    "updated_at": r["updated_at"],
                }
                for r in conn.execute(
                    "SELECT id, title, status, task_type, updated_at FROM tasks WHERE case_id = ? ORDER BY updated_at DESC LIMIT 5",
                    (case_id,),
                )
            ]

        return {
            "case": case.model_dump(),
            "stats": {
                "materials": materials,
                "searches": searches,
                "summaries": summaries,
                "summaries_confirmed": summaries_confirmed,
                "reviews": reviews,
                "reviews_confirmed": reviews_confirmed,
                "litigation_docs": litigation_docs,
                "litigation_confirmed": litigation_confirmed,
                "tasks_total": tasks_total,
                "tasks_running": tasks_running,
                "tasks_completed": tasks_completed,
                "tasks_failed": tasks_failed,
                "deliveries": deliveries,
            },
            "task_types": task_types,
            "recent_tasks": recent_tasks,
        }

    @app.get("/api/tasks", response_model=TaskList)
    def list_tasks(case_id: Optional[str] = Query(default=None)):
        items = task_service.repository.list(case_id=case_id)
        return TaskList(items=items, total=len(items), demo_enabled=enable_task_demo)

    @app.get("/api/tasks/{task_id}", response_model=TaskDetail)
    def get_task(task_id: str):
        try:
            return task_service.repository.get_detail(task_id)
        except (TaskNotFoundError, TaskStateError) as error:
            raise_task_error(error)

    @app.post("/api/tasks/{task_id}/cancel", response_model=Task)
    def cancel_task(task_id: str):
        try:
            return task_service.cancel(task_id)
        except (TaskNotFoundError, TaskStateError) as error:
            raise_task_error(error)

    @app.post("/api/tasks/{task_id}/retry", response_model=Task)
    async def retry_task(task_id: str, payload: Optional[TaskRetry] = None):
        try:
            scope = payload.scope if payload else None
            if scope is not None:
                task = task_service.repository.get(task_id)
                if task.task_type == "search":
                    scope = search_handler.normalize_retry(task.case_id, task.input_snapshot, scope)
                elif task.task_type == "summarize":
                    scope = summary_handler.normalize_retry(scope)
            return await task_service.retry(task_id, scope=scope)
        except (SearchValidationError, SummaryValidationError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (TaskNotFoundError, CaseArchivedError, TaskStateError) as error:
            raise_task_error(error)

    @app.get("/api/tasks/{task_id}/stream")
    async def stream_task(task_id: str):
        """SSE 流式推送任务进度事件，任务终态后关闭连接。"""
        async def event_generator():
            last_progress = -1
            while True:
                try:
                    detail = task_service.repository.get_detail(task_id)
                except (TaskNotFoundError, TaskStateError):
                    yield f"data: {json.dumps({'type': 'error', 'message': '任务不存在'})}\n\n"
                    return
                progress_changed = (detail.progress or 0) != last_progress
                status_terminal = detail.status in ("completed", "partial", "failed", "cancelled")
                if progress_changed or status_terminal:
                    last_progress = detail.progress or 0
                    yield f"data: {json.dumps(detail.model_dump(), ensure_ascii=False)}\n\n"
                if status_terminal:
                    return
                await asyncio.sleep(0.5)
        return StreamingResponse(event_generator(), media_type="text/event-stream")

    if enable_task_demo:
        @app.post("/api/demo/tasks", response_model=Task, status_code=201)
        async def create_demo_task(payload: DemoTaskCreate):
            try:
                return await task_service.create_task(
                    case_id=payload.case_id,
                    task_type="demo",
                    title="隔离演示任务",
                    input_snapshot={
                        "outcome": payload.outcome,
                        "delay_ms": payload.delay_ms,
                    },
                )
            except (TaskCaseNotFoundError, CaseArchivedError, TaskStateError) as error:
                raise_task_error(error)

    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend_dist.exists():
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(frontend_dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def index():
            return {"name": "SLP 法律工作台 API", "status": "ok"}

    return app


app = create_app()
