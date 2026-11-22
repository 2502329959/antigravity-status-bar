# Antigravity 状态栏（antigravity-status-bar）

- **所属 Agent**：Antigravity
- **目录性质**：持久化（长期保留）
- **创建日期**：2026-11-21
- **任务简述**：Fork 自 `dimitr-io/antigravity-desktop-context`，更名为「Antigravity 状态栏」，二次开发 Claude Code 桌面端风格分段线条上下文可视化与当前会话后台任务监控。

---

A live **Antigravity 状态栏 (Status Bar)** side panel for the **Antigravity 2.0 desktop app**, forked from [dimitr-io/antigravity-desktop-context](https://github.com/dimitr-io/antigravity-desktop-context).

Like the upstream plugin, it reads the exact token breakdown and step metadata directly from Antigravity's conversation SQLite database (`~/.gemini/antigravity/conversations/<id>.db`) with **zero tokens consumed per refresh**, and fetches live model limits from the app's model catalog (`GetAvailableModels`).

## What's New in Antigravity 状态栏

1. **Claude Code Desktop Style Segmented Context Bar**:
   - Replaces the 100-dot grid with a sleek horizontal segmented progress line where each context type (System, User, Agent, Tools, Custom, System Messages) has a distinct color band proportional to its token footprint.
   - Hovering over any segment highlights it and displays its exact category, token count, and percentage in an interactive readout bar, alongside a compact legend strip.
2. **Background Tasks Monitor (`后台任务`)**:
   - Captures background tasks launched in the active conversation directly from `steps.task_details` (`run_command` async/background commands, `IsDaemon` daemons, and `schedule` timers).
   - Displays live running counts (`运行中`) and recent finished/cancelled tasks.
   - Click any task to expand its full executed command, status details (Task ID, start time, log size/update time), and live tail of its `.system_generated/tasks/task-<idx>.log` output (with one-click copy and manual/auto log refresh).

## Installation

Requires the Antigravity 2.0 desktop app and Python 3 on your `PATH` (`python`, `py`, or `python3`).

### Option 1: Symlink from workspace (recommended for local development)

```bash
git clone https://github.com/ballShell/antigravity-status-bar.git ~/Documents/AgentsWorkSpace/Antigravity-status-bar
ln -sfn ~/Documents/AgentsWorkSpace/Antigravity-status-bar ~/.gemini/config/plugins/antigravity-status-bar
```

### Option 2: Direct clone into Antigravity plugins directory

```bash
# macOS / Linux
git clone https://github.com/ballShell/antigravity-status-bar.git ~/.gemini/config/plugins/antigravity-status-bar
```

```powershell
# Windows (PowerShell)
git clone https://github.com/ballShell/antigravity-status-bar.git "$HOME\.gemini\config\plugins\antigravity-status-bar"
```

Then restart Antigravity (or toggle the plugin in **Customizations → Installed**), open **Antigravity 状态栏**, and select it from the right-side auxiliary pane.

## How it works

```text
plugin.json               plugin manifest ("Antigravity 状态栏")
sidecars/context/
  sidecar.json            registers the side panel (runs main.mjs with Antigravity's Node)
  main.mjs                backend: serves the panel, /api/report, and /api/task-log
  index.html              the panel (Claude Code style segmented bar + background task monitor)
  context_report.py       SQLite + protobuf parser (context breakdown + background_tasks)
tests/                    unit tests
```

- Every 3 seconds, the panel fetches `/api/report` for the active `conversationId` and `workspace`.
- `context_report.py --json` parses `gen_metadata` and `steps` (including `task_details` and `error_details`) from `~/.gemini/antigravity/conversations/<id>.db` read-only.
- Expanding a task calls `/api/task-log?conversationId=<id>&stepIdx=<idx>` to read up to the last 32 KiB of `~/.gemini/antigravity/brain/<id>/.system_generated/tasks/task-<idx>.log`.

## Command line and testing

```bash
# The most recently active desktop conversation
python3 sidecars/context/context_report.py --json

# Run unit tests
python3 -m unittest discover -v tests
```

## License

[MIT](LICENSE)
