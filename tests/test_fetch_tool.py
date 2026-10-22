"""本地查證抓頁工具（spec 026 US2）。

OpenAI 沒有「只能開對話中出現過的 URL」的抓頁工具，所以查證閉環要在系統端重建：
白名單＝當篇 facts pack 的 URL、擋私有位址、每次請求都由系統記錄。
這裡釘住 contracts/decision-llm.md §3 的不變式——任何一條鬆掉，模型就能自由上網或打內網。
"""
from __future__ import annotations

import json
import socket

import httpx
import pytest

from ai import fetch_tool
from ai.fetch_tool import FetchSession

ARTICLE = "https://news.example.com/article?id=1"
OTHER = "https://news.example.com/article?id=2"

_PUBLIC = {"news.example.com": "93.184.216.34", "cdn.example.org": "93.184.216.35",
           "evil.example.net": "93.184.216.36"}


def _fake_getaddrinfo(table):
    def _resolve(host, port, *args, **kwargs):  # noqa: ARG001
        if host not in table:
            raise socket.gaierror(f"unknown host {host}")
        ips = table[host] if isinstance(table[host], list) else [table[host]]
        out = []
        for ip in ips:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (ip, port or 443)))
        return out
    return _resolve


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    table = dict(_PUBLIC)
    monkeypatch.setattr(fetch_tool.socket, "getaddrinfo", _fake_getaddrinfo(table))
    return table


class _Recorder:
    """MockTransport 的 handler：記錄所有實際發出的請求。"""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(str(request.url))
        if handler is None:
            return httpx.Response(404, text="not found")
        if callable(handler):
            return handler(request)
        return handler


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          content=body.encode("utf-8"))


def _session(routes, allowed=(ARTICLE,), *, max_uses=3, max_chars=12_000):
    rec = _Recorder(routes)
    sess = FetchSession(allowed, max_uses=max_uses, max_chars=max_chars,
                        transport=httpx.MockTransport(rec))
    return sess, rec


PAGE = """<html><head><title>t</title><style>.x{color:red}</style>
<script>var tracking = 1;</script></head><body>
<nav>首頁 | 財經 | 股市</nav>
<article><h1>台積電法說會</h1><p>第三季營收年增 30%，毛利率 58%。</p></article>
<footer>版權所有</footer></body></html>"""


def test_allowed_url_fetched_and_cleaned():
    sess, rec = _session({ARTICLE: _html(PAGE)})
    out, attempt = sess.fetch(ARTICLE)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "第三季營收年增 30%" in payload["content"]
    for noise in ("tracking", "color:red", "首頁 | 財經", "版權所有"):
        assert noise not in payload["content"]
    assert payload["truncated"] is False
    assert payload["retrieved_at"]
    assert attempt.ok and attempt.error_code is None
    assert attempt.content_chars == len(payload["content"])
    assert len(rec.requests) == 1
    assert sess.attempts == [attempt]


def test_content_truncated_but_content_chars_is_pre_truncation_length():
    body = "<html><body><p>" + ("字" * 5000) + "</p></body></html>"
    sess, _ = _session({ARTICLE: _html(body)}, max_chars=100)
    out, attempt = sess.fetch(ARTICLE)
    payload = json.loads(out)
    assert payload["truncated"] is True
    assert len(payload["content"]) == 100
    assert attempt.content_chars == 5000


def test_url_not_in_facts_pack_rejected_without_network():
    sess, rec = _session({OTHER: _html(PAGE)})
    out, attempt = sess.fetch(OTHER)
    payload = json.loads(out)
    assert payload["ok"] is False and payload["error_code"] == "url_not_allowed"
    assert attempt.error_code == "url_not_allowed"
    assert attempt.reject_reason == "not_in_facts_pack"
    assert rec.requests == []          # 一個連線都不能發


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254",
                                "172.16.0.1", "::1", "0.0.0.0", "224.0.0.1"])
def test_private_addresses_rejected_without_network(public_dns, ip):
    public_dns["news.example.com"] = ip
    sess, rec = _session({ARTICLE: _html(PAGE)})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "url_not_allowed"
    assert attempt.reject_reason == "private_address"
    assert rec.requests == []


