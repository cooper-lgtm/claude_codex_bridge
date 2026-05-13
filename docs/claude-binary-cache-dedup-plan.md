# Claude Binary Cache Dedup Plan

## 1. Purpose

This plan defines how CCB should stop storing a full Claude Code binary version
cache inside every managed Claude agent home.

It complements the Claude isolation authority in
[docs/claude-session-isolation-contract.md](/home/bfly/yunwei/ccb_source/docs/claude-session-isolation-contract.md).
That contract remains authoritative for conversation, auth, config, and
session-state isolation. This plan narrows only the provider binary/cache
placement problem.

This is a Claude-specific child plan of
[docs/ccb-provider-state-storage-boundary-plan.md](/home/bfly/yunwei/ccb_source/docs/ccb-provider-state-storage-boundary-plan.md).
The general storage boundary plan is authoritative for cross-provider storage
classes, `.ccb/provider-profiles` semantics, shared cache placement, and
diagnostics/cleanup sequencing.

## 2. Current Problem

Managed Claude launches set `HOME` to the agent-scoped provider-state home:

```text
.ccb/agents/<agent>/provider-state/claude/home/
```

This is necessary because Claude Code does not expose a stable dedicated
`CLAUDE_HOME` flag and reads important runtime state from `HOME`.

However, Claude Code also stores its user-level executable version cache under
that same home:

```text
<HOME>/.local/share/claude/versions/<version>
<HOME>/.local/bin/claude -> ../share/claude/versions/<current-version>
```

In a CCB managed home, that becomes:

```text
.ccb/agents/<agent>/provider-state/claude/home/.local/share/claude/versions/
```

Observed local example:

```text
2.1.132  ~249 MB
2.1.133  ~230 MB
2.1.137  ~231 MB
```

Only `2.1.137` is the current symlink target, but the older binaries remain in
the agent provider-state tree. This turns provider-state into a durable binary
cache and can make one Claude agent consume hundreds of MB.

This is a side effect of CCB's private-`HOME` isolation strategy. CCB is not
intentionally treating Claude binaries as project authority. Because Claude Code
uses `$HOME/.local/...` for its self-managed executable cache, changing `HOME`
for session isolation also changes the binary cache location.

That coupling is undesirable:

- session/config/auth isolation is project and agent scoped
- executable binaries and self-update caches are tool/runtime artifacts
- tool binaries should normally be user-level, system-level, or shared CCB cache
  resources, not per-project and per-agent durable state

## 3. Design Boundary

CCB must keep these categories separate:

- **Agent-isolated authority**
  - `.claude/projects/`
  - `.claude/session-env/`
  - `.claude/settings.json`
  - `.claude/skills/`, `.claude/commands/`, `.claude/CLAUDE.md`
  - `.claude.json`
- **Agent-local secret**
  - `.claude/.credentials.json`
  - `.config/claude-code/auth.json`
- **Shared or cleanable provider binary/cache**
  - `.local/share/claude/versions/`
  - `.local/bin/claude`
  - other Claude self-update binaries that do not define conversation identity

The version cache is not Claude conversation authority. It should not be copied
into diagnostics as session evidence, and it should not be duplicated per
agent unless the user explicitly requests fully self-contained managed homes.

The intended boundary is:

- CCB may set private `HOME` to isolate Claude conversation state.
- CCB should not let that private `HOME` make provider binaries project-owned.
- If Claude insists on `$HOME/.local/...`, CCB must either prune that cache
  conservatively or redirect it through a verified shared-cache strategy.

## 4. Target State

Default managed Claude launches should still use an agent-scoped private
`HOME`, but the Claude executable/version cache should resolve outside that
private home.

Preferred target:

```text
~/.cache/ccb/projects/<project-id-prefix>/provider-cache/
  claude/
    versions/
      <version>

.ccb/agents/<agent>/provider-state/claude/home/
  .local/share/claude/versions -> ~/.cache/ccb/.../claude/versions
  .local/bin/claude -> ../share/claude/versions/<current-version>/claude
```

or, if CCB can safely rely on the user installation:

```text
~/.local/bin/claude
~/.local/share/claude/versions/
```

The managed agent home should contain only a shim or no `.local/share/claude`
tree at all.

## 5. Implementation Phases

### Phase 0 - Audit And Classification

Add a read-only storage inspection path before deleting anything.

Required behavior:

- report `provider-state/claude/home/.local/share/claude/versions/*`
- identify the current symlink target from `.local/bin/claude`
- classify non-target versions as `cleanable_binary_cache`
- classify the target version as `active_binary_cache`
- include parent-plan metadata such as `reachable_from_current_symlink`,
  `is_active_version`, and `reclaimable`
- surface projected auth/config/session files separately as authority
  or secret according to the parent storage-boundary plan

Suggested command surface:

```text
ccb doctor storage
```

Exit criteria:

- users can see how much disk is binary cache vs. session authority
- no file deletion occurs in this phase

### Phase 1 - Safe Prune Policy

Add conservative pruning for per-agent Claude version caches.

Policy:

- never delete the current `.local/bin/claude` symlink target
- delete older versions only when they are regular files under the managed
  Claude home
- skip pruning if the symlink target cannot be resolved safely
- report and skip if the `versions/` directory itself is a symlink
- cleanup must follow the parent storage-boundary plan's lifecycle guard: do
  not prune while the backend is active or pending `ask` jobs exist, and hold
  the project startup/lifecycle lock while checking and pruning

Suggested command surface:

```text
ccb cleanup
```

Exit criteria:

