from __future__ import annotations

from .ensure_context import load_namespace_context, refresh_session_liveness
from .ensure_identity import prepare_namespace_root_pane
from .ensure_state import (
    build_created_namespace,
    force_recreate_namespace,
    persist_refreshed_namespace,
    recreate_for_layout_change,
)
from .materialize_topology import (
    existing_topology_agent_panes,
    materialize_topology,
    refresh_topology_ui_for_project,
    topology_active_panes,
    topology_recreate_reason,
)


def ensure_project_namespace(
    controller,
    *,
    layout_signature: str | None = None,
    topology_plan=None,
    force_recreate: bool = False,
    recreate_reason: str | None = None,
    session_probe_timeout_s: float | None = None,
    terminal_size: tuple[int, int] | None = None,
) -> object:
    controller._layout.ccbd_dir.mkdir(parents=True, exist_ok=True)
    context = load_namespace_context(
        controller,
        layout_signature=layout_signature,
        topology_plan=topology_plan,
        recreate_reason=recreate_reason,
    )
    context = refresh_session_liveness(
        controller,
        context,
        timeout_s=session_probe_timeout_s,
    )

    if force_recreate:
        context = force_recreate_namespace(controller, context)
    context = recreate_for_layout_change(controller, context)
    if topology_plan is not None and context.session_is_alive and context.current is not None:
        reason = topology_recreate_reason(controller, context, topology_plan=topology_plan)
        if reason is not None:
            context = force_recreate_namespace(
                controller,
                context.with_updates(recreate_cause=reason),
            )

    if context.session_is_alive and context.current is not None:
        if topology_plan is not None:
            agent_panes = existing_topology_agent_panes(controller, context, topology_plan=topology_plan)
            refresh_topology_ui_for_project(
                controller,
                context,
                topology_plan=topology_plan,
                timeout_s=session_probe_timeout_s,
            )
            setattr(controller, '_last_materialized_agent_panes', agent_panes)
            setattr(
                controller,
                '_last_topology_active_panes',
                topology_active_panes(controller, context, topology_plan=topology_plan),
            )
        else:
            setattr(controller, '_last_materialized_agent_panes', {})
            setattr(controller, '_last_topology_active_panes', ())
        return persist_refreshed_namespace(
            controller,
            context,
            timeout_s=session_probe_timeout_s,
        )

    epoch = context.current.namespace_epoch + 1 if context.current is not None else 1
    if topology_plan is not None:
        agent_panes = materialize_topology(
            controller,
            context,
            topology_plan=topology_plan,
            epoch=epoch,
            terminal_size=terminal_size,
            timeout_s=session_probe_timeout_s,
        )
        setattr(controller, '_last_materialized_agent_panes', agent_panes)
        setattr(
            controller,
            '_last_topology_active_panes',
            topology_active_panes(controller, context, topology_plan=topology_plan),
        )
    else:
        prepare_namespace_root_pane(
            controller,
            context,
            epoch=epoch,
            terminal_size=terminal_size,
            timeout_s=session_probe_timeout_s,
        )
        setattr(controller, '_last_materialized_agent_panes', {})
        setattr(controller, '_last_topology_active_panes', ())
    return build_created_namespace(
        controller,
        context,
        timeout_s=session_probe_timeout_s,
    )


__all__ = ['ensure_project_namespace']
