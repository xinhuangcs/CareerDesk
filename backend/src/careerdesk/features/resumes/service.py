"""Resume registration, bounded AI annotation, and version management."""

import unicodedata
from pathlib import Path

from ...platform.storage.uploads import user_upload_root
from ..applications import public as applications
from . import repository as resumes
from . import ai_tasks
from .ai_models import (
    ResumeParse,
)
from .policy import (
    MAX_RESUME_SOURCE_LINE_CHARS,
    MAX_RESUME_SOURCE_SEGMENTS,
    validate_resume_text,
)

DUPLICATE_NAME_MESSAGE = "同名已存在，请改版本名或使用更新"
STALE_UPDATE_MESSAGE = "简历在解析期间已更新或归档，请刷新后重试"


def _source_lines(content_text: str) -> list[str]:
    """Freeze original text into stable, non-empty, bounded segments.

    A PDF text layer can collapse a page into one physical line. Deterministic
    splitting prefers sentence punctuation or whitespace in the latter half and
    uses the hard limit only without a natural boundary. Full ``content_text``
    remains unchanged, and the model returns indices rather than rewritten text.
    """

    def unicode_safe_cut(text: str, proposed: int) -> int:
        """Keep ordinary combining/emoji clusters on one side of a hard cut.

        This deliberately remains a small, dependency-free boundary guard, not
        a full Unicode grapheme segmenter.  If one hostile cluster itself is
        longer than the per-item hard limit, retaining the bound wins and the
        original proposed cut is used.
        """

        def extends_previous(character: str) -> bool:
            codepoint = ord(character)
            return (
                unicodedata.category(character).startswith("M")
                or 0x1F3FB <= codepoint <= 0x1F3FF  # emoji skin-tone modifier
                or 0xE0020 <= codepoint <= 0xE007F  # emoji tag sequence
            )

        def is_regional_indicator(character: str) -> bool:
            return 0x1F1E6 <= ord(character) <= 0x1F1FF

        cut = proposed
        while 0 < cut < len(text):
            following = text[cut]
            previous = text[cut - 1]
            if extends_previous(following) or following == "\u200d" or previous == "\u200d":
                cut -= 1
                continue
            if is_regional_indicator(previous) and is_regional_indicator(following):
                preceding_run = 0
                cursor = cut - 1
                while cursor >= 0 and is_regional_indicator(text[cursor]):
                    preceding_run += 1
                    cursor -= 1
                # GB12/GB13: regional indicators form pairs from the start of a run.
                if preceding_run % 2 == 1:
                    cut -= 1
                    continue
            break
        return cut if cut > 0 else proposed

    def split_long_line(raw_line: str) -> list[str]:
        remaining = raw_line.strip()
        segments: list[str] = []
        strong_boundaries = "。！？；.!?;"
        while len(remaining) > MAX_RESUME_SOURCE_LINE_CHARS:
            window = remaining[:MAX_RESUME_SOURCE_LINE_CHARS]
            search_start = MAX_RESUME_SOURCE_LINE_CHARS // 2
            cut = 0
            for marker in strong_boundaries:
                position = window.rfind(marker, search_start)
                if position >= 0:
                    cut = max(cut, position + 1)
            if cut == 0:
                for position in range(len(window) - 1, search_start - 1, -1):
                    if window[position].isspace():
                        cut = position
                        break
            if cut == 0:
                cut = MAX_RESUME_SOURCE_LINE_CHARS
            cut = unicode_safe_cut(remaining, cut)
            segment = remaining[:cut].strip()
            if segment:
                segments.append(segment)
            remaining = remaining[cut:].lstrip()
        if remaining:
            segments.append(remaining)
        return segments

    lines: list[str] = []
    for raw_line in content_text.splitlines():
        if raw_line.strip():
            segments = split_long_line(raw_line)
            if len(lines) + len(segments) > MAX_RESUME_SOURCE_SEGMENTS:
                raise ValueError(
                    "简历可见段落过多（最多 "
                    f"{MAX_RESUME_SOURCE_SEGMENTS:,} 段）；"
                    "请移除异常空行、逐字换行或重复内容后重试"
                )
            lines.extend(segments)
    return lines


def _materialize_resume_lines(
    parsed: ResumeParse,
    source_lines: list[str],
) -> list[dict]:
    """Persist every trusted segment and add bounded labels to matched items.

    ``ResumeParse.lines`` is a bounded classification result, not a full-text
    allowlist. Unselected segments remain in order with empty knowledge points.
    """
    annotations: dict[int, dict] = {}
    for line in parsed.lines:
        if line.line_index >= len(source_lines):
            raise ai_tasks.ResumeAITaskError("模型未能可靠对齐简历原文，请重试")
        annotations[line.line_index] = {
            "knowledge_points": list(line.knowledge_points),
        }
    return [
        {
            "text": text,
            **annotations.get(line_index, {
                "knowledge_points": [],
            }),
        }
        for line_index, text in enumerate(source_lines)
    ]


