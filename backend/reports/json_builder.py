"""JSON report builder（M1 step 4）。

從 AnalysisResult + metadata 組出 JSON-safe dict，給 web / 下載 / guardrail（M5）用。
保留每個 claim 的 type 與 source_ref，讓下游可驗證來源。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import AnalysisResult


def build_report_dict(
    result: AnalysisResult,
    *,
    report_type: str = "每日跨市場晨報",
    raw_query: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    ts = generated_at or datetime.now(ZoneInfo(settings.tz))
    payload = result.model_dump(mode="json")  # data_as_of/date → ISO 字串
    payload.update(
        {
            "report_type": report_type,
            "raw_query": raw_query,
            "generated_at": ts.isoformat(),
        }
    )
    return payload


def build_report_json(result: AnalysisResult, **kwargs: Any) -> str:
    return json.dumps(build_report_dict(result, **kwargs), ensure_ascii=False, indent=2)
