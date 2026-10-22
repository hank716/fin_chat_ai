"""查證行為紀錄與本地抓頁工具（spec 023 FetchAttempt；spec 026 FetchSession）。

`FetchAttempt` 原本住在 `claude_client`。spec 026 新增 OpenAI 決策路徑後，它不再是某一家
供應商的東西，搬到這個中立模組；`claude_client` 保留同名 re-export，舊的 import 不必改。

## 本地 web_fetch（spec 026 US2）

OpenAI 沒有「只能開對話中已出現 URL」的伺服器端抓頁工具，所以由 backend 自己抓。
這比 Anthropic 那個限制更嚴：白名單是**程式強制**的「當篇 facts pack 引用的 URL」，
模型連 web_search 結果裡的網址都開不了——查證只能核對召回層真的引用過的來源（憲章 I）。

錯誤碼刻意沿用 spec 023 的值域（anthropic SDK 的 WebFetchToolResultErrorCode），
`reports.verification_stats.classify_outcomes` 因此不必認得這是哪一家供應商。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger("ai-market-backend.fetch_tool")


@dataclass(frozen=True)
class FetchAttempt:
    """一次 `web_fetch` 的實際結果（spec 023 FR-006）。

    `fact_checks[].verdict` 是**模型自述**，無法稽核；這個結構是**工具行為**，可以。
    2026-08-20 那篇實測：4 則 `confirmed` 分屬 4 個不同 URL，但當天只開了 3 次原文——
    至少一則「已查證」沒有任何工具行為支撐。只讀 verdict 永遠看不出這件事。
    """

    url: str
    ok: bool
    error_code: str | None
    retrieved_at: str | None
    # 回到手上的原文長度（字元）。None＝失敗或非純文字（如 PDF）。「成功但幾乎沒內容」
    # 與「成功且讀得到內文」在工具端都是 ok=True，不記長度就分不出來。
    content_chars: int | None = None
    # True＝這次 fetch 是從 code execution 裡發出的（原文不直接進模型視野）。
    # `claude_client.build_tools` 已釘 direct，這個欄位正常應恆為 False——出現 True 代表那個設定失效了。
    via_code: bool = False
    # spec 026：`error_code="url_not_allowed"` 時的細分原因（not_in_facts_pack / private_address /
    # bad_scheme），只供除錯。統計一律看 error_code，分類邏輯不需要認得這個欄位。
    reject_reason: str | None = None
    # spec 026：跟完轉址後的實際網址（與 url 不同時才記）。
    final_url: str | None = None


_TIMEOUT_SECONDS = 10.0
_MAX_REDIRECTS = 5
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
_ALLOWED_SCHEMES = ("http", "https")
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain",)
# 抽純文字時整段丟掉的元素：版頭版尾、導覽、腳本。留著只會讓 12k 字元的額度被選單吃光
# （Claude 那邊 3000 token 上限時 money.udn 只拿到頁首 meta 就是同一種失效）。
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside",
               "form", "iframe", "svg")
_USER_AGENT = "Mozilla/5.0 (compatible; fin-chat-ai-verifier/1.0)"

# 被拒原因（FetchAttempt.reject_reason）
NOT_IN_FACTS_PACK = "not_in_facts_pack"
PRIVATE_ADDRESS = "private_address"
BAD_SCHEME = "bad_scheme"


def normalize_url(url: str) -> str:
    """白名單比對鍵：scheme/host 轉小寫、去 `www.`、去 fragment、去 path 尾斜線。

    與 `reports.verification_stats.normalize_url` 同一套規則（那邊用來把 attempt 配回線索），
    兩邊對「同一篇」的認定必須一致，否則會出現「放行了卻配不上」或反之。
    ⚠️ **query 保留**——常常就是文章 id；**不做前綴匹配**——否則白名單一條就放行整站。
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw.rstrip("/").lower()
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = "?" + parts.query if parts.query else ""
    return parts.scheme.lower() + "://" + host + path + query


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])   # 去掉 IPv6 zone id
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped                         # ::ffff:127.0.0.1 也要擋
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