class ResumeService:
    """Resume registration orchestration entry point."""

    def __init__(self, db_path: str, llm):
        """Initialize with the business database and duck-typed AI client."""
        self._db_path = db_path
        self._llm = llm

    async def register(self, user_id: str, name: str, content_text: str, *,
                       binding: str = "family", application_id: int | None = None,
                       family: str | None = None, file_path: str | None = None,
                       replace_existing: bool = False,
                       expected_update: resumes.ResumeUpdateSnapshot | None = None) -> dict:
        """Register a resume and bind role-specific versions to an application.

        An explicit ``family`` overrides automatic classification.
        ``replace_existing`` is true only for an explicit version update.
        """
        try:
            content_text = validate_resume_text(content_text)
            source_lines = _source_lines(content_text)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        if expected_update is not None and not replace_existing:
            return {"status": "error", "message": "更新快照只能用于显式替换"}
        if binding not in ("family", "application"):
            return {"status": "error", "message": "binding 只能是 family 或 application"}
        if binding == "application":
            if application_id is None:
                return {"status": "error", "message": "binding=application 时必须传 application_id"}
            if applications.application_detail(self._db_path, user_id, application_id) is None:
                return {"status": "error", "message": f"找不到岗位 #{application_id}（或无权访问）"}
        elif application_id is not None:
            return {"status": "error", "message": "binding=family 时不能传 application_id"}

        # Reject before AI parsing to avoid an invalid paid call. Archived versions
        # still reserve names; users must update explicitly or choose a new name.
        if replace_existing:
            update_snapshot = expected_update or resumes.get_resume_update_snapshot_by_name(
                self._db_path,
                user_id,
                name,
            )
            if update_snapshot is None:
                return {"status": "error", "message": f"找不到简历版本「{name}」"}
            if (
                update_snapshot.name != name
                or update_snapshot.archived
                or not resumes.resume_update_snapshot_matches(
                    self._db_path,
                    user_id,
                    update_snapshot,
                )
            ):
                return {"status": "stale", "message": STALE_UPDATE_MESSAGE}
        else:
            update_snapshot = None
            if resumes.resume_name_exists(self._db_path, user_id, name):
                return {"status": "error", "message": DUPLICATE_NAME_MESSAGE}

        indexed_lines = [
            {"line_index": line_index, "text": text}
            for line_index, text in enumerate(source_lines)
        ]
        parse = None
        parsed_lines = None
        if self._llm is not None:
            try:
                parse = await ai_tasks.parse_resume(
                    self._llm,
                    source_lines=indexed_lines,
                )
                parsed_lines = _materialize_resume_lines(parse, source_lines)
            except ai_tasks.ResumeAITaskError:
                # Technical annotations are optional derivatives; their failure
                # must not roll back deterministically extracted full text.
                parsed_lines = None
        resolved_family = family or (parse.family if parse is not None else None)
        try:
            upserted = resumes.upsert_resume(
                self._db_path, user_id, name, content_text, family=resolved_family, binding=binding,
                application_id=application_id, lines=parsed_lines,
                file_path=file_path, overwrite_existing=replace_existing,
                return_previous_file=file_path is not None,
                expected_update=update_snapshot)
        except ValueError as error:
            # Another request can delete the role after preflight; transaction-time
            # validation closes that race.
            return {"status": "error", "message": str(error)}
        # Close the race where another request inserts the same name after preflight;
        # never turn that conflict into an overwrite.
        if file_path is not None:
            resume_id, previous_file = upserted
        else:
            resume_id, previous_file = upserted, None
        if resume_id is None:
            if replace_existing:
                return {"status": "stale", "message": STALE_UPDATE_MESSAGE}
            return {"status": "error", "message": DUPLICATE_NAME_MESSAGE}
        # After replacement commits, delete the old copy in the managed directory.
        # Only the full-snapshot CAS winner receives the old path; losing requests
        # clean their own uploads in the API finally block.
        cleanup_warning = None
        if previous_file and previous_file != file_path:
            old_candidate = Path(previous_file).expanduser()
            new_candidate = Path(file_path).expanduser()
            try:
                managed_root = user_upload_root(Path(self._db_path).parent, "resumes", user_id)
                # Resolve only for boundary comparison. Unlink the original path
                # without following its final symlink.
                if (not old_candidate.is_symlink() and not new_candidate.is_symlink()
                        and old_candidate.resolve().parent == managed_root
                        and new_candidate.resolve().parent == managed_root):
                    old_candidate.unlink(missing_ok=True)
            except (OSError, ValueError) as error:
                # The database already points to the new file. Old-copy cleanup is
                # best effort and must never remove the replacement.
                cleanup_warning = f"简历已更新，但旧文件清理失败：{error}"
        return {
            "status": "ok",
            "resume_id": resume_id,
            "family": resolved_family,
            "line_count": len(parsed_lines or []),
            **({"cleanup_warning": cleanup_warning} if cleanup_warning else {}),
        }