- current Claude still launches after cleanup
- repeated cleanup is idempotent
- corrupted or unexpected symlink layouts are reported, not force-deleted

### Phase 2 - Shared Binary Cache

Move Claude binary/version storage out of each agent home.

Approach:

1. Resolve the real account source Claude executable using the source home or
   `PATH`.
2. Create a CCB project-scoped user cache under
   `~/.cache/ccb/projects/<project-id-prefix>/provider-cache/claude/`.
3. In each managed Claude home, ensure `.local/bin/claude` points to the shared
   cache executable or force the launch command to use the shared executable
   path directly.
4. Ensure the managed `HOME` still points to the agent home for `.claude/*`
   isolation.

Important constraint:

- do not share `.claude/projects`, `.claude/session-env`, settings, auth, or
  trust files
- only share executable/version cache material

Exit criteria:

- two Claude agents do not duplicate `versions/<version>` under their
  provider-state homes
- Claude conversations remain isolated by managed home
- removing one agent does not remove the shared executable needed by another
  agent

### Phase 3 - Startup Guard

Prevent future binary-cache drift back into provider-state.

Required behavior:

- on managed Claude startup, detect
  `<managed-home>/.local/share/claude/versions`
- if shared cache is enabled, move or prune it according to Phase 1/2 policy
- emit a diagnostics notice when Claude writes binary cache into provider-state
  again
- never let this notice affect ask/job completion semantics

Exit criteria:

- normal restarts do not steadily grow `.ccb/agents/<agent>/provider-state`
  with old Claude binaries

## 6. Risk Analysis

### Claude CLI May Require HOME-Local Binaries

If Claude Code hardcodes `$HOME/.local/share/claude/versions`, direct sharing may
not be enough. In that case, keep Phase 1 pruning as the default safe fix and
make Phase 2 opt-in until verified on Linux, macOS, and WSL.

### Symlink Safety

Cleanup must never follow arbitrary symlinks outside the expected managed home
or shared cache. Only delete normalized paths that are direct children of the
managed `versions/` directory.

### macOS Login Compatibility

macOS may materialize auth from Keychain into the managed home. Binary cache
dedup must not touch `.claude/.credentials.json` or
`.config/claude-code/auth.json`.

### Diagnostics Semantics

Binary caches are not diagnostic evidence by default. Diagnostic bundles should
record their presence and size, but should not export hundreds of MB of version
binaries.

## 7. Tests

Required unit tests:

- detects current Claude version symlink target
- classifies current vs. old version files
- prune keeps only versions currently referenced by managed Claude homes
- prune refuses unsafe symlink targets
- shared-cache launch still writes `HOME=<managed-home>`
- shared-cache launch does not share `.claude/projects`
- storage classification marks `.claude/.credentials.json` and
  `.config/claude-code/auth.json` as secret, not cache or projected config

Required integration tests:

- Linux managed Claude startup after prune
- macOS managed Claude startup after prune
- WSL managed Claude startup after prune
- two Claude agents share binary cache but keep separate session roots

## 8. Recommended First Slice

Implement Phase 0 and Phase 1 first.

Reason:

- they solve the immediate disk-growth problem
- they do not change Claude startup semantics
- they are safer than redirecting Claude's self-update path before cross-platform
  verification

Only after Phase 1 is stable should CCB attempt the shared-cache startup path in
Phase 2.

## 9. Current Implementation Status

Implemented:

- Parent storage classification reports Claude
  `.local/share/claude/versions/*` as `REBUILDABLE_CACHE`.
- The current `.local/bin/claude` symlink is surfaced as the active entry, and
  files inside the current version subtree are marked with
  `is_active_version` and `reachable_from_current_symlink`.
- Claude auth files classify as `SECRET`.
- Claude `.claude.json` classifies as session/trust authority.
- Claude no longer accepts provider-profile `runtime_home` as a supported
  launch boundary; managed launches keep `HOME` under
  `.ccb/agents/<agent>/provider-state/claude/home`.
- `ccb cleanup` prunes old per-agent Claude version caches while keeping the
  current symlink target.
- `ccb cleanup` reports symlinked `versions/` directories instead of silently
  ignoring them.
- Managed Claude startup preparation records a de-duplicated
  `claude_binary_cache_drift` agent event when a per-agent `versions/` cache
  appears, so diagnostics can explain why provider-state is growing again.
- Managed Claude startup preparation now routes
  `.local/share/claude/versions` to
  `~/.cache/ccb/projects/<project-id-prefix>/provider-cache/claude/versions`.
  Existing per-agent version directories, and legacy symlinks that pointed at
  `.ccb/shared-cache/claude/versions`, are copied into the external cache and
  replaced with a marked symlink when every entry is a recognizable Claude
  version cache. Unknown entries are left untouched and continue to emit the
  drift event.
- `ccb cleanup` prunes the external Claude `versions/` cache, legacy
  `.ccb/shared-cache/claude/versions`, and legacy per-agent `versions/`
  directories. A shared cache keeps only versions referenced by managed homes
  that actually point at that cache; unreferenced legacy shared caches are
  removed after external-cache migration.
- `ccb cleanup` removes rebuildable Claude cache residue from managed homes:
  `.cache/claude`, `.npm/_logs`, `.claude/cache`, `.claude/telemetry`,
  `.claude/paste-cache`, and `.claude/plugins/marketplaces`.

Not implemented yet:

- Active startup guard that redirects or removes future per-agent binary-cache
  drift after Claude recreates an unrecognized local versions directory.

The next Claude-specific step is real launch validation on Linux, macOS, and
WSL to confirm Claude Code continues to update through the symlinked
`versions/` directory.
