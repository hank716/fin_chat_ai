---
name: spec-kit-migration
description: fin_chat_ai 已導入 GitHub Spec Kit 並完整遷移；specs/ 為單一真相來源、新功能走 /speckit-* 流程
metadata: 
  node_type: memory
  type: project
  originSessionId: 8f03a5a1-06d7-4d43-8f21-5a0dd2e87154
---

fin_chat_ai 已導入 **GitHub Spec Kit（spec-driven development）並完成完整遷移**（2026-07-03，已 push 到 main）。相關專案背景見 [[fin-chat-ai-project]]。

**現況與規則：**
- `specs/` 是**單一真相來源**（16/16 feature 各有 `spec.md`/`plan.md`/`tasks.md`）。`design_docs.md` 已降為背景/歷史來源，頂部有遷移橫幅；**衝突時以 `specs/` 為準**。
- 開發原則固化在 `.specify/memory/constitution.md`（憲章 v1.0.0，七原則：Gemini-only、成本上限 NT$600、guardrail fail-closed、point-in-time、local-first 10GB、服務隔離、Conventional Commits）。plan 階段要過「Constitution Check」。
- **新功能一律走正向流程**：`/speckit-specify → /speckit-clarify → /speckit-plan → /speckit-tasks →（/speckit-analyze）→ /speckit-implement`。commit 引用 `specs/NNN`。
- feature 編號對照表在 `specs/README.md`。既有 M0–M9 功能是「回溯補規格」建立的 baseline（描述現況行為）。

**工具細節（踩過的坑）：**
- 安裝 `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git`；就地初始化 `specify init --here --force --integration claude --script sh`。**integration key 是 `claude`**（README 範例寫 `copilot` 是通用例子，別照抄）。
- 這版（0.12.5）把指令裝成 **Claude Code skills**，名稱是**連字號** `/speckit-constitution`… **不是** 舊文件的 `/speckit.constitution` 點號式。裝在 `.claude/skills/speckit-*`。
- `.specify/feature.json` 是「當前 feature」工作指標、會頻繁變動 → 已加進 `.gitignore`（別提交）。`.specify/` 與 `.claude/skills/` 要入版控。

**唯一共同未竟項：** 專案**仍無 `tests/`**。16 份 tasks.md 的「測試基線」Phase 已寫成具體 pytest 任務、全標未竟 `[ ]`。使用者決策（2026-07-03）：**pytest 留到之後新的 phase 再做**，不急著補。
