# Multi-Agent Orchestration

Spawn background agents for independent subtasks while you keep chatting.

**Spawn when:** 2+ independent subtasks, parallel research, "do X while Y", comparative analysis, or task clearly belongs to a domain-specialist.
**Don't spawn for:** single focused tasks, dependent steps, quick questions, anything needing clarification, trivial work (< 5s).

## Predefined Domain Agents (preferred routing)

| Agent | Role Key | Skills | Use When |
|:---|:---|:---|:---|
| **Utility** | `sysutil` | system_repair, phone_control, background_command, youtube_downloader, grep_search, code_editor, file_uploader | OS repairs, Hyprland/pacman/systemd, ADB, video downloads, file ops, formatting, conversions |
| **Docs** | `docs` | document_skills, file_uploader, text_humanizer, code_editor | Creating/editing DOCX/PDF/PPTX/XLSX, reading non-text files, humanizing AI text |
| **Visuals** | `visuals` | graph_master, svg_creator, frontend_design, simulacra_engine, code_editor | Math plots, diagrams, flowcharts, UI components, physics simulations |
| **Tester** | `tester` | testing_debugging, code_editor, grep_search, background_command | Bug investigation, error diagnosis, test failures, crash analysis |

Spawn specialized subagent when there is too much context to re-read for a small final response.

## Generic Roles

| Role | Use When |
|:---|:---|
| `analyst` | Web research, fact-finding, source gathering, code review/quality analysis |
| `coder` | Multi-file code implementation, refactoring |
| `writer` | Documentation, structured writing |

## Tag Reference

| Tag | Attributes | Description |
|:--|:--|:--|
| `spawn_agent` | `role` (req), `task` (req), `model`, `context`, `instruction`, `browser_data`, `timeout`, `collect`, `todos` | Spawn a background agent. `role`: sysutil/docs/visuals/tester/analyst/coder/writer. `collect="true"` blocks for result (not recommended). `todos`: pipe-separated step list (3+ steps only). Default timeout: 300s. Give full self-contained context — agent can't see parent conversation. |
| `agent_status` | *(none)* | Check status of all running agents |
| `kill_agent` | `id` (req) | Cancel one agent by ID |

Fallback models on failure: qwen3.7-max, qwen3.7-plus, qwen3.8-max, deepseek-expert, deepseek-instant, deepseek-vision (avoid deepseek unless asked).

## Rules

- Up to 5 concurrent agents (DeepSeek: max 2 parallel, Qwen: max 4)
- Fire-and-forget is default
- If an agent finishes mid-response, acknowledge naturally and fold in the finding
- If a collected agent fails, use what you have and note the gap
- Prefer domain agents over generic roles when the task clearly fits
