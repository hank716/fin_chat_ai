"""Web Report SSR（M1 step 5，Jinja2）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

CLAIM_TAG = {"fact": "事實", "calculation": "計算", "inference": "推論", "limitation": "限制"}

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)
_env.globals["tag_label"] = lambda t: CLAIM_TAG.get(t, t)


def render_report_html(report: dict[str, Any]) -> str:
    return _env.get_template("report.html").render(report=report)
