"""供應商中立的 LLM 例外階層（spec 022-llm-tiering）。

系統有兩個 LLM 供應商（Gemini 廣度召回 / Claude 決策查證），但上層（`api/brief.py`、
`api/ask.py`）只在乎「這個錯誤該不該重試、該不該降級、要不要回 503」，不該為了多一個
供應商就把每個呼叫點都改成 `except (GeminiError, AnthropicError)`。

因此把語意抽出來放這裡，`gemini_client.GeminiError` 家族改為繼承之——既有的
`except GeminiError` 呼叫點行為完全不變，新的決策層則丟同一組中立例外。
"""
from __future__ import annotations


class LLMError(RuntimeError):
    """LLM 呼叫失敗的共同基底。"""


class LLMUnavailable(LLMError):
    """暫時性過載（Gemini 503 / Anthropic 5xx、連線錯誤）→ 值得 retry。"""


class LLMQuotaExceeded(LLMError):
    """配額用盡（429）→ 短時間內不會恢復，fail-fast 不 retry。"""


class LLMBadRequest(LLMError):
    """請求本身被拒（400）→ 不 retry，由呼叫端降級。"""


class LLMRefused(LLMError):
    """模型以 `stop_reason="refusal"` 婉拒（安全分類器）。

    這是**成功的 HTTP 200**，不是錯誤回應——但對呼叫端而言等同「這條路走不通」，
    故包成例外走同一套降級路徑。財經內容屬良性，會踩到多半是 cyber/bio 分類器誤判。
    """
