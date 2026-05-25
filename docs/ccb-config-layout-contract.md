# CCB Config And Layout Contract

## 1. Purpose

This document defines the non-drifting user-facing contract for project configuration, pane layout, and tmux presentation in `ccb_source`.

It is the authoritative design anchor for:

- `.ccb/ccb.config`
- `.ccb/ccb_memory.md` and `.ccb/agents/<agent>/memory.md` project memory placement
- compact layout grammar
- config source precedence and fallback
- pane naming and pane color identity
- tmux split sizing rules for the project UI

## 2. User-Facing Config Contract

- Effective config is resolved in three layers:
  1. built-in default config from code;
  2. user config at `~/.ccb/ccb.config`;
  3. project config at `.ccb/ccb.config`.
- Higher layers override lower layers by replacing the whole effective config; CCB does not merge partial config documents across layers.
- `.ccb/ccb.config` is the highest-priority user-facing project config file.
- `~/.ccb/ccb.config` is the user-level config file used only when project config is absent.
- CCB must not auto-create, reconstruct, or rewrite `.ccb/ccb.config`; it is a user-authored project file.
- When both `.ccb/ccb.config` and `~/.ccb/ccb.config` are absent, config loading must use the built-in default project config from code and report the source kind as `builtin_default`.
- User help text, validation output, diagnostics, and docs must report the active config source kind: `project_config`, `user_config`, or `builtin_default`.
- `.ccb/config.yaml` is not part of the contract and must not be read or written by current code.

### 2.1 Project Shared Memory Files

Project memory files are user context, not startup/layout authority.

- `.ccb/ccb_memory.md` under the project anchor is the shared CCB project memory file.
  - Startup may create it only when missing.
  - Startup must not overwrite user edits.
  - Teams may whitelist and commit it when they want shared agent collaboration rules.
- `.ccb/agents/<agent>/memory.md` is optional agent-private memory.
  - It is user-editable local data and must not be deleted or rewritten by
    normal startup.
  - `<agent>` is the normalized logical agent name from `.ccb/ccb.config`.
  - This path is anchored under the project `.ccb/` directory and does not move
    with relocated runtime state.
- Generated project-memory metadata and runtime bundles are CCB-owned state:
  - `<runtime_state_root>/state/memory.seed.json` records template seed
    metadata for the initial `.ccb/ccb_memory.md`.
  - `<runtime_state_root>/runtime/memory/<agent>.md` is generated runtime
    memory for providers that need a stable file path.
  - `project_root/.ccb/runtime/memory/<agent>.md` may be generated as a
    provider compatibility bridge for tools such as OpenCode that consume
    project-relative instruction paths.
- When runtime state is relocated, user-editable memory remains under the
  project anchor while generated runtime memory follows `runtime_state_root`.
- `.ccb/` is local/runtime state by default. Projects that want to commit
  selected files such as `.ccb/ccb.config` must opt in with their own
  `.gitignore` whitelist.

## 3. Compact Layout Grammar

The primary config format is compact text.

Leaf tokens:

- `cmd`
- `agent_name:provider`

Operators:

- `;`
  - horizontal split, left to right
- `,`
  - vertical split, top to bottom
- `(...)`
  - explicit grouping

Operator precedence:

1. `,`
2. `;`

Examples:

- `cmd; agent1:codex`
- `cmd; agent1:codex, agent2:claude`
- `cmd, agent1:codex; agent2:codex, agent3:claude`
- `cmd, agent1:codex; agent2:codex, (agent3:claude; agent4:gemini)`

## 4. Semantic Rules

- `cmd` is reserved and must not declare a provider.
- Each configured agent must appear exactly once in the layout.
- `cmd` may appear at most once.
- When `cmd` is enabled, `cmd` must be the first leaf in layout traversal so the invoking pane remains the command pane anchor.
- Compact config leaf order defines `default_agents`.
- Rich `ccb.config` formats may define agents separately, but must still provide a `layout` compatible with the same leaf rules.

### 4.1 Compact Header With Agent Overlay

`ccb.config` may combine a compact layout header with trailing TOML agent
overlays.

Example:

```toml
cmd, agent1:codex; agent2:claude

[agents.agent1]
key = "..."
url = "..."
```

Contract:

- The first compact block is the authority for:
  - `layout`
  - `default_agents`
  - `cmd_enabled`
  - agent `provider`
  - agent `workspace_mode`
