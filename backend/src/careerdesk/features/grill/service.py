"""Question-set-only Grill state machine with one bounded follow-up."""

import asyncio
from collections.abc import Callable
from uuid import uuid4

from ...core.config import local_today
from ...platform.ai.client import close_llm_client
from . import repository
from .ai_tasks import GrillAITaskError, judge_answer

_ACK = "记下了。下一题："
_MODEL_REQUIRED = "练习判卷需要模型，请先到「模型与隐私」完成配置"


class GrillService:
    def __init__(self, db_path: str, llm, *, llm_configured: bool | None = None,
                 llm_factory: Callable[[], object] | None = None):
        self._db_path = db_path
        self._llm = llm
        self._llm_factory = llm_factory
        self._llm_configured = (llm is not None or llm_factory is not None
                                if llm_configured is None else llm_configured)
        self._pending_answer: dict | None = None

    def _load_llm(self):
        if self._llm is None and self._llm_factory is not None:
            self._llm = self._llm_factory()
        return self._llm

    async def close(self) -> None:
        await close_llm_client(self._llm)

    def start(self, user_id: str, *, question_set_id: int, question_count: int = 5) -> dict:
        try:
            session_id, question, total = repository.create_session(
                self._db_path, user_id, question_set_id=question_set_id,
                question_count=question_count,
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return {"status": "ok", "session_id": session_id, "question": question,
                "progress": {"answered": 0, "total": total}}

    async def answer(self, user_id: str, session_id: int, text: str, *,
                     session_item_id: int, answering_follow_up: bool = False,
                     today: str | None = None, enqueue_only: bool = False) -> dict:
        today = today or local_today().isoformat()
        session = repository.get_session(self._db_path, user_id, session_id)
        if session is None or session["state"] != "active":
            return self._sync(user_id, session_id)
        item = repository.current_item(self._db_path, user_id, session_id)
        if item is None or item["id"] != session_item_id:
            return self._sync(user_id, session_id)
        transcript = list(session["plan"].get("transcript", []))
        if item["follow_up_count"] and not answering_follow_up:
            prior_answer = next((part.get("answer") for part in reversed(transcript) if "answer" in part), None)
            prior_follow_up = next((part.get("follow_up") for part in reversed(transcript) if "follow_up" in part), None)
            if prior_answer == text and prior_follow_up:
                return {"status": "ok", "follow_up": prior_follow_up,
                        "progress": self._progress(session["plan"])}
        token = uuid4().hex
        claim = repository.claim_current_item(
            self._db_path, user_id, session_id, session_item_id, token,
        )
        if claim["status"] == "busy":
            return {"status": "error", "code": "submission_in_progress", "message": "这题正在处理"}
        if claim["status"] != "claimed":
            return self._sync(user_id, session_id)
        output_locale = session["plan"].get("content_locale", "zh-CN")
        if output_locale not in {"zh-CN", "en"}:
            output_locale = "zh-CN"
        guide = item.get("answer_guide") or {}
        if isinstance(guide, dict) and guide.get("kind") == "self_review":
            feedback = ({
                "strengths": [],
                "gaps": ["This question lacks a verified evaluation contract, so the answer was saved for self-review only."],
                "next_step": "Add or verify an answer guide before requesting model evaluation.",
            } if output_locale == "en" else {
                "strengths": [],
                "gaps": ["该题缺少经核验的评价契约，本次仅保存回答供自我复盘。"],
                "next_step": "补充或核验回答指南后再进行模型判卷。",
            })
            if not repository.record_answer(
                self._db_path, user_id, session_id, session_item_id, token,
                transcript=[{"question": item["text"]}, {"answer": text}],
                verdict="ungradable", stuck=False, feedback=feedback, today=today,
            ):
                return self._sync(user_id, session_id)
            return self._advance(user_id, session_id)
        llm = self._load_llm()
        if llm is None:
            repository.release_claim(self._db_path, user_id, session_item_id, token)
            return {"status": "error", "code": "model_required", "message": _MODEL_REQUIRED}
        pending = {"llm": llm, "item": item, "transcript": transcript, "text": text,
                   "user_id": user_id, "session_id": session_id,
                   "session_item_id": session_item_id, "token": token, "today": today,
                   "session_plan": session["plan"], "output_locale": output_locale}
        if enqueue_only:
            self._pending_answer = pending
            return {"status": "processing", "session_id": session_id,
                    "progress": self._progress(session["plan"])}
        return await self._complete_answer(**pending)

    async def run_pending_answer(self) -> dict:
        if self._pending_answer is None:
            return {"status": "error", "code": "no_pending_answer"}
        pending, self._pending_answer = self._pending_answer, None
        return await self._complete_answer(**pending)

    async def _complete_answer(self, *, llm, item: dict, transcript: list, text: str,
                               user_id: str, session_id: int, session_item_id: int,
                               token: str, today: str, session_plan: dict,
                               output_locale: str) -> dict:
        try:
            verdict = await judge_answer(llm, item={
                "text": item["text"], "category": item["category"], "channel": item["channel"],
                "response_format": item["response_format"], "evaluation_kind": item["evaluation_kind"],
                "rubric": item["rubric"], "answer_authority": item["answer_authority"],
                "answer_guide": item["answer_guide"], "evidence": item["evidence"],
            }, transcript=transcript, answer_text=text, output_locale=output_locale)
        except GrillAITaskError as exc:
            repository.fail_claim(
                self._db_path, user_id, session_item_id, token, str(exc),
            )
            return {"status": "error", "code": "judge_failed", "message": str(exc)}
        except asyncio.CancelledError:
            repository.fail_claim(
                self._db_path, user_id, session_item_id, token, "outcome_unknown",
            )
            raise
        except Exception:
            repository.fail_claim(
                self._db_path, user_id, session_item_id, token, "unexpected_judge_error",
            )
            return {"status": "error", "code": "judge_failed", "message": "判卷失败，请重试"}
        if (verdict.follow_up and item["follow_up_allowed"] and item["channel"] == "interview"
                and item["follow_up_count"] == 0):
            transcript += [{"answer": text}, {"follow_up": verdict.follow_up}]
            if not repository.save_follow_up(
                self._db_path, user_id, session_id, session_item_id, token,
                transcript=transcript,
            ):
                return self._sync(user_id, session_id)
            return {"status": "ok", "follow_up": verdict.follow_up,
                    "progress": self._progress(session_plan)}
        feedback = {"strengths": verdict.strengths, "gaps": verdict.gaps,
                    "next_step": verdict.next_step}
        transcript += [{"answer": text}]
        if not repository.record_answer(
            self._db_path, user_id, session_id, session_item_id, token,
            transcript=[{"question": item["text"]}, *transcript], verdict=verdict.verdict,
            stuck=verdict.stuck, feedback=feedback, today=today,
        ):
            return self._sync(user_id, session_id)
        return self._advance(user_id, session_id)

    def skip(self, user_id: str, session_id: int, *, session_item_id: int) -> dict:
        token = uuid4().hex
        claim = repository.claim_current_item(
            self._db_path, user_id, session_id, session_item_id, token,
        )
        if claim["status"] == "busy":
            return {"status": "error", "code": "submission_in_progress", "message": "这题正在处理"}
        if claim["status"] != "claimed":
            return self._sync(user_id, session_id)
        if not repository.record_skip(self._db_path, user_id, session_id, session_item_id, token):
            return self._sync(user_id, session_id)
        return self._advance(user_id, session_id)

    def suspend(self, user_id: str, session_id: int) -> dict:
        if not repository.set_state(self._db_path, user_id, session_id, "active", "suspended"):
            return {"status": "error", "message": "场次不存在或不在进行中"}
        return {"status": "suspended", "session_id": session_id}

    def resume(self, user_id: str, session_id: int) -> dict:
        current = repository.get_session(self._db_path, user_id, session_id)
        if current is not None and current["state"] == "active":
            return self._sync(user_id, session_id)
        try:
            changed = repository.set_state(self._db_path, user_id, session_id, "suspended", "active")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if not changed:
            return {"status": "error", "message": "场次不存在或未挂起"}
        return self._sync(user_id, session_id)

    async def summary(self, user_id: str, session_id: int) -> dict:
        return repository.replay(self._db_path, user_id, session_id) or {
            "status": "error", "message": "场次尚未完成或不存在",
        }

    async def finalize_summary(self, user_id: str, session_id: int) -> dict:
        return await self.summary(user_id, session_id)

    def _advance(self, user_id: str, session_id: int) -> dict:
        summary = repository.finish_if_complete(self._db_path, user_id, session_id)
        if summary is not None:
            return {"status": "finished", "session_id": session_id, "summary": summary}
        return self._sync(user_id, session_id, ack=True)

    def _sync(self, user_id: str, session_id: int, *, ack: bool = False) -> dict:
        session = repository.get_session(self._db_path, user_id, session_id)
        if session is None:
            return {"status": "error", "message": "场次不存在"}
        if session["state"] == "finished":
            return {"status": "finished", "session_id": session_id, "summary": session["summary"] or {}}
        if session["state"] == "suspended":
            return {"status": "suspended", "session_id": session_id}
        item = repository.current_item(self._db_path, user_id, session_id)
        if item is None:
            return self._advance(user_id, session_id)
        if item["claim_token"]:
            return {"status": "processing", "session_id": session_id,
                    "progress": self._progress(session["plan"])}
        if item.get("claim_error_code"):
            code = item["claim_error_code"]
            known = {
                "当前模型容量不足，无法安全判卷": "当前模型容量不足，无法安全判卷",
                "判卷超时，请重试": "判卷超时，请重试",
                "模型服务请求失败": "模型服务请求失败",
                "模型未能生成合规评价": "模型未能生成合规评价",
            }
            return {"status": "error", "session_id": session_id, "code": "judge_failed",
                    "message": known.get(code, "判卷失败，请重试")}
        if item["follow_up_count"]:
            follow_up = next((part.get("follow_up") for part in reversed(session["plan"].get("transcript", []))
                              if "follow_up" in part), None)
            if follow_up:
                return {"status": "ok", "follow_up": follow_up,
                        "progress": self._progress(session["plan"])}
        public = {key: item[key] for key in (
            "id", "text", "category", "channel", "response_format", "difficulty",
            "primary_competency", "secondary_tags",
        )}
        result = {"status": "ok", "session_id": session_id, "question": public,
                  "progress": self._progress(session["plan"])}
        if ack:
            result["ack"] = _ACK
        return result

    @staticmethod
    def _progress(plan: dict) -> dict:
        return {"answered": int(plan.get("current_ordinal", 0)), "total": int(plan.get("total", 0))}
