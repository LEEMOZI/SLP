import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictInt, constr

from . import parser as parser_module
from .judgments import (
    InvalidSourceReference,
    JudgmentEngine,
    SourceNotFound,
    SourceUnavailable,
)
from .parser import MaterialParser, PARSER_VERSION, ParserLimits
from .retrieval import search as retrieval_search
from .litigation_generator import (
    LitigationGenerationError,
    LitigationGenerator,
    LitigationRequest,
)
from .review_generator import (
    ReviewGenerationError,
    ReviewGenerator,
    ReviewRequest,
)
from .summarizer import (
    SummaryGenerationError,
    SummaryGenerator,
    SummaryRequest,
    SummarySettings,
)

APP_VERSION = "1.0.0"


@dataclass(frozen=True)
class AdapterSettings:
    material_root: Path
    judgment_db_dir: Path
    parser_limits: ParserLimits
    token: str = ""
    judgment_deadline_s: float = 45.0
    summary: SummarySettings = field(default_factory=SummarySettings.from_env)

    @classmethod
    def from_env(cls) -> "AdapterSettings":
        module_root = Path(__file__).resolve().parents[2]
        deadline = _positive_float_env("SLP_JUDGMENT_SEARCH_DEADLINE_S", 45.0)
        return cls(
            material_root=Path(
                os.environ.get(
                    "SLP_WORKBENCH_MATERIAL_ROOT",
                    "~/.slp-workbench/dev/files/cases",
                )
            ).expanduser(),
            judgment_db_dir=Path(
                os.environ.get(
                    "SLP_JUDGMENT_DB_DIR",
                    str(module_root / "MCP" / "judgment-search" / "data" / "judgments"),
                )
            ).expanduser(),
            parser_limits=ParserLimits.from_env(),
            token=os.environ.get("SLP_CAPABILITY_ADAPTER_TOKEN", ""),
            judgment_deadline_s=deadline,
        )


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class ParseRequest(BaseModel):
    material_id: constr(strip_whitespace=True, min_length=1, max_length=128)
    parse_version_id: constr(strip_whitespace=True, min_length=1, max_length=128)
    path: constr(min_length=1, max_length=4096)
    sha256: constr(pattern=r"^[0-9a-fA-F]{64}$")


class SearchRequest(BaseModel):
    query: constr(strip_whitespace=True, min_length=1, max_length=2000)
    limit: StrictInt = Field(default=8, ge=1, le=50)
    filters: Dict[str, Any] = Field(default_factory=dict)


class JudgmentSearchRequest(BaseModel):
    query: constr(strip_whitespace=True, min_length=1, max_length=2000)
    year_from: Optional[StrictInt] = Field(default=None, ge=1000, le=9999)
    year_to: Optional[StrictInt] = Field(default=None, ge=1000, le=9999)
    court: constr(strip_whitespace=True, max_length=200) = ""
    cause: constr(strip_whitespace=True, max_length=200) = ""
    limit: StrictInt = Field(default=10, ge=1, le=50)


class JudgmentSourceRequest(BaseModel):
    year: StrictInt = Field(..., ge=1000, le=9999)
    rowid: StrictInt = Field(..., ge=1)


