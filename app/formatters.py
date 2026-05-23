from __future__ import annotations

from typing import Any


AUTH_REQUIRED_MARKERS = (
    "access token",
    "authentication",
    "authenticate",
    "authorization",
    "login",
    "travog_access_token",
)


def format_channel_response(
    *,
    message: str,
    artifacts: list[dict[str, Any]],
    result_limit: int,
    public_app_url: str,
) -> str:
    if _looks_auth_related(message):
        return (
            f"{message.strip()}\n\n"
            f"Please connect your Travog account here: {public_app_url.rstrip('/')}"
        )

    parts = [message.strip()] if message.strip() else []
    for artifact in artifacts:
        text = format_artifact_summary(artifact, result_limit=result_limit)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip() or "I could not produce a response for that message."


def format_artifact_summary(artifact: dict[str, Any], *, result_limit: int) -> str:
    summary = artifact.get("summary")
    if not isinstance(summary, dict) or not summary:
        return ""

    rows = _extract_rows(summary)
    if rows:
        formatted = [_format_row(row, index) for index, row in enumerate(rows[:result_limit], start=1)]
        return "\n".join(item for item in formatted if item)

    fields = [
        f"{_humanize_key(key)}: {value}"
        for key, value in summary.items()
        if isinstance(value, str | int | float | bool)
    ]
    return "\n".join(fields[:result_limit])


def _extract_rows(summary: dict[str, Any]) -> list[Any]:
    for key in ("results", "offers", "items", "flights", "bookings", "expenses"):
        value = summary.get(key)
        if isinstance(value, list):
            return value
    for value in summary.values():
        if isinstance(value, list):
            return value
    return []


def _format_row(row: Any, index: int) -> str:
    if isinstance(row, dict):
        parts = [
            str(value)
            for key, value in row.items()
            if key not in {"raw", "payload"} and isinstance(value, str | int | float | bool)
        ]
        return f"{index}. " + " | ".join(parts[:6])
    return f"{index}. {row}"


def _humanize_key(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _looks_auth_related(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in AUTH_REQUIRED_MARKERS)
