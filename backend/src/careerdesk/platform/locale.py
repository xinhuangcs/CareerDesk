"""Bounded locale contract shared by user-facing generation workflows."""

from typing import Literal

OutputLocale = Literal["zh-CN", "en"]
DEFAULT_OUTPUT_LOCALE: OutputLocale = "zh-CN"


def output_language_name(locale: OutputLocale) -> str:
    """Return the native language name used in model instructions."""
    return "Simplified Chinese" if locale == "zh-CN" else "English"
