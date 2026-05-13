# Useful Tools

This directory contains optional tools that are useful for CCB users but are not
installed by default.

The default installer intentionally keeps provider skills minimal. Copy tools
from this directory into the agent or provider home where you want them enabled.

## Skill Sets

Provider-specific skill sets live under:

- `useful_tools/codex_skills/`
- `useful_tools/claude_skills/`

To enable a skill for all future Codex-managed agents that inherit your global
Codex skills:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R useful_tools/codex_skills/plan-tree "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Then restart or relaunch the managed CCB agent so its isolated Codex home can
inherit the skill.

To enable a skill for all future Claude-managed agents that inherit your global
Claude skills:

```bash
mkdir -p "$HOME/.claude/skills"
cp -R useful_tools/claude_skills/plan-tree "$HOME/.claude/skills/"
```

Then restart or relaunch the managed CCB agent so its isolated Claude home can
inherit the skill.

To enable a skill for one already-mounted managed Codex agent:

```bash
mkdir -p .ccb/agents/<agent>/provider-state/codex/home/skills
cp -R useful_tools/codex_skills/plan-tree .ccb/agents/<agent>/provider-state/codex/home/skills/
```

To enable a skill for one already-mounted managed Claude agent:

```bash
mkdir -p .ccb/agents/<agent>/provider-state/claude/home/.claude/skills
cp -R useful_tools/claude_skills/plan-tree .ccb/agents/<agent>/provider-state/claude/home/.claude/skills/
```

Replace `<agent>` with the configured CCB agent name.

## Included Tools

- `plan-tree`: Maintain linked planning document trees containing roadmaps,
  topic notes, decision records, and open questions.
