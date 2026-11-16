import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cases import (
    Case,
    CaseArchivedError,
    CaseConflictError,
    CaseCreate,
    CaseList,
    CaseRepository,
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
            return await task_service.retry(task_id, scope=scope)
        except SearchValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (TaskNotFoundError, CaseArchivedError, TaskStateError) as error:
            raise_task_error(error)

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
