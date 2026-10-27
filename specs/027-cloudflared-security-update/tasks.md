# Tasks: cloudflared 安全性更新（P0）

**Input**: [spec.md](spec.md)、[plan.md](plan.md)

## Phase 1: 評估

- [X] T001 [US1] 確認目前版本：`docker exec ai-market-cloudflared cloudflared --version` → 2026.5.2（受影響）
- [X] T002 [US1] 對照官方 RELEASE_NOTES，列出 5.2 之後的安全修補（見 spec 背景）

## Phase 2: 實作

- [X] T003 [US2] `docker-compose.yml` cloudflared image 釘 `cloudflare/cloudflared:2026.10.5`
- [X] T004 [P] [US2] `design_docs.md` §26 範例同步
- [X] T005 [P] [US2] `DEPLOY.md` 新增 §7.1 升級流程
- [X] T006 [P] [US2] 新增 `tests/test_compose_pins.py`
- [X] T007 [P] `specs/README.md` 新增 027 列、標註最高優先

## Phase 3: 部署與驗收

- [X] T008 [US1] `docker compose pull cloudflared; docker compose up -d cloudflared`（避開 08:30）
- [X] T009 [US1] 驗證：版本 = 2026.10.5；`docker logs ai-market-cloudflared` 有 4 條 registered；ingress 仍 → `http://caddy:80`
- [ ] T010 [US1] 瀏覽器開 `https://www.hank-finflow.com/`，Access 登入後看得到歷史列表（使用者手動確認）　※ 10/27 curl 已確認未登入 → 302 至 cloudflareaccess.com 登入頁
- [X] T011 [US2] `pytest tests/test_compose_pins.py` 通過（本次只動 compose/文件，未碰程式碼）

## Phase 4: 待使用者確認

- [ ] T012 對照 Cloudflare email 內文：若指定的修補版本高於 2026.10.5，或要求輪換 tunnel token 等額外動作，補 task 處理
