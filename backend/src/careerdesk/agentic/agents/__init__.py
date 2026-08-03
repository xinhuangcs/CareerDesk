"""Business agent definitions and assembly; one interactive assistant."""

from .career_assistant import build_career_assistant

__all__ = ["build_career_assistant"]
# High-risk tools belong only to the primary assistant and require human confirmation.
