"""廣度召回層 facts pack（spec 022 WP2）。

重點只有一個：**查證不了的線索不該進決策層**。無來源的 event 一律丟棄，
有 groundingMetadata 就把 URL 補回去——漏掉 URL 等於決策層沒辦法 web_fetch 查證那條事實。
"""
from __future__ import annotations

import pytest

from ai import retrieval


def _chunks(*uris):
    return {"groundingMetadata": {"groundingChunks": [{"web": {"uri": u}} for u in uris]}}


def test_all_grounding_urls_is_not_truncated():
    """gemini_client._grounding_sources 的呼叫端只取前 4 筆（給 Discord 附連結用）；
    查證需要完整清單，第 5 筆之後不能掉。"""
    cand = _chunks(*[f"https://e{i}.test" for i in range(9)])
    assert len(retrieval._all_grounding_urls(cand)) == 9


def test_all_grounding_urls_dedupes():
    cand = _chunks("https://a.test", "https://a.test", "https://b.test")
    assert retrieval._all_grounding_urls(cand) == ["https://a.test", "https://b.test"]


def test_grounding_redirect_is_resolved_to_real_url(monkeypatch):
    """轉址網址必須換成真正的文章網址，否則決策層的 web_fetch 一律回 url_not_allowed。

    2026-08-07 那篇 7 條 fact_checks **全部 unverifiable**、note 幾乎都寫著 url_not_allowed
    ——查證層從上線第一天起就結構性失效：每一次 fetch 都注定失敗，卻照樣佔用 max_uses 額度。
    """
    calls: list[str] = []

    def _fake_resolve(url: str) -> str:
        calls.append(url)
        return "https://news.example.com/real-article" if "grounding-api-redirect" in url else url

    monkeypatch.setattr(retrieval, "_resolve_redirect", _fake_resolve)
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ"
    events = [
        {"claim": "A", "date": "2026-08-05", "source": "鉅亨", "url": redirect},
        {"claim": "B", "date": "2026-08-05", "source": "鉅亨", "url": redirect},
    ]
    out = retrieval._attach_urls(events, [])
    assert [e.url for e in out] == ["https://news.example.com/real-article"] * 2
    # 多則線索常共用同一來源；同一個轉址只該解析一次
    assert calls == [redirect]


def test_resolve_redirect_passes_through_normal_urls():
    """非 grounding 轉址不碰（不發任何網路請求）。"""
    url = "https://www.cnyes.com/news/id/1234567"
    assert retrieval._resolve_redirect(url) == url


def test_resolve_redirect_keeps_original_on_failure(monkeypatch):
    """解不開就沿用原網址——決策層仍會誠實標成 unverifiable，不會假裝查過。"""
    class _Boom:
        def __init__(self, *a, **k): pass
        def __enter__(self): raise RuntimeError("network down")
        def __exit__(self, *exc): return False

    import httpx
    monkeypatch.setattr(httpx, "Client", _Boom)
    url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/XYZ"
    assert retrieval._resolve_redirect(url) == url


def test_event_without_any_source_is_dropped():
    """模型沒填 url、grounding 也補不到 → 丟棄。查證不了的東西不進決策層。"""
    events = [{"claim": "無來源傳聞", "date": "2026-08-05", "source": "不明"}]
    assert retrieval._attach_urls(events, []) == []


def test_url_backfilled_from_grounding_in_order():
    """Gemini 常在 groundingMetadata 有來源、卻沒把 url 寫進 JSON 欄位。"""
    events = [
        {"claim": "A", "date": "2026-08-05", "source": "鉅亨"},
        {"claim": "B", "date": "2026-08-05", "source": "經濟日報"},
    ]
    out = retrieval._attach_urls(events, ["https://a.test", "https://b.test"])
    assert [e.url for e in out] == ["https://a.test", "https://b.test"]


def test_model_supplied_url_wins_over_grounding():
    events = [{"claim": "A", "date": "2026-08-05", "source": "鉅亨",
               "url": "https://explicit.test"}]
    out = retrieval._attach_urls(events, ["https://fallback.test"])
    assert out[0].url == "https://explicit.test"


def test_incomplete_event_is_dropped():
    """缺 date/source 的線索無法查證也無法標註，丟掉而不是硬塞空字串。"""
    events = [{"claim": "只有結論沒有時間"}]
    assert retrieval._attach_urls(events, ["https://a.test"]) == []