def test_any_private_ip_among_many_rejects(public_dns):
    """DNS 回多個 IP 時只要有一個是私有就拒——否則攻擊者可混一個公開 IP 過關。"""
    public_dns["news.example.com"] = ["93.184.216.34", "127.0.0.1"]
    sess, rec = _session({ARTICLE: _html(PAGE)})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.reject_reason == "private_address"
    assert rec.requests == []


def test_redirect_to_private_address_rejected_each_hop(public_dns):
    public_dns["internal.example.com"] = "127.0.0.1"
    sess, rec = _session({
        ARTICLE: httpx.Response(302, headers={"location": "http://internal.example.com/admin"}),
        "http://internal.example.com/admin": _html("secret"),
    })
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.ok is False
    assert attempt.error_code == "url_not_allowed"
    assert attempt.reject_reason == "private_address"
    assert [str(r.url) for r in rec.requests] == [ARTICLE]   # 內網那一跳沒有發出


def test_redirect_to_public_non_whitelisted_url_allowed_and_final_url_recorded():
    """新聞站常有 canonical 轉址；跳到白名單外的**公開**網址允許，但要記下最終網址。"""
    final = "https://cdn.example.org/a/1"
    sess, _ = _session({
        ARTICLE: httpx.Response(301, headers={"location": final}),
        final: _html(PAGE),
    })
    out, attempt = sess.fetch(ARTICLE)
    assert attempt.ok is True
    assert attempt.final_url == final
    assert json.loads(out)["final_url"] == final


def test_relative_redirect_resolved():
    sess, _ = _session({
        ARTICLE: httpx.Response(302, headers={"location": "/moved"}),
        "https://news.example.com/moved": _html(PAGE),
    })
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.ok is True
    assert attempt.final_url == "https://news.example.com/moved"


def test_too_many_redirects():
    routes = {ARTICLE: httpx.Response(302, headers={"location": "https://news.example.com/r1"})}
    for i in range(1, 8):
        routes[f"https://news.example.com/r{i}"] = httpx.Response(
            302, headers={"location": f"https://news.example.com/r{i + 1}"})
    sess, rec = _session(routes)
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "url_not_accessible"
    assert len(rec.requests) == 6      # 原始 + 5 跳，第 6 次轉址就停


def test_non_http_scheme_rejected():
    url = "ftp://news.example.com/file.txt"
    sess, rec = _session({}, allowed=(url,))
    _out, attempt = sess.fetch(url)
    assert attempt.error_code == "url_not_allowed"
    assert attempt.reject_reason == "bad_scheme"
    assert rec.requests == []


def test_redirect_to_non_http_scheme_rejected():
    sess, _ = _session({ARTICLE: httpx.Response(302, headers={"location": "file:///etc/passwd"})})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.reject_reason == "bad_scheme"


def test_quota_exhausted():
    urls = [f"https://news.example.com/a{i}" for i in range(3)]
    sess, rec = _session({u: _html(PAGE) for u in urls}, allowed=urls, max_uses=2)
    sess.fetch(urls[0])
    sess.fetch(urls[1])
    out, attempt = sess.fetch(urls[2])
    assert json.loads(out)["error_code"] == "max_uses_exceeded"
    assert attempt.error_code == "max_uses_exceeded"
    assert len(rec.requests) == 2


def test_network_failures_consume_quota_but_rejections_do_not():
    urls = [f"https://news.example.com/a{i}" for i in range(3)]
    sess, _ = _session({urls[0]: httpx.Response(500), urls[2]: _html(PAGE)},
                       allowed=urls, max_uses=2)
    sess.fetch("https://evil.example.net/x")      # 白名單外：不扣
    sess.fetch(urls[0])                           # 500：扣
    sess.fetch(urls[1])                           # 404：扣
    _out, attempt = sess.fetch(urls[2])
    assert attempt.error_code == "max_uses_exceeded"
    network = [a for a in sess.attempts if a.error_code not in ("url_not_allowed",
                                                                 "max_uses_exceeded")]
    assert len(network) == 2


