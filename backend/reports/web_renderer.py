"""Web Report SSR（M1 step 5，Jinja2）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from reports.degradation import degradation_notes

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

CLAIM_TAG = {"fact": "事實", "calculation": "計算", "inference": "推論", "limitation": "限制"}

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)
_env.globals["tag_label"] = lambda t: CLAIM_TAG.get(t, t)


def render_report_html(report: dict[str, Any]) -> str:
    # 降級提示在 Python 端算好再丟進模板：判斷邏輯與 markdown / Discord 共用同一份
    # （reports.degradation），避免在 Jinja 裡重寫一次條件後三個面漂移。
    return _env.get_template("report.html").render(
        report=report,
        degradation_notes=degradation_notes(report.get("cost")),
    )


def render_history_html(
    reports: list[dict[str, Any]],
    cost: dict[str, Any] | None = None,
    activity: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
) -> str:
    return _env.get_template("history.html").render(
        reports=reports, cost=cost, activity=activity,
        calibration=calibration, evaluation=evaluation, history=history,
    )