class _Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Failed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_target(url: str) -> None:
    """scheme 與解析後 IP 檢查。**每一跳轉址都要重跑**——只檢查第一跳等於沒擋。

    已知限制：檢查與 httpx 實際連線各解析一次 DNS，理論上有 rebinding 的時間差。
    這裡的威脅是「模型被誘導去開內網」，不是有能力控制 DNS 的對手；而白名單已把可開的
    網址限縮到召回層引用的新聞來源，殘餘風險可接受。
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise _Rejected(BAD_SCHEME)
    host = parts.hostname
    if not host:
        raise _Failed("url_not_accessible", "網址缺少主機名稱")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise _Failed("url_not_accessible", f"無法解析主機 {host}") from exc
    ips = {info[4][0] for info in infos}
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise _Rejected(PRIVATE_ADDRESS)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _extract_text(raw: bytes, media_type: str, charset: str | None) -> str:
    if media_type in _TEXT_TYPES:
        return raw.decode(charset or "utf-8", errors="replace").strip()
    from bs4 import BeautifulSoup   # 延遲匯入：只有真的抓到 HTML 才需要

    soup = BeautifulSoup(raw, "html.parser", from_encoding=charset)
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _read_capped(response: httpx.Response) -> bytes:
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) >= _MAX_DOWNLOAD_BYTES:
            del buf[_MAX_DOWNLOAD_BYTES:]
            break
    return bytes(buf)


_MESSAGES = {
    "url_not_allowed": "不允許開啟此網址：只能開啟 facts pack 中列出的來源。",
    "max_uses_exceeded": "本篇查證次數已用完，請以已取得的資訊作答。",
    "url_not_accessible": "此來源目前無法開啟。",
    "too_many_requests": "此來源暫時拒絕連線（請求過多）。",
    "unavailable": "此來源回應逾時。",
    "unsupported_content_type": "此來源不是可讀的文字頁面（例如 PDF 或圖片），無法讀取。",
}


class FetchSession:
    """單篇晨報生命週期內的查證抓頁器。

    不變式（contracts/decision-llm.md §3）：
    - 白名單外、非 http(s)、解析到非公開 IP 的請求**不發出任何網路連線**。
    - 每一跳轉址都重新檢查 scheme 與 IP。
    - 實際走到網路的次數（成功＋網路失敗）≤ `max_uses`；被拒不扣額度。
    - 同一網址（正規化後）第二次請求回上次結果，不新增 attempt、不扣額度。
    - `fetch` 永不 raise：任何例外都轉成失敗 attempt，不讓晨報掛掉。

    錯誤碼對照：HTTP 429 記 `too_many_requests`（spec 023 歸「暫時性失敗」，比籠統的
    `url_not_accessible` 誠實——那個來源不是打不開，只是當下擋了我們）。
    """

    def __init__(
        self, allowed_urls: Iterable[str], *, max_uses: int, max_chars: int,
        transport: httpx.BaseTransport | None = None,
    ):
        self._allowed = {normalize_url(u) for u in allowed_urls if u}
        self._max_uses = int(max_uses)
        self._max_chars = int(max_chars)
        self._transport = transport
        self._used = 0
        self._cache: dict[str, str] = {}
        self._attempts: list[FetchAttempt] = []

    @property
    def attempts(self) -> list[FetchAttempt]:
        return list(self._attempts)

    @property
    def used(self) -> int:
        """實際發出網路請求的次數（扣額度的那種）。"""
        return self._used

    def fetch(self, url: str) -> tuple[str, FetchAttempt | None]:
        """回 (給模型的 JSON 字串, 新增的 attempt；重複 URL 回 None)。"""
        url = str(url or "").strip()
        key = normalize_url(url)
        if key in self._cache:
            return self._cache[key], None
        try:
            output, attempt = self._fetch_new(url, key)
        except Exception as exc:  # noqa: BLE001 — 最後防線：查證失敗不得讓晨報失敗
            logger.warning("web_fetch 非預期錯誤 %s: %s", url, exc)
            output, attempt = self._fail(url, "url_not_accessible")
        self._cache[key] = output
        self._attempts.append(attempt)
        return output, attempt

    # ── internals ──

    def _fail(self, url: str, code: str, *, reason: str | None = None,
              final_url: str | None = None) -> tuple[str, FetchAttempt]:
        attempt = FetchAttempt(url=url, ok=False, error_code=code, retrieved_at=None,
                               reject_reason=reason, final_url=final_url)
        payload = {"ok": False, "url": url, "error_code": code,
                   "message": _MESSAGES.get(code, "無法開啟此來源。")}
        return json.dumps(payload, ensure_ascii=False), attempt

    def _fetch_new(self, url: str, key: str) -> tuple[str, FetchAttempt]:
        if key not in self._allowed:
            return self._fail(url, "url_not_allowed", reason=NOT_IN_FACTS_PACK)
        if urlsplit(url).scheme.lower() not in _ALLOWED_SCHEMES:
            return self._fail(url, "url_not_allowed", reason=BAD_SCHEME)
        if self._used >= self._max_uses:
            return self._fail(url, "max_uses_exceeded")
        try:
            _check_target(url)
        except _Rejected as rej:
            return self._fail(url, "url_not_allowed", reason=rej.reason)
        except _Failed as fail:
            return self._fail(url, fail.code)

        self._used += 1
        current = url
        try:
            with httpx.Client(transport=self._transport, timeout=_TIMEOUT_SECONDS,
                              follow_redirects=False,
                              headers={"user-agent": _USER_AGENT}) as client:
                for hop in range(_MAX_REDIRECTS + 1):
                    with client.stream("GET", current) as resp:
                        if not resp.is_redirect:
                            return self._finish(url, current, resp)
                        location = resp.headers.get("location", "")
                        if hop == _MAX_REDIRECTS or not location:
                            return self._fail(url, "url_not_accessible", final_url=current)
                        current = urljoin(current, location)
                    _check_target(current)
        except _Rejected as rej:
            return self._fail(url, "url_not_allowed", reason=rej.reason, final_url=current)
        except _Failed as fail:
            return self._fail(url, fail.code, final_url=current)
        except httpx.TimeoutException:
            return self._fail(url, "unavailable")
        except httpx.HTTPError:
            return self._fail(url, "url_not_accessible")
        return self._fail(url, "url_not_accessible")   # 理論上到不了（迴圈內必 return）

    def _finish(self, url: str, final: str, resp: httpx.Response) -> tuple[str, FetchAttempt]:
        final_url = final if final != url else None
        if resp.status_code == 429:
            return self._fail(url, "too_many_requests", final_url=final_url)
        if resp.status_code >= 400:
            return self._fail(url, "url_not_accessible", final_url=final_url)
        media = _media_type(resp)
        if media not in _HTML_TYPES + _TEXT_TYPES:
            return self._fail(url, "unsupported_content_type", final_url=final_url)
        raw = _read_capped(resp)
        text = _extract_text(raw, media, resp.charset_encoding)
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        attempt = FetchAttempt(url=url, ok=True, error_code=None, retrieved_at=retrieved_at,
                               content_chars=len(text), final_url=final_url)
        payload = {"ok": True, "url": url, "final_url": final, "retrieved_at": retrieved_at,
                   "truncated": len(text) > self._max_chars,
                   "content": text[: self._max_chars]}
        return json.dumps(payload, ensure_ascii=False), attempt