def test_duplicate_url_returns_cached_without_new_attempt_or_quota():
    sess, rec = _session({ARTICLE: _html(PAGE)}, max_uses=1)
    first, a1 = sess.fetch(ARTICLE)
    second, a2 = sess.fetch(ARTICLE + "#comments")     # fragment 不同仍是同一篇
    assert a1 is not None and a2 is None
    assert second == first
    assert len(rec.requests) == 1
    assert len(sess.attempts) == 1


def test_http_error_maps_to_url_not_accessible():
    sess, _ = _session({ARTICLE: httpx.Response(404)})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "url_not_accessible"


def test_http_429_maps_to_transient_code():
    sess, _ = _session({ARTICLE: httpx.Response(429)})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "too_many_requests"


def test_timeout_maps_to_unavailable():
    def _boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    sess, _ = _session({ARTICLE: _boom})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "unavailable"


def test_connect_error_maps_to_url_not_accessible():
    def _boom(request):
        raise httpx.ConnectError("refused", request=request)
    sess, _ = _session({ARTICLE: _boom})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.error_code == "url_not_accessible"


def test_dns_failure_maps_to_url_not_accessible():
    url = "https://nowhere.example.com/x"
    sess, rec = _session({}, allowed=(url,))
    _out, attempt = sess.fetch(url)
    assert attempt.error_code == "url_not_accessible"
    assert rec.requests == []


def test_pdf_is_unsupported_and_no_content_returned():
    sess, _ = _session({ARTICLE: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 ...")})
    out, attempt = sess.fetch(ARTICLE)
    payload = json.loads(out)
    assert attempt.error_code == "unsupported_content_type"
    assert attempt.content_chars is None
    assert "content" not in payload


def test_plain_text_passed_through():
    sess, _ = _session({ARTICLE: httpx.Response(
        200, headers={"content-type": "text/plain; charset=utf-8"},
        content="純文字公告內容".encode("utf-8"))})
    out, attempt = sess.fetch(ARTICLE)
    assert attempt.ok
    assert json.loads(out)["content"] == "純文字公告內容"


def test_download_capped(monkeypatch):
    monkeypatch.setattr(fetch_tool, "_MAX_DOWNLOAD_BYTES", 1000)
    big = "<html><body><p>" + "a" * 5000 + "</p></body></html>"
    sess, _ = _session({ARTICLE: _html(big)})
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.ok
    assert attempt.content_chars <= 1000


@pytest.mark.parametrize("variant", [
    "HTTPS://NEWS.EXAMPLE.COM/article?id=1",
    "https://news.example.com/article/?id=1",
    "https://news.example.com/article?id=1#top",
    "https://www.news.example.com/article?id=1",
])
def test_whitelist_normalization_matches(variant, public_dns):
    public_dns["www.news.example.com"] = "93.184.216.34"
    sess, _ = _session({ARTICLE: _html(PAGE), variant: _html(PAGE)})
    _out, attempt = sess.fetch(variant)
    assert attempt.error_code != "url_not_allowed"


@pytest.mark.parametrize("not_same", [
    "https://news.example.com/article?id=2",         # query 是文章 id，不可當雜訊
    "https://news.example.com/article/more",          # 不做前綴匹配
    "https://news.example.com/",
])
def test_whitelist_is_not_prefix_or_query_blind(not_same):
    sess, rec = _session({})
    _out, attempt = sess.fetch(not_same)
    assert attempt.reject_reason == "not_in_facts_pack"
    assert rec.requests == []


def test_empty_whitelist_rejects_everything():
    sess, rec = _session({}, allowed=())
    _out, attempt = sess.fetch(ARTICLE)
    assert attempt.reject_reason == "not_in_facts_pack"
    assert rec.requests == []


def test_never_raises_on_unexpected_error(monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(fetch_tool, "_extract_text", _explode)
    sess, _ = _session({ARTICLE: _html(PAGE)})
    out, attempt = sess.fetch(ARTICLE)
    assert json.loads(out)["ok"] is False
    assert attempt.ok is False


def test_garbage_url_does_not_raise():
    sess, _ = _session({}, allowed=("not a url",))
    out, attempt = sess.fetch("not a url")
    assert json.loads(out)["ok"] is False
    assert attempt is not None and attempt.ok is False