- The trailing TOML overlay may define only `agents.<name>...` tables.
- Hybrid overlays must not redefine compact-header-owned agent fields such as:
  - `provider`
  - `workspace_mode`
- Hybrid overlays must not introduce agents that do not already exist in the
  compact layout header.
- Config rendering should prefer:
  - pure compact when no per-agent overlay is needed
  - compact header + TOML overlay when agent-local overrides are needed
  - expanding simple overlay cases into full-document TOML is not the preferred
    canonical output

### 4.1.1 Explicit Windows Topology

Rich TOML may declare named project windows through `[windows]`:

```toml
version = 2
entry_window = "main"

[windows]
main = "main:codex"
work = "worker1:codex(worktree), worker2:claude(worktree)"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
```

Contract:

- legacy compact and hybrid configs that do not declare `[windows]` remain
  single-window configs; they are mounted in the project workspace window and
  keep their existing `cmd` pane semantics
- `[windows]` is the authority for layout, default agent traversal, and per-window agent grouping.
- Each `[windows]` value uses the compact layout grammar, but `cmd` is not supported in windows topology.
- Every agent leaf in `[windows]` must declare a provider.
- Each configured agent must appear in exactly one window layout.
- Windows topology must not be combined with legacy `default_agents`, `layout`, or `cmd_enabled` fields.
- `entry_window` is optional and defaults to the first declared window.
- `[ui.sidebar]` is valid only with windows topology. Defaults are `mode = "every_window"`, `width = "15%"`, and `bottom_height = 20`; `width` accepts either a positive integer column count or a percentage string.
- In `mode = "every_window"`, CCB treats `width` as a project-wide sidebar width. Topology refreshes must resize every managed sidebar pane to the same configured share of its tmux window so page/window switches do not leave sidebars at different widths. If the user drags a sidebar border, CCB stores that runtime column width in the project tmux session and applies it to every managed sidebar window until the session is recreated. If tmux later resizes a window because a terminal client attaches or changes size, CCB reapplies the stored runtime width instead of treating the auto-resized pane width as a new user preference.
- `[agents.<name>]` tables may provide agent overrides; if they repeat `provider`, it must match the provider declared in `[windows]`.

### 4.2 Agent API Shortcut

For the common case where an agent only needs its own API key or base URL, rich
or hybrid `ccb.config` may use agent-local shortcut fields in the agent table:

```toml
[agents.agent1]
key = "..."
url = "..."
```

Contract:

- `key` and `url` are supported only for known API-backed providers with
  first-class
  mappings:
  - `codex`
  - `claude`
  - `gemini`
- `key` and `url` are the only canonical shortcut fields.
- `key/url` is user-facing sugar only. The loader must compile it to the existing
  provider-profile API env authority for that provider and force
  `provider_profile.inherit_api = false`.
- For Codex, compiling the `url` shortcut must normalize a bare origin such as
  `https://example.test` to the OpenAI-compatible API root
  `https://example.test/v1`.
- Explicit `key/url` authority must also suppress inherited provider state that
  would silently redefine that API authority.
  For all shortcut-backed providers, compiling `key/url` must also disable
  inherited auth projection so managed startup does not retain a second
  credential authority beside the explicit agent-local API route.
  For Codex, `key/url` disables inherited global `config.toml` routing
  projection, replaces it with an agent-local managed `config.toml`
  `model_provider` / `model_providers.<id>` authority derived from that
  explicit API route. That managed Codex route must use the standard custom
  provider shape with `requires_openai_auth = false`, and explicit base-url env
  exports must be suppressed so the managed `config.toml` remains the single
  route authority. An explicit `key` also disables inherited global `auth.json`
  credential projection.
- When `key` or `url` is present, provider API env must not also be declared in:
  - `agents.<name>.env`
  - `agents.<name>.provider_profile.env`
- Advanced API env not expressible as `key` or `url` remains a
  `provider_profile.env` concern. Do not invent a second runtime path for that
  advanced case.
- Legacy nested syntax under `agents.<name>.api` remains accepted for backward
  compatibility, but it is non-canonical.
- Config rendering and recovery must preserve the user-facing `key/url` shortcut
  instead of expanding it back into verbose provider-profile API env or nested
  `api` tables.

### 4.3 Agent Model Shortcut

For the common case where an agent only needs a provider model override, rich or
hybrid `ccb.config` may use an agent-local model shortcut:

```toml
[agents.agent1]
model = "gpt-5"
```

Contract:

- `model` is supported only for providers with first-class CLI model flags:
  - `codex`
  - `claude`
  - `gemini`
  - `opencode`
- `model` is user-facing sugar only. The loader/runtime model must compile it
  onto the existing provider startup-argument path instead of introducing a
  second launch authority.
- `model` may coexist with unrelated `startup_args`, but must not be combined
  with provider model flags already present in `startup_args`.
- Config rendering and recovery must preserve the user-facing `model` field
  instead of expanding it into provider-specific `startup_args`.

### 4.4 Workspace Mode Semantics

- `workspace_mode = "inplace"` means the agent uses the project root directly.
- `workspace_mode = "git-worktree"` means the project root must be a valid git
  repository and startup must materialize a real `git worktree`.
- `git-worktree` must not silently fall back to copying the project directory
  when the project root is not a git repository. Startup must fail with an
  actionable error instead.
- `workspace_mode = "copy"` is the only mode that may create an explicit
  directory copy of the project tree.

## 5. Default Layout Contract

Bootstrap must generate a balanced two-column layout over all visible panes.

For `cmd + N agents`:

- 1 agent: `cmd; agent1`
- 2 agents: `cmd; agent1, agent2`
- 3 agents: `cmd, agent1; agent2, agent3`
- 4 agents: `cmd, agent1; agent2, agent3, agent4`

General rule:

- split the full pane list into left and right halves
- stack each half vertically
- keep pane areas uniform by sizing each split according to descendant leaf counts

## 6. Tmux Layout Execution Contract

- The current pane is the `cmd` anchor pane.
- Layout execution must prune the configured layout to the requested foreground agent subset plus `cmd`.
- Layout execution must first build a normalized visible-layout plan from `parse -> prune -> render`, and that normalized render is the visible layout signature.
- Layout execution must preserve the relative structure of the configured layout after pruning.
- Recursive split percentages must be computed from leaf-count ratios, not hardcoded repeated `50%` splits.
- Pane pruning must never silently reorder agents.
- Incremental in-place splitting on top of an already materialized project namespace is not a valid way to realize a different visible layout signature.
- When the desired visible layout signature changes, startup must recreate the project namespace before rematerializing tmux panes.
- During layout materialization, newly split panes must be created with a silent placeholder command in the initial `split-window` call, not as empty panes that are later respawned. Once assigned to a provider leaf, panes must keep using the normal managed respawn path so provider shell, stderr-log, and `remain-on-exit` semantics are preserved.

## 7. Pane Presentation Contract

- `.ccb/ccb.config` logical leaf names are the only authority for pane display names.
- Pane titles must be the exact logical names:
  - `cmd`
  - `agent1`, `agent2`, ...
- Pane border labels must show the logical pane name, not tmux pane numbers.
- Provider-specific pane markers such as `CCB-agent1-...` are internal runtime evidence only:
  - they may be persisted in provider session files
  - they must not override pane titles, pane headers, or focus labels in the project namespace UI
- Tmux pane user options and visible titles must be reconciled back to the configured logical name whenever a project-owned pane is reused or rebound.
- The command pane and agent panes must have stable, distinct color identities.
- Pane styling is session-scoped CCB UI state and must not permanently overwrite unrelated user tmux themes.

## 8. Project Namespace UI Contract

- The project-owned tmux socket/session is responsible for its own theme and pane header rendering.
- Project UI correctness must not depend on whether the invoking shell is already inside some outer tmux server.
- Namespace creation or reuse must reapply session-scoped CCB tmux options on the project-owned socket.
- When a project-owned pane dies and the daemon chooses namespace-level recovery, it must recreate and re-project the configured layout so each logical pane returns to its canonical position.
- Namespace `layout_version` is the compatibility key for visible pane topology and tmux UI presentation:
  - when the stored namespace layout version differs from the current code contract, the project namespace must be recreated
  - recreating the namespace is the preferred healing path for stale pane geometry or stale session-scoped UI options
- Namespace state must also track the current visible layout signature derived from `.ccb/ccb.config` after foreground pruning.
- If the stored visible layout signature differs from the desired visible layout signature for the current foreground start, the namespace must be recreated instead of trying to patch geometry in place.

## 9. Update Discipline

- If `.ccb/ccb.config` grammar changes, update this document in the same patch.
- If project memory file placement or generated memory path semantics change,
  update this document in the same patch.
- If bootstrap layout defaults change, update this document in the same patch.
- If pane naming, split sizing, or pane theming rules change materially, update this document in the same patch.
