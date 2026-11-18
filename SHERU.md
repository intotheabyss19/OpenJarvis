# Sheru build of OpenJarvis

Yash's personal-assistant fork. OpenJarvis is the brain/data layer (connectors, memory, scheduler, digest,
approvals, channels); Sheru adds the Mac body (voice, dictation + clean-text, computer use) on top.

## Branches and remotes
- `upstream` = https://github.com/open-jarvis/OpenJarvis (read-only; pull their updates from here)
- `sheru` = our work. Keep `main` identical to `upstream/main`.

Sync with upstream (weekly, small steps):

    git fetch upstream
    git checkout main && git merge --ff-only upstream/main
    git checkout sheru && git merge main      # resolve, then run tests + our eval

Rules: put our code in our own modules (tools/agents/connectors under a `sheru` package or MCP servers);
touch upstream files as little as possible, and list every one we do touch below.

## Running
1. Model server (huihui Qwen3.6-35B-A3B, MLX 4-bit, thinking off):

       .venv/bin/mlx_lm.server --model /Users/ysh/Projects/Sheru/data/models/huihui-qwen3.6-35b-a3b-ablit-4bit \
           --host 127.0.0.1 --port 8080 --chat-template-args '{"enable_thinking": false}'

2. Config: `~/.openjarvis/config.toml` (engine = mlx, analytics off, security profile personal).
3. `.venv/bin/jarvis ask --agent orchestrator --tools calculator "..."`
4. Google (Gmail, Calendar, Tasks, Drive, Contacts): Cloud project `sheru-515013`, OAuth client "Sheru Mac" (Desktop app).
   One consent writes tokens to `~/.openjarvis/connectors/{google,gmail,gcalendar,...}.json` (chmod 600).
   Re-auth: `.venv/bin/jarvis connect gdrive`. App is published (no 7-day token expiry) once "Publish app" is clicked
   under Google Auth Platform → Audience.

## Our code
- `src/openjarvis/sheru/approvals.py` — yes/no approvals for tool calls (tests: `tests/sheru/`).
  Turned on by `[sheru.approvals] enabled = true` in config.toml. Reads (calendar, inbox, files, search) run
  freely; anything that sends / writes / deletes / runs code waits for a Yes, otherwise it's refused (no answer
  within `timeout_seconds` counts as No). Asks via a native macOS dialog now; voice / phone answer through
  `approvals.pending()` + `approvals.decide(id, yes)` (first answer wins). Which tools ask: the `ASK_TOOLS`
  set, any tool with `requires_confirmation`, `http_request` unless GET/HEAD, and any unknown (MCP) tool whose
  name has an action verb (send, delete, create, click, ...). Tune with `always_ask` / `never_ask`.

- `src/openjarvis/sheru/google_tools.py`: live Google tools for the agent. Reads (`gmail_search`, `gmail_read`,
  `calendar_events`, `tasks_list`) run freely; `calendar_create_event` and `gmail_create_draft` ask first. The
  OAuth scope can't send mail, so drafts are as far as email goes. Uses `~/.openjarvis/connectors/google.json`.

## Upstream files we've changed
- `src/openjarvis/tools/_stubs.py` (`ToolExecutor.execute`, ~10 lines at "Confirmation check"): calls
  `sheru.approvals.gate()`. Doing it here, rather than at each call site, overrides every hardcoded
  auto-approve (`confirm_callback=lambda _prompt: True` in `server/agent_manager_routes.py` x4, `cli/ask.py` x2,
  `jarvis agents ask --yes` default) and any new ones upstream adds. With approvals disabled, behaviour is
  exactly upstream's. On merge conflicts there: keep their change, re-add the gate call above
  `if tool.spec.requires_confirmation`.
- `src/openjarvis/tools/__init__.py` (+5 lines at the end): imports `openjarvis.sheru.google_tools` so its tools
  register. Upstream has no plugin loading yet; drop this when it does.

Known limit: the server's SSE chat path calls the executor from inside the async handler, so while a dialog
is waiting that request's stream pauses (the dialog still works). Revisit when the voice front end answers
over HTTP.