def create_app(
    settings: Optional[AdapterSettings] = None,
    material_parser: Optional[MaterialParser] = None,
    retriever: Any = None,
    judgment_engine: Optional[JudgmentEngine] = None,
    summary_generator: Optional[SummaryGenerator] = None,
    review_generator: Optional[ReviewGenerator] = None,
    litigation_generator: Optional[LitigationGenerator] = None,
) -> FastAPI:
    settings = settings or AdapterSettings.from_env()
    material_parser = material_parser or MaterialParser(
        settings.material_root, settings.parser_limits
    )
    judgment_engine = judgment_engine or JudgmentEngine(
        settings.judgment_db_dir, deadline_s=settings.judgment_deadline_s
    )
    summary_generator = summary_generator or SummaryGenerator(settings.summary)
    review_generator = review_generator or ReviewGenerator(
        settings=settings.summary, retriever=retriever
    )
    litigation_generator = litigation_generator or LitigationGenerator(
        settings=settings.summary, retriever=retriever
    )
    app = FastAPI(title="SLP Capability Adapter", version=APP_VERSION)
    app.state.settings = settings
    app.state.material_parser = material_parser
    app.state.retriever = retriever
    app.state.judgment_engine = judgment_engine
    app.state.summary_generator = summary_generator
    app.state.review_generator = review_generator
    app.state.litigation_generator = litigation_generator

    @app.middleware("http")
    async def internal_token_auth(request: Request, call_next):
        if (
            settings.token
            and request.url.path.startswith("/v1/")
            and request.url.path != "/v1/capabilities"
        ):
            supplied = request.headers.get("X-SLP-Internal-Token", "")
            if not hmac.compare_digest(supplied, settings.token):
                return JSONResponse(status_code=401, content={"detail": "未授权"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "请求参数无效"})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _error: Exception):
        return JSONResponse(status_code=500, content={"detail": "内部能力调用失败"})

    @app.get("/v1/capabilities")
    def capabilities() -> Dict[str, Any]:
        sources = [
            {"id": "law", "status": "lazy"},
            {"id": "guiding_cases", "status": "lazy"},
            {"id": "reference", "status": "lazy"},
            {
                "id": "judgments",
                "status": "available" if judgment_engine._available_dbs() else "unavailable",
            },
        ]
        return {
            "version": APP_VERSION,
            "parser": {
                "formats": ["pdf", "docx", "txt", "md"],
                "version": PARSER_VERSION,
                "ocr_available": parser_module.ocr_available(),
                "limits": settings.parser_limits.as_dict(),
            },
            "sources": sources,
            "summary": summary_generator.capability(),
            "review": review_generator.capability(),
        }

    @app.post("/v1/parse")
    def parse(payload: ParseRequest) -> StreamingResponse:
        def events():
            try:
                for event in material_parser.parse(
                    payload.material_id,
                    payload.parse_version_id,
                    payload.path,
                    payload.sha256,
                ):
                    yield json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
            except Exception:
                yield json.dumps(
                    {
                        "type": "error",
                        "material_id": payload.material_id,
                        "parse_version_id": payload.parse_version_id,
                        "status": "failed",
                        "error": {"code": "parse_failed", "message": "材料解析失败"},
                    },
                    ensure_ascii=False,
                ) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    def run_retrieval(source: str, payload: SearchRequest) -> Dict[str, Any]:
        return retrieval_search(
            source,
            payload.query,
            limit=payload.limit,
            filters=payload.filters,
            retriever=app.state.retriever,
        )

    @app.post("/v1/search/law")
    def search_law(payload: SearchRequest) -> Dict[str, Any]:
        return run_retrieval("law", payload)

    @app.post("/v1/search/guiding-cases")
    def search_guiding_cases(payload: SearchRequest) -> Dict[str, Any]:
        return run_retrieval("guiding_cases", payload)

    @app.post("/v1/search/reference")
    def search_reference(payload: SearchRequest) -> Dict[str, Any]:
        return run_retrieval("reference", payload)

    @app.post("/v1/search/judgments")
    def search_judgments(payload: JudgmentSearchRequest) -> Dict[str, Any]:
        if (
            payload.year_from is not None
            and payload.year_to is not None
            and payload.year_from > payload.year_to
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_year_range", "message": "起始年份不能晚于结束年份"},
            )
        return judgment_engine.search(
            payload.query,
            year_from=payload.year_from,
            year_to=payload.year_to,
            court=payload.court,
            cause=payload.cause,
            limit=payload.limit,
        )

    @app.post("/v1/judgments/source")
    def judgment_source(payload: JudgmentSourceRequest) -> Dict[str, Any]:
        try:
            return judgment_engine.read_source(payload.year, payload.rowid)
        except InvalidSourceReference as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_source_ref", "message": "来源引用无效"},
            ) from error
        except SourceNotFound as error:
            raise HTTPException(
                status_code=404,
                detail={"code": "source_not_found", "message": "来源不存在"},
            ) from error
        except SourceUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "source_unavailable", "message": "来源暂不可用"},
            ) from error

    @app.post("/v1/summaries")
    async def generate_summary(payload: SummaryRequest):
        try:
            return await summary_generator.generate(payload)
        except SummaryGenerationError as error:
            return JSONResponse(
                status_code=error.status_code,
                content={
                    "detail": {
                        "code": error.code,
                        "message": error.message,
                        "provider": payload.provider,
                    }
                },
            )

    @app.post("/v1/reviews")
    async def generate_review(payload: ReviewRequest):
        try:
            return review_generator.generate(payload)
        except ReviewGenerationError as error:
            return JSONResponse(
                status_code=error.status_code,
                content={
                    "detail": {
                        "code": error.code,
                        "message": error.message,
                    }
                },
            )

    @app.post("/v1/litigation")
    async def generate_litigation(payload: LitigationRequest):
        try:
            return litigation_generator.generate(payload)
        except LitigationGenerationError as error:
            return JSONResponse(
                status_code=error.status_code,
                content={
                    "detail": {
                        "code": error.code,
                        "message": error.message,
                    }
                },
            )

    return app


app = create_app()
