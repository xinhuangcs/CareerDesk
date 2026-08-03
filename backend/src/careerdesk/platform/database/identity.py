"""Stable natural-key normalization shared by Python and SQLite schema expressions."""

from __future__ import annotations


# Python 3.12's complete Unicode ``str.isspace`` set.  Keep the explicit list so
# the persisted SQLite key does not silently change when the runtime is upgraded.
_IDENTITY_WHITESPACE_CODEPOINTS = (
    9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760,
    8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
    8232, 8233, 8239, 8287, 12288,
)
_IDENTITY_WHITESPACE = frozenset(chr(value) for value in _IDENTITY_WHITESPACE_CODEPOINTS)


def normalize_application_identity_part(value: str | None) -> str | None:
    """Remove every persisted identity whitespace code point, preserving display text."""
    if value is None:
        return None
    return "".join(character for character in value if character not in _IDENTITY_WHITESPACE)


def application_identity_sql(column: str) -> str:
    """Return the SQLite generated-column expression equivalent to the Python helper."""
    if column not in {"name", "company", "position"}:
        raise ValueError("unsupported application identity column")
    expression = column
    for codepoint in _IDENTITY_WHITESPACE_CODEPOINTS:
        expression = f"replace({expression}, char({codepoint}), '')"
    return expression


def application_identity_key(company: str, position: str) -> tuple[str, str]:
    """Canonical key for one application; both parts must remain non-empty."""
    company_key = normalize_application_identity_part(company)
    position_key = normalize_application_identity_part(position)
    if not company_key or not position_key:
        raise ValueError("公司名和岗位名去除空白后不能为空")
    return company_key, position_key
