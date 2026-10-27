# Implementation Plan: cloudflared 安全性更新

**Branch**: `027-cloudflared-security-update` | **Date**: 2026-10-27 | **Spec**: [spec.md](spec.md) | **Priority**: P0

## Summary

把 `docker-compose.yml` 的 `cloudflare/cloudflared:latest` 改成釘死 `2026.10.5`，只 pull + 重建 cloudflared 容器；補 DEPLOY.md 升級流程與一個防退回 `:latest` 的測試。

## Technical Context

- 服務：`cloudflared`（`docker-compose.yml`），`tunnel --no-autoupdate run`，token 走 `TUNNEL_TOKEN`；ingress 由 Cloudflare 後台管理（`www.hank-finflow.com → http://caddy:80`），不需改後台。
- 無 published ports；與 backend/bot/scheduler 互不依賴（只 depends_on backend/caddy 啟動順序）。
- 測試：pytest + PyYAML（已在環境中）。

## Constitution Check

- 不改任何 LLM／成本／資料流程；不影響晨報。✅
- 對外入口安全屬 P0，優先於研究型待辦。✅

## Changes

| 檔案 | 變更 |
|------|------|
| `docker-compose.yml` | cloudflared image → `cloudflare/cloudflared:2026.10.5` |
| `design_docs.md` | §26 範例同步釘版本 |
| `DEPLOY.md` | 新增 §7.1 升級 cloudflared |
| `specs/README.md` | 新增 027 列並標註目前最高優先 |
| `tests/test_compose_pins.py` | 新增：cloudflared image 必須有明確版本 tag |

## Rollout

1. `docker compose pull cloudflared`
2. `docker compose up -d cloudflared`（只重建這一個；外網斷線數秒）
3. 驗證版本、registered 連線、Access 頁面

## Rollback

把 tag 改回前一個已知可用的版本（例如 `2026.10.4`）→ `docker compose up -d cloudflared`。不建議回到 2026.5.2。