def test_verified_starts_empty():
    """verified 是留給決策層回填的欄位，召回層一律不碰。"""
    out = retrieval._attach_urls(
        [{"claim": "A", "date": "2026-08-05", "source": "s", "url": "https://a.test"}], [],
    )
    assert out[0].verified is None
    # 送進 prompt 時不帶 verified（那是輸出欄位，不是輸入）
    assert "verified" not in retrieval.FactsPack(events=out).to_prompt_json()


def test_fetch_facts_parses_fenced_json(monkeypatch):
    """模型偶爾會把 JSON 包在 ```json fence 裡。"""
    body = '```json\n{"events": [{"claim": "A", "date": "2026-08-05", "source": "s"}]}\n```'
    monkeypatch.setattr(
        retrieval.gemini_client, "generate_text_with_candidate",
        lambda prompt, model, use_search: (body, {"input_tokens": 5}, _chunks("https://a.test")),
    )
    pack, usage, model = retrieval.fetch_facts("2026-08-06")
    assert [e.claim for e in pack.events] == ["A"]
    assert pack.events[0].url == "https://a.test"
    assert usage["input_tokens"] == 5
    assert model == retrieval.settings.gemini_model_qa, "首選檔位成功時回預設 model"


@pytest.mark.parametrize("banned", ["分析", "選股", "推薦", "預測"])
def test_retrieval_rules_forbid_analysis(banned):
    """召回層 prompt 必須明文禁止做分析/選股——那是決策層的工作。"""
    assert banned in retrieval._RETRIEVAL_RULES


# ── 503 韌性：換檔位比多等有效（2026-08-06 實測 flash 整段 503，4 次重試全滅）──

def test_model_chain_is_cheap_first_and_deduped(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "gemini_model_qa", "gemini-flash-latest")
    monkeypatch.setattr(retrieval.settings, "gemini_model_classifier", "gemini-flash-lite-latest")
    monkeypatch.setattr(retrieval.settings, "gemini_model_brief", "gemini-pro-latest")
    assert retrieval._model_chain() == [
        "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest",
    ]
    # 設成同一個模型時不該重試同一檔位三次
    monkeypatch.setattr(retrieval.settings, "gemini_model_classifier", "gemini-flash-latest")
    monkeypatch.setattr(retrieval.settings, "gemini_model_brief", "gemini-flash-latest")
    assert retrieval._model_chain() == ["gemini-flash-latest"]


def test_fetch_facts_falls_through_to_next_model(monkeypatch):
    """flash 過載時要換檔位，而不是放棄——重試同一個 503 的模型退避再久也沒用。"""
    tried: list[str] = []
    ok_body = '{"events": [{"claim": "A", "date": "2026-08-05", "source": "s"}]}'

    def fake(prompt, model, use_search):  # noqa: ARG001
        tried.append(model)
        if len(tried) < 3:
            raise retrieval.LLMError("Gemini 503 overloaded")
        return ok_body, {"input_tokens": 7}, _chunks("https://a.test")

    monkeypatch.setattr(retrieval.gemini_client, "generate_text_with_candidate",
                        lambda prompt, model, use_search: fake(prompt, model, use_search))
    pack, usage, model = retrieval.fetch_facts("2026-08-06")
    assert len(tried) == 3, "前兩個檔位失敗後應繼續往下試"
    assert len(tried) == len(set(tried)), "不該重複試同一個檔位"
    assert [e.claim for e in pack.events] == ["A"]
    assert usage["input_tokens"] == 7
    # 換檔位是靜默的，呼叫端必須拿得到實際跑的 model 才能正確計價——
    # 一律以 flash 計價會在落到 flash-lite 時高估 6 倍、落到 pro 時低估。
    assert model == tried[-1] != retrieval.settings.gemini_model_qa


def test_fetch_facts_all_models_fail_degrades_quietly(monkeypatch):
    """全滅時回空包、不 raise——晨報仍要能只憑 features 產出。"""
    def _always_503(prompt, model, use_search):  # noqa: ARG001
        raise retrieval.LLMError("Gemini 503 overloaded")

    monkeypatch.setattr(retrieval.gemini_client, "generate_text_with_candidate", _always_503)
    pack, usage, model = retrieval.fetch_facts("2026-08-06")
    assert pack.events == [] and usage == {}
    assert model == retrieval.settings.gemini_model_qa, "全滅時回預設 model，呼叫端不必處理 None"
