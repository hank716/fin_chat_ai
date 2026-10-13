"""處置股與變更交易（全額交割）名單（spec 024 FR-007）。

小型股候選是低流動性標的，被處置（分盤撮合、預收款券）或列為變更交易的個股實際上很難正常買賣，
不該出現在名單裡。資料取自交易所公開資料，每次呼叫 3 個請求、不需金鑰。

涵蓋：上市處置（TWSE punish）、上櫃處置（TPEx disposal）、上市變更交易（TWSE TWT85U）。
**未涵蓋**：上櫃變更交易——尚未找到可用的公開端點（已知缺口）。
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("ai-market-backend.trading_restrictions")

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# (名稱, URL, 代號欄位)
_SOURCES = (
    ("twse_disposition", "https://openapi.twse.com.tw/v1/announcement/punish", "Code"),
    ("tpex_disposition", "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
     "SecuritiesCompanyCode"),
    ("twse_altered_trading", "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U", "Code"),
)


def restricted_symbols() -> set[str] | None:
    """目前被處置或列為變更交易的代號集合。

    **任一來源失敗就回 None**（而不是回部分結果）：呼叫端據此停列小型股候選。
    拿一份不完整的名單去過濾，等於放行了沒查到的那一半——寧可當天不列。
    """
    out: set[str] = set()
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            for name, url, key in _SOURCES:
                resp = client.get(url)
                resp.raise_for_status()
                rows = resp.json()
                if not isinstance(rows, list):
                    raise ValueError(f"{name} 回應不是清單")
                out.update(str(r.get(key) or "").strip() for r in rows if isinstance(r, dict))
    except Exception as exc:  # noqa: BLE001 — 任何失敗都視為「名單不可用」
        logger.warning("交易限制名單取得失敗（本次不列小型股候選）: %s", exc)
        return None
    out.discard("")
    return out
