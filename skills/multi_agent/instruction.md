# Multi-Agent Orchestration

Spawn background agents for independent subtasks while you keep chatting.

**Spawn when:** 2+ independent subtasks (parallel research, independent multi-file edits, "do X while Y", comparative analysis), OR the task clearly belongs to a domain-specialist agent.
**Don't spawn for:** single focused tasks you can handle directly with a skill, dependent steps, quick questions, anything needing clarification.

## Predefined Domain Agents (preferred routing)
Check these before falling back to a generic role — focused skill sets, better results for their domain.

| Agent | Role Key | Skills | Use When |
|:---|:---|:---|:---|
| **Utility** | `sysutil` | system_repair, phone_control, background_command, youtube_downloader, grep_search, code_editor, file_uploader | OS repairs, Hyprland/pacman/systemd, ADB, video downloads, file ops, formatting, conversions |
| **Docs** | `docs` | document_skills, file_uploader, text_humanizer, code_editor | Creating/editing DOCX/PDF/PPTX/XLSX, reading non-text files, humanizing AI text |
| **Visuals** | `visuals` | graph_master, svg_creator, frontend_design, simulacra_engine, code_editor | Math plots, diagrams, flowcharts, UI components, physics simulations |
| **Tester** | `tester` | testing_debugging, code_editor, grep_search, background_command | Bug investigation, error diagnosis, test failures, crash analysis |

### Routing Rules
Spawn the specialized subagent for those task when there is too much context to readd for a small final response.

## Generic Roles

| Role | Use When |
|:---|:---|
| `analyst` | Web research, fact-finding, source gathering, and code review/quality analysis |
| `coder` | Multi-file code implementation, refactoring |
| `writer` | Documentation, structured writing |

## spawn_agent
Attributes:
- role (required): sysutil | docs | visuals | tester | analyst | coder | writer
- task (required): clear, specific task description
- model: default unless user specifies or default fails (default = no mention of model)
- context: background info the agent needs
- instruction: special constraints or output format requirements
- browser_data: browser profile for authenticated access
- timeout: seconds before auto-kill (default 300)
- collect: "true" to block and wait for result inline (Not recommended)How tts can be useful on a agentic chat model (which have full access to pc, control browser, write code, edit document file, control email, telegram, has built in ide, search online, reseach, create graph simulation etc)How tts can be useful on a agentic chat model (which have full access to pc, control browser, write code, edit document file, control email, telegram, has built in ide, search online, reseach, create graph simulation etc)How tts can be useful on a agentic chat model (which have full access to pc, control browser, write code, edit document file, control email, telegram, has built in ide, search online, reseach, create graph simulation etc)
- todos: pipe-separated step list, for tasks with 3+ distinct steps only

Fallback models on failure: qwen3.7-max, qwen3.7-plus, qwen3.8-max, deepseek-expert, deepseek-instant, deepseek-vision (avoid deepseek unless asked).

Example — domain agent:
<action>
<spawn_agent role="visuals" collect="true">
  Plot sin(x) * e^(-x/5) from 0 to 4π with labeled axes and a title. Save to output/assets/damped_sine.svg.
</spawn_agent>
</action>

Example — parallel domain agents:
<action>
<spawn_agent role="sysutil">Download the audio from this YouTube video: https://youtu.be/example</spawn_agent>
</action>
<action>
<spawn_agent role="docs">Create a one-page PDF summary of the meeting notes in /tmp/notes.txt</spawn_agent>
</action>

Example — generic role:
<action>
<spawn_agent role="analyst" todos="Read engine/agents/|Read server/api/|Search for patterns|Synthesize">
  Analyze the Sable agent architecture vs industry best practices.
</spawn_agent>
</action>

Give each agent full self-contained context — it can't see the parent conversation.

## Other tags
- agent_status — check status of all agents
- kill_agent id=<agent_id> — cancel one agent

## Rules
- Up to 5 concurrent agents
- DeepSeek: max 2 parallel. Qwen: max 4
- Fire-and-forget is default; 
- If an agent finishes mid-response, acknowledge it naturally and fold in the finding
- If a collected agent fails, use what you have and note the gap
- Prefer domain agents over generic roles when the task clearly fits a domain
- Prefer doing it yourself over spawning when the task is trivial (< 5s of work)