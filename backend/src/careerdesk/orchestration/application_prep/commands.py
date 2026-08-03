"""Shared command boundary for starting the durable Application Prep job."""

from collections.abc import Callable
import logging
from uuid import uuid4

from ...features.applications import public as applications
from ...platform.ai.client import LLMClientOwnership, OutboundAccessDisabled
from ...platform.locale import OutputLocale

logger = logging.getLogger(__name__)


def _text(locale: OutputLocale, zh: str, en: str) -> str:
    return en if locale == "en" else zh


class PrepApplicationNotFound(LookupError):
    """The requested application is absent from the current tenant."""


def _known_reuse_claim(detail: dict, *, force: bool, restart_ready: bool) -> dict | None:
    prep_status = detail["prep_status"]
    if prep_status == "ready" and (force or not restart_ready):
        return {
            "status": "completed",
            "prep_status": prep_status,
            "takeover": False,
            "retry_after_seconds": None,
        }
    if prep_status not in ("pending", "running"):
        return None
    retry_after = detail["prep_retry_after_seconds"] or 0
    if not force or retry_after > 0:
        return {
            "status": "running",
            "takeover": False,
            "retry_after_seconds": retry_after,
        }
    return None


def _reused_response(
    claim: dict,
    *,
    application_id: int,
    refresh_research: bool,
    research_disabled: bool,
    output_locale: OutputLocale,
) -> dict | None:
    if claim["status"] == "completed":
        return {
            "status": "completed",
            "application_id": application_id,
            "prep_status": claim["prep_status"],
            "reused": True,
            "refresh_requested": refresh_research,
            "refresh_applied": False,
            "takeover_applied": False,
            "retry_after_seconds": None,
            "message": (
                _text(
                    output_locale,
                    "联网公司调研当前未启用，本次不会刷新网页材料；已加载现有调研。",
                    "Online company research is disabled, so web sources were not refreshed. Existing research was loaded.",
                )
                if research_disabled
                else _text(
                    output_locale,
                    "原任务已在接管前结束，已加载最新结果，没有重复调用模型",
                    "The previous task finished before takeover. The latest result was loaded without another model call.",
                )
            ),
        }
    if claim["status"] != "running":
        return None
    retry_after = claim["retry_after_seconds"] or 0
    if retry_after == 0:
        message = _text(
            output_locale,
            "原任务租约已到期，可重新启动生成",
            "The previous task lease has expired. Generation can be restarted safely.",
        )
    elif research_disabled:
        message = _text(
            output_locale,
            "联网公司调研当前未启用，本次不会刷新网页材料；"
            f"当前调研任务仍在运行，约 {retry_after} 秒后可安全接管",
            "Online company research is disabled, so web sources will not be refreshed. "
            f"The current task is still running and can be taken over safely in about {retry_after} seconds.",
        )
    elif refresh_research:
        message = _text(
            output_locale,
            f"已有任务正在运行，本次未另行刷新公司调研；约 {retry_after} 秒后可安全接管，"
            "或等待当前任务完成后再次刷新",
            "A task is already running, so company research was not refreshed again. "
            f"It can be taken over safely in about {retry_after} seconds, or refreshed after it finishes.",
        )
    else:
        message = _text(
            output_locale,
            f"已有调研任务正在运行；约 {retry_after} 秒后仍未完成即可安全接管",
            f"A research task is already running. If it is not finished in about {retry_after} seconds, it can be taken over safely.",
        )
    return {
        "status": "reused",
        "application_id": application_id,
        "reused": True,
        "refresh_requested": refresh_research,
        "refresh_applied": False,
        "takeover_applied": False,
        "retry_after_seconds": retry_after,
        "message": message,
    }


async def request_prep_generation(
    settings,
    user_id: str,
    application_id: int,
    *,
    force: bool = False,
    refresh_research: bool = False,
    schedule: Callable[..., None],
    output_locale: OutputLocale = "zh-CN",
) -> dict:
    """Validate, claim and schedule one prep job without depending on HTTP."""
    if settings.llm_model is None:
        return {"status": "error", "message": _text(
            output_locale,
            "生成公司调研需要模型，请先到「模型与隐私」完成配置",
            "Company research requires a model. Configure one under Model & Privacy first.",
        )}
    research_refresh = refresh_research and settings.web_research_enabled
    research_disabled = refresh_research and not settings.web_research_enabled
    detail = applications.application_detail(settings.db_path, user_id, application_id)
    if detail is None:
        raise PrepApplicationNotFound(application_id)
    restart_ready = refresh_research and not force
    if known_claim := _known_reuse_claim(
        detail,
        force=force,
        restart_ready=restart_ready,
    ):
        return _reused_response(
            known_claim,
            application_id=application_id,
            refresh_research=refresh_research,
            research_disabled=research_disabled,
            output_locale=output_locale,
        )

    from .factory import build_prep_llm, build_prep_service

    try:
        llm = build_prep_llm(settings)
    except OutboundAccessDisabled:
        raise
    except Exception:
        logger.exception("prep model initialization failed")
        return {"status": "error", "message": _text(
            output_locale,
            "调研模型初始化失败，请检查「模型与隐私」后重试",
            "The research model could not be initialized. Check Model & Privacy and retry.",
        )}

    async with LLMClientOwnership(llm) as ownership:
        generation = uuid4().hex
        claim = applications.claim_prep_generation(
            settings.db_path,
            user_id,
            application_id,
            generation,
            force=force,
            restart_ready=restart_ready,
        )
        if claim["status"] == "missing":
            raise PrepApplicationNotFound(application_id)
        if reused := _reused_response(
            claim,
            application_id=application_id,
            refresh_research=refresh_research,
            research_disabled=research_disabled,
            output_locale=output_locale,
        ):
            return reused
        try:
            service = build_prep_service(settings, llm=llm)
        except Exception:
            logger.exception("prep service initialization failed")
            message = _text(
                output_locale,
                "调研服务初始化失败，请检查「模型与隐私」后重试",
                "The research service could not be initialized. Check Model & Privacy and retry.",
            )
            applications.fail_prep_generation(
                settings.db_path,
                user_id,
                application_id,
                message,
                generation=generation,
            )
            return {"status": "error", "message": message}
        try:
            schedule(
                service,
                user_id,
                application_id,
                generation=generation,
                refresh_research=research_refresh,
                output_locale=output_locale,
            )
        except Exception:
            await service.close()
            logger.exception("prep task scheduling failed")
            message = _text(
                output_locale,
                "调研任务启动失败，请稍后重试",
                "The research task could not be started. Try again shortly.",
            )
            applications.fail_prep_generation(
                settings.db_path,
                user_id,
                application_id,
                message,
                generation=generation,
            )
            return {"status": "error", "message": message}
        ownership.transfer()
        response = {
            "status": "started",
            "application_id": application_id,
            "reused": False,
            "refresh_requested": refresh_research,
            "refresh_applied": research_refresh,
            "takeover_applied": claim["takeover"],
            "retry_after_seconds": claim["retry_after_seconds"],
        }
        if research_disabled:
            response["message"] = _text(
                output_locale,
                "联网公司调研当前未启用，本次不会刷新网页材料；"
                "岗位报告、建议答案等非联网内容已开始生成。",
                "Online company research is disabled, so web sources will not be refreshed. "
                "Offline content such as the role report and suggested answers is being generated.",
            )
        return response


__all__ = ["PrepApplicationNotFound", "request_prep_generation"]
