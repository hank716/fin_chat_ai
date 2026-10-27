# Feature Specification: cloudflared 安全性更新

**Feature Branch**: `027-cloudflared-security-update`

**Created**: 2026-10-27

**Status**: 已部署（2026-10-27 22:26，版本 2026.10.5、4 條連線 registered）；待使用者以瀏覽器登入確認 + 對照 email

**Priority**: 🔴 **P0（最高）**——優先於所有其他待辦

**Input**: 使用者 2026-10-27 收到 Cloudflare email，通知 Cloudflare Tunnel 有安全性問題需更新；要求檢查是否受影響並排入最高優先 spec。

## 背景與影響評估

- **受影響**：容器 `ai-market-cloudflared` 跑的是 **cloudflared 2026.5.2**（image 建於 2026-05-27）；官方最新 **2026.10.5**（2026-10-27）。
- **根因**：`docker-compose.yml` 使用 `cloudflare/cloudflared:latest` + `--no-autoupdate`；compose 不會自動重抓 `:latest`，所以卡在舊版五個月。
- **5.2 之後官方 RELEASE_NOTES 的安全相關修補**：
  - 2026.10.0 `VULN-141859`：Normalize request path when using access rules（**與本專案用的 Cloudflare Access 直接相關**）
  - 2026.10.1：Go 1.27.2 修 `GO-2026-6600`
  - 2026.10.2 / 10.3：Go 1.27.3、distroless／debian 基底映像更新
  - 2026.10.4 `VULN-176088`：Allowlist debug endpoints on the metrics server
  - 2026.10.5 `VULN-183456`：升級 go-chi/chi v5.5.0
  - 第三方回報（官方未證實）：< 2026.10.2 信任 8 個弱金鑰 eMudhra 中繼 CA
- **實際曝險**：偏低但不可忽略。cloudflared 未對外 publish 任何 port（metrics 不外露）；origin 是內網 `http://caddy:80`（CA 信任問題不影響 origin 段）；但 Access 路徑正規化問題位在對外入口。

## User Scenarios

### US1 — 對外入口跑在已修補版本（P0）

家庭成員透過 `https://www.hank-finflow.com` 看報告時，tunnel 使用已修補的 cloudflared 版本，Access 認證照常運作。

**驗收**：
1. `cloudflared --version` 顯示 2026.10.5（或更新的官方修補版）。
2. tunnel 4 條連線 registered，ingress 仍指向 `http://caddy:80`。
3. Access 名單內 email 登入後能看歷史列表；名單外被擋。

### US2 — 不再靜默落後（P0）

cloudflared 版本由 repo 明確控制，有文件化升級流程，並有測試防止退回浮動 tag。

**驗收**：
1. `docker-compose.yml` 中 cloudflared image 為明確版本 tag。
2. `tests/test_compose_pins.py` 通過；改回 `:latest` 會讓測試失敗。
3. `DEPLOY.md` §7.1 有升級步驟。

## Requirements

- **FR-001**：cloudflared image 必須釘明確版本 tag，不得為 `:latest` 或無 tag。
- **FR-002**：部署只重建 cloudflared，不得打斷 backend / bot / scheduler；避開 08:30 晨報時段。
- **FR-003**：升級流程寫入 `DEPLOY.md`。

## Out of Scope

- 輪換 tunnel token：目前沒有證據顯示 token 外洩；若 Cloudflare email 要求，另開 task。
- 自動化版本監控（例如定期比對最新 release）：先靠 Cloudflare email + 文件流程。

## Success Criteria

- **SC-001**：部署後版本 = 2026.10.5，外網 Access 頁可用。
- **SC-002**：全套測試基線不退步。
