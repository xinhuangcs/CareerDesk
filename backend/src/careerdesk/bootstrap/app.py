"""FastAPI composition root for middleware, routes, resources, and frontend hosting."""

from pathlib import Path

from agentmaker import LLMConfigError
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..core.config import get_settings
from ..features.applications import api as applications_api
from ..features.grill import api as grill_api
from ..features.preferences import api as preferences_api
from ..features.questions import api as questions_api
from ..features.resumes import api as resumes_api
from ..features.reviews import api as reviews_api
from ..features.settings import api as settings_api
from ..orchestration.assistant.api import router as assistant_router
from ..orchestration.application_prep import api as application_prep_api
from ..orchestration.maintenance import api as maintenance_api
from ..orchestration.interview_generation import api as interview_generation_api
from ..platform.database import DatabaseBusy
from ..platform.http.request_limits import RequestBodyLimitMiddleware
from ..platform.http.request_trust import (RequestTrustMiddleware,
                                           WRITE_REQUEST_HEADER,
                                           WRITE_REQUEST_VALUE)
from ..platform.http.problem_details import (
    ProblemDetails,
    RequestIdMiddleware,
    install_problem_details_handlers,
    problem_response,
)
from ..platform.http.response_security import ResponseSecurityMiddleware
from ..platform.http.static import mount_frontend
from ..platform.ai.client import MODEL_CAPABILITY_MESSAGE, OutboundAccessDisabled
from ..platform.runtime import InstanceLock
from .lifespan import lifespan


def _document_browser_write_header(app: FastAPI) -> None:
    """Expose transport-only browser trust and Problem Details in OpenAPI.

    Middleware is intentionally transport-level, so FastAPI cannot infer this
    header from route signatures.  Adding a one-value required parameter keeps
    generated clients honest and makes local Swagger "Try it out" prefill it.
    """
    generated_openapi = app.openapi

    def openapi_with_write_header():
        schema = generated_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        problem_schema = ProblemDetails.model_json_schema(
            ref_template="#/components/schemas/{model}",
        )
        components.update(problem_schema.pop("$defs", {}))
        components["ProblemDetails"] = problem_schema
        documented_problem = {
            "description": "RFC 9457 Problem Details",
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                },
            },
        }
        for path, path_item in schema.get("paths", {}).items():
            for method in (
                "get", "put", "post", "delete", "options", "head", "patch", "trace",
            ):
                operation = path_item.get(method)
                if not isinstance(operation, dict):
                    continue
                responses = operation.setdefault("responses", {})
                responses.setdefault("default", documented_problem)
                if "422" in responses:
                    responses["422"] = documented_problem
                if path != "/api" and not path.startswith("/api/"):
                    continue
                if method not in {"post", "put", "patch", "delete"}:
                    continue
                parameters = operation.setdefault("parameters", [])
                if any(
                    item.get("in") == "header"
                    and item.get("name", "").lower() == WRITE_REQUEST_HEADER.lower()
                    for item in parameters
                    if isinstance(item, dict)
                ):
                    continue
                parameters.append({
                    "name": WRITE_REQUEST_HEADER,
                    "in": "header",
                    "required": True,
                    "description": "CareerDesk 浏览器写请求来源标记（固定值 1，不是密钥）",
                    "schema": {
                        "type": "string",
                        "enum": [WRITE_REQUEST_VALUE],
                        "default": WRITE_REQUEST_VALUE,
                    },
                })
        return schema

    app.openapi = openapi_with_write_header


def create_app(*, instance_lock: InstanceLock | None = None) -> FastAPI:
    """Build the app, disabling production API docs to hide the route map."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    install_problem_details_handlers(app)

    @app.exception_handler(DatabaseBusy)
    async def database_busy(request: Request, error: DatabaseBusy):
        """Fail interactive writes quickly while background writes hold the lock."""
        del error
        return problem_response(
            request.scope,
            status_code=503,
            type_uri="urn:careerdesk:problem:database-busy",
            title="Database busy",
            detail="后台任务正在写入，本次修改未保存；请稍后重试。",
            code="database_busy",
            extensions={"retryable": True},
        )

    @app.exception_handler(OutboundAccessDisabled)
    async def outbound_access_disabled(request: Request, error: OutboundAccessDisabled):
        """Return stable actionable policy rejection before headers, never a fake 500."""
        return problem_response(
            request.scope,
            status_code=409,
            type_uri="urn:careerdesk:problem:strict-offline",
            title="Outbound access disabled",
            detail=str(error),
            code="strict_offline",
        )

    @app.exception_handler(LLMConfigError)
    async def llm_config_error(request: Request, error: LLMConfigError):
        """Normalize provider configuration failures into safe retryable responses."""
        missing_capabilities = str(error) == MODEL_CAPABILITY_MESSAGE
        return problem_response(
            request.scope,
            status_code=409,
            type_uri=(
                "urn:careerdesk:problem:model-capabilities-missing"
                if missing_capabilities
                else "urn:careerdesk:problem:model-not-configured"
            ),
            title=(
                "Model capabilities missing"
                if missing_capabilities
                else "Model not configured"
            ),
            detail=(
                f"{MODEL_CAPABILITY_MESSAGE} 补齐后可直接重试。"
                if missing_capabilities
                else "模型尚未完成配置，请前往「模型与隐私」检查模型与凭证后重试。"
            ),
            code=(
                "model_capabilities_missing"
                if missing_capabilities
                else "model_not_configured"
            ),
        )

    if instance_lock is not None:
# Desktop acquires before startup and transfers ownership; Uvicorn/Docker acquire here.
        app.state.instance_lock = instance_lock
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        RequestTrustMiddleware,
        runtime_mode=settings.runtime_mode,
        allowed_origins=settings.allowed_origin_list,
    )
# Later middleware wraps earlier layers: validate host/origin before reading large bodies.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_host_list,
        www_redirect=False,
    )
# Apply API cache/browser policy consistently, including host/origin rejection responses.
    app.add_middleware(
        ResponseSecurityMiddleware,
# Settings may toggle the runtime master switch; each response reads current policy.
        strict_offline=lambda: get_settings().strict_offline,
    )
# The outermost layer assigns request IDs to success and every early rejection.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(assistant_router)
    app.include_router(applications_api.router)
    app.include_router(application_prep_api.router)
    app.include_router(interview_generation_api.router)
    app.include_router(interview_generation_api.grill_router)
    app.include_router(grill_api.router)
    app.include_router(preferences_api.router)
    app.include_router(resumes_api.router)
    app.include_router(questions_api.router)
    app.include_router(reviews_api.router)
    app.include_router(maintenance_api.router)
    app.include_router(settings_api.router)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> PlainTextResponse:
        """Return fixed health content without disk, version, or component details."""
        return PlainTextResponse("ok")

    mount_frontend(app, Path(settings.frontend_dist_dir))
    _document_browser_write_header(app)
    return app


app = create_app()
