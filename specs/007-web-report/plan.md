# Implementation Plan: Web Report Page（SSR）

**Branch**: `007-web-report` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

以 Jinja2 SSR（autoescape）渲染單篇報告與首頁歷史；FastAPI 路由提供 HTML/JSON/MD 三種格式與首頁面板
（cost/activity/calibration/evaluation/history）。純伺服器端渲染、無前端框架；深色模式/RWD 為模板層增益。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: FastAPI（HTMLResponse/PlainTextResponse）、Jinja2
**Storage**: 讀 [006] 落地的 report dict（本機/pCloud 回補）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）；瀏覽器（RWD）
**Project Type**: web-service（SSR 頁面 + 路由）
**Performance Goals**: SSR 單頁毫秒級
**Constraints**: autoescape 防注入；不外洩 raw CoT；report_id 路徑守衛（[006]）
**Scale/Scope**: `web_renderer.py`（35）+ 3 templates + `api/brief.py` 路由

## Constitution Check

*GATE: 對照憲章七原則。*

- **III. Fail-Closed / 不外洩 raw CoT** — ✅ 頁面顯示 guardrail 攔截狀態，只呈現結構化結論。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/II/IV/V/VI — N/A（純呈現層；資料/成本/儲存在其他 feature）。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/007-web-report/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/reports/web_renderer.py   # render_report_html / render_history_html + CLAIM_TAG
backend/templates/
├── base.html      # 版型（深色/RWD/回頂）
├── report.html    # 單篇報告（敘事/候選/evidence/guardrail/成本）
└── history.html   # 首頁列表 + 面板
backend/api/brief.py               # GET / , /report/{id}(.json/.md/html) 路由
tests/web/                         # ⬜ 待新增
```

**Structure Decision**: 渲染邏輯集中在 `web_renderer.py`（薄）+ 模板承載版面；路由在 `api/brief.py`。
維持純 SSR、無前端建置流程，符合家用單機的簡單性。

## Complexity Tracking

> 無 Constitution 違規，免填。
