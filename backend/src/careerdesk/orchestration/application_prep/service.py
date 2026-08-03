"""Background pipeline for company and role preparation snapshots.

Only an explicit first/retry/regenerate action queues this work, so it never incurs
automatic cost. Results persist in ``prep_status`` and ``prep_json`` for instant reuse.
Research observations remain in the snapshot and never enter the global question bank.
"""

import asyncio
import logging

from ...platform.database import now_iso
from ...features.applications import public as applications
from ...features.research.public import (
    ResearchAITaskError,
    ResearchService,
    build_research_snapshot,
)
from ...platform.ai.client import close_llm_client
from ...platform.locale import OutputLocale

PREP_JOB_TIMEOUT_SECONDS = 8 * 60
logger = logging.getLogger(__name__)


def _text(locale: OutputLocale, zh: str, en: str) -> str:
    return en if locale == "en" else zh


class PrepService:
    """Execute the application preparation pipeline."""

    def __init__(self, db_path: str, research_service: ResearchService, llm=None):
        """
        Args:
            llm: Duck-typed model client used for adaptation and research synthesis.
        """
        self._db_path = db_path
        self._research = research_service
        self._llm = llm

    async def close(self) -> None:
        """Release every network resource owned by this background job."""
        await close_llm_client(self._llm)

    async def run(self, user_id: str, application_id: int, *, today: str | None = None,
                  generation: str | None = None, refresh_research: bool = False,
                  output_locale: OutputLocale = "zh-CN") -> dict:
        """Run one owned prep job and release its resources on every exit."""
        try:
            return await self._run_once(
                user_id,
                application_id,
                today=today,
                generation=generation,
                refresh_research=refresh_research,
                output_locale=output_locale,
            )
        finally:
            await self.close()

    async def _run_once(self, user_id: str, application_id: int, *, today: str | None = None,
                        generation: str | None = None,
                        refresh_research: bool = False,
                        output_locale: OutputLocale = "zh-CN") -> dict:
        """Generate both research reports and mark the application ready or failed."""
        detail = applications.application_detail(self._db_path, user_id, application_id)
        if detail is None:
            return {"status": "error", "message": _text(
                output_locale,
                f"找不到岗位 #{application_id}",
                f"Role #{application_id} could not be found.",
            )}
        if not applications.set_prep_status(
                self._db_path, user_id, application_id, "running", generation=generation):
            return {"status": "stale", "message": _text(
                output_locale,
                "调研任务已被更新的请求取代",
                "A newer request replaced this research task.",
            )}
        if generation is not None and not applications.set_research_attempt(
            self._db_path,
            user_id,
            application_id,
            {
                "attempt_state": "running",
                "generation": generation,
                "updated_time": now_iso(),
                "error_code": None,
            },
            generation=generation,
        ):
            return {"status": "stale", "message": _text(
                output_locale,
                "调研任务已被更新的请求取代",
                "A newer request replaced this research task.",
            )}
        try:
            async with asyncio.timeout(PREP_JOB_TIMEOUT_SECONDS):
                result = await self._run_pipeline(
                    user_id, application_id, detail, today=today, generation=generation,
                    refresh_research=refresh_research,
                    output_locale=output_locale,
                )
            if result.get("status") == "stale":
                # A newer generation/profile edit makes this guarded write a no-op.
                # If this task still owns the generation, however, ``stale`` came
                # from company-profile semantic drift (aliases/notes changed while
                # researching).  Close that now-orphaned lease instead of leaving
                # the UI in running until takeover/restart recovery.
                applications.fail_prep_generation(
                    self._db_path,
                    user_id,
                    application_id,
                    _text(
                        output_locale,
                        "调研输入已更新，本次结果未发布；请重新生成",
                        "The research input changed, so this result was not published. Generate it again.",
                    ),
                    generation=generation,
                )
            return result
        except TimeoutError:
            message = _text(
                output_locale,
                "公司调研生成超过 8 分钟，已安全停止；请重试",
                "Company research exceeded eight minutes and was stopped safely. Retry.",
            )
            applications.fail_prep_generation(
                self._db_path, user_id, application_id, message, generation=generation)
            return {"status": "error", "message": message}
        except ResearchAITaskError as error:
            # Composition errors are already safe user-facing copy, such as capacity errors.
            message = str(error)
            applications.fail_prep_generation(
                self._db_path, user_id, application_id, message, generation=generation)
            return {"status": "error", "message": message}
        except Exception:   # noqa: BLE001 - background failures must be visible on the role
            logger.exception("prep generation failed")
            message = _text(
                output_locale,
                "公司调研生成失败，请重试；若持续失败请检查模型与网络设置",
                "Company research failed. Retry, and check model and network settings if the problem continues.",
            )
            applications.fail_prep_generation(
                self._db_path, user_id, application_id, message, generation=generation)
            return {"status": "error", "message": message}

    async def _run_pipeline(self, user_id: str, application_id: int, detail: dict, *,
                            today: str | None, generation: str | None,
                            refresh_research: bool,
                            output_locale: OutputLocale = "zh-CN") -> dict:
        """Run costly stages under one deadline, renewing and verifying ownership."""
        def renew() -> bool:
            return generation is None or applications.touch_prep_generation(
                self._db_path, user_id, application_id, generation)

        def stale() -> dict:
            return {"status": "stale", "message": _text(
                output_locale,
                "调研任务已被更新的请求取代",
                "A newer request replaced this research task.",
            )}

        if not renew():
            return stale()
        research = await self._research.research(
            user_id, detail["company"], detail["position"],
            jd_text=detail.get("jd_text"), department=detail.get("department"),
            refresh=refresh_research, today=today,
            application_id=application_id if generation is not None else None,
            generation=generation,
            output_locale=output_locale)
        if research.get("status") == "stale" or not renew():
            return stale()
        position_report = research.get("position_report")
        company_report = research.get("company_report")
        semantic_claim = research.get("semantic_claim")
        if (
            research.get("status") == "ok"
            and isinstance(company_report, dict)
            and isinstance(position_report, dict)
            and isinstance(semantic_claim, dict)
            and research.get("company_report_generated_time")
            and research.get("position_report_generated_time")
        ):
            snapshot = build_research_snapshot(
                company_report=company_report,
                position_report=position_report,
                semantic_claim=semantic_claim,
                company_report_generated_time=research["company_report_generated_time"],
                position_report_generated_time=research["position_report_generated_time"],
            )
            if generation is None:
                # Ownerless legacy sync calls may complete old artifacts, but authoritative
                # snapshots publish only through the generation-guarded production seam.
                pass
            elif not applications.publish_research_snapshot(
                self._db_path,
                user_id,
                application_id,
                snapshot,
                generation=generation,
                expected_semantic_claim=semantic_claim,
            ):
                return stale()
        elif research.get("status") == "ok" and generation is not None:
            raise RuntimeError("research 返回 ok 但缺少可原子发布的完整双报告或语义元数据")
        elif research.get("status") in {"disabled", "unavailable"}:
            attempt = {
                "attempt_state": research["status"],
                "generation": generation,
                "updated_time": now_iso(),
                "error_code": f"research_{research['status']}",
            }
            if not applications.set_research_attempt(
                self._db_path,
                user_id,
                application_id,
                attempt,
                generation=generation,
            ):
                return stale()

        prep = {
            "research": research["status"],
            "prepared_time": now_iso(),
        }
        if research.get("anchor"):
            prep["anchor"] = research["anchor"]
        if research.get("planner"):
            prep["planner"] = research["planner"]
        if position_report:
            prep["position_report"] = position_report

        # Key-level merge preserves adaptation, snapshots, and extension keys written
        # by other workflows during generation.
        if not applications.merge_localized_prep_artifacts(
            self._db_path,
            user_id,
            application_id,
            output_locale,
            prep,
            generation=generation,
            terminal_status="ready",
        ):
            return stale()
        fresh_detail = applications.application_detail(self._db_path, user_id, application_id)
        stored_prep = (fresh_detail or {}).get("prep")
        localized = stored_prep.get("localized") if isinstance(stored_prep, dict) else None
        stored_entry = localized.get(output_locale) if isinstance(localized, dict) else None
        response_prep = {
            **(stored_prep if isinstance(stored_prep, dict) else {}),
            **(stored_entry if isinstance(stored_entry, dict) else prep),
        }
        return {
            "status": "ok",
            "content_locale": output_locale,
            "prep": response_prep,
        }
