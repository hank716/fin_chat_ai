# Claude 專案記憶（版控副本）

這是 Claude Code 專案記憶的**版控副本**，讓記憶能隨 repo 同步到其他機器。

- 執行環境的真實記憶在各機器的 `~/.claude/projects/<encoded-project-path>/memory/`。
- 在另一台機器要套用時，把本目錄的 `*.md` 複製到該機器對應的 `~/.claude/projects/.../memory/` 即可。
- `MEMORY.md` 是索引（每則記憶一行）；其餘每檔為一則記憶（含 frontmatter）。
