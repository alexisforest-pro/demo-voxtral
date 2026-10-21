"""Bounded report contract shared by the worker and backend; no inference imports."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Label = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Description = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
Evidence = Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class KPI(_StrictModel):
    name: Label
    value: Label | None = None
    unit: Label | None = None
    timeframe: Label | None = None
    context: Description | None = None
    evidence: Evidence


class Announcement(_StrictModel):
    description: Description
    evidence: Evidence


class Action(_StrictModel):
    action: Description
    owner: Label | None = None
    deadline: Label | None = None
    evidence: Evidence


class Date(_StrictModel):
    date: Label
    context: Description | None = None
    evidence: Evidence

    @field_validator("date")
    @classmethod
    def reject_financial_amount(cls, value: str) -> str:
        if re.search(
            r"[€$£%]|\b(?:euros?|EUR|USD|GBP|dollars?|pounds?|percent|percentage)\b",
            value,
            re.IGNORECASE,
        ):
            raise ValueError(
                "Dates must be temporal expressions, not financial amounts or percentages."
            )
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+(?:[,.]\d+)*", stripped) and not re.fullmatch(
            r"(?:19|20|21)\d{2}", stripped
        ):
            raise ValueError(
                "A standalone count or amount is not a date; include a temporal expression."
            )
        return value


class IntelligenceReport(_StrictModel):
    title: Label | None = None
    summary: (
        Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")] | None
    ) = None
    topics: list[Label] = Field(default_factory=list, max_length=50)
    speakers: list[Label] = Field(default_factory=list, max_length=50)
    organisations: list[Label] = Field(default_factory=list, max_length=50)
    policy_references: list[Label] = Field(default_factory=list, max_length=50)
    kpis: list[KPI] = Field(default_factory=list, max_length=100)
    announcements: list[Announcement] = Field(default_factory=list, max_length=100)
    actions: list[Action] = Field(default_factory=list, max_length=100)
    dates: list[Date] = Field(default_factory=list, max_length=100)


def intelligence_response_format() -> dict:
    """Require every field, including nullable ones, in newly generated reports."""
    schema = IntelligenceReport.model_json_schema()

    def require_fields(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                require_fields(value)
        elif isinstance(node, list):
            for value in node:
                require_fields(value)

    require_fields(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "IntelligenceReport", "schema": schema, "strict": True},
    }


class ReportQualityError(ValueError):
    """Safe field-level correction feedback, never raw model or transcript text."""


def validate_model_report(result: dict, text: str) -> dict:
    """Validate new inference separately from backward-compatible saved reports."""
    if not isinstance(result, dict) or set(result) != set(
        IntelligenceReport.model_fields
    ):
        raise ReportQualityError("Include every report field; do not omit sections.")
    for name, model in (
        ("kpis", KPI),
        ("actions", Action),
        ("announcements", Announcement),
        ("dates", Date),
    ):
        entries = result.get(name)
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != set(model.model_fields):
                    raise ReportQualityError(
                        f"Include every field in each {name} entry, using null where missing."
                    )
    try:
        report = validate_report(result, text)
    except ValueError:
        raise ReportQualityError(
            "Return the required field types and bounds; dates cannot be currency amounts or percentages."
        ) from None
    if text.strip():
        if not report["title"] or not report["summary"] or not report["topics"]:
            raise ReportQualityError(
                "For nonempty speech provide a descriptive title, summary and discussed topics."
            )
        if (
            re.search(
                r"\b(?:my name is|je m[’']appelle|ich heiße|me llamo|mi chiamo)\s+[^\W\d_]",
                text,
                re.IGNORECASE,
            )
            and not report["speakers"]
        ):
            raise ReportQualityError(
                "Review the explicit speaker self-introduction and populate speakers with the stated name."
            )
        amount = (
            r"(?:[€$£]\s*\d|\d[\d,.\s]*\s*(?:euros?|dollars?|pounds?|EUR|USD|GBP)\b)"
        )
        if re.search(amount, text, re.IGNORECASE) and not report["kpis"]:
            raise ReportQualityError(
                "Review the stated monetary figure and include it as a KPI with units and evidence, not a date."
            )
    return report


def validate_report(result: dict, text: str) -> dict:
    """Reject invalid shapes/bounds, then discard items without an exact source quote.

    Matching collapses whitespace only: case, punctuation and source language remain
    significant. Quote presence is not semantic fact verification. Title, summary
    and label lists are context-derived, not evidence-verified.
    """
    report = IntelligenceReport.model_validate(result).model_dump(mode="json")
    source = " ".join(text.split())
    for field in ("kpis", "announcements", "actions", "dates"):
        report[field] = [
            item
            for item in report[field]
            if (quote := " ".join(item["evidence"].split())) and quote in source
        ]
    return report
