# ccb-agent-sidebar

CCB-native tmux sidebar for rendering `ccbd` ProjectView.

This crate is intentionally not a generic tmux scanner. It talks to the project
`ccbd` Unix socket and treats `ProjectView` as the only UI authority.

Phase 1 launch shape:

```text
ccb-agent-sidebar --ccbd-socket <path> --project-root <path> --pane-window <name>
```

Keyboard controls:

- `q` / `Esc`: exit the sidebar process only.
- `R`: restart every configured agent pane through ccbd without detaching the current tmux session.
- `Q`: run `ccb kill` from the project root.

The top panel shows inline controls on the right side of the `Sidebar` title bar:

- `↻`: restart every configured project pane (`R`)
- `×`: kill project (`Q`)

The sidebar does not enable terminal mouse capture by default so tmux copy-mode
mouse dragging remains available. Project tmux bindings translate clicks on the
inline controls into the same `R` and `Q` key actions.

Upstream inspiration and future UI component migration come from
`hiroppy/tmux-agent-sidebar`; its MIT license is retained in `LICENSE.upstream`.
