from __future__ import annotations

from heartbeat import HeartbeatAction, evaluate_heartbeat
from mailbox_runtime.targets import known_mailbox_targets, normalize_mailbox_target

from .common import heartbeat_diagnostics, heartbeat_timeout_decision, snapshot_is_terminal
from .models import HeartbeatTickContext


def tick_job_heartbeat(service, dispatcher, job) -> bool:
    context = build_heartbeat_tick_context(service, dispatcher, job)
    if context is None:
        return False
    if context.decision.action is HeartbeatAction.RESET:
        return handle_reset_heartbeat(service, dispatcher, job, context)
    if not context.decision.notice_due:
        return True
    if heartbeat_timeout_due(service, context):
        return terminalize_heartbeat_timeout(service, dispatcher, job, context)
    return record_internal_heartbeat(service, dispatcher, job, context)


def build_heartbeat_tick_context(service, dispatcher, job) -> HeartbeatTickContext | None:
    snapshot = dispatcher.get_snapshot(job.job_id)
    if snapshot_is_terminal(snapshot):
        service._store.remove(service._subject_kind, job.job_id)
        return None
    observed_last_progress_at = (
        str(snapshot.updated_at).strip()
        if snapshot is not None and str(snapshot.updated_at).strip()
        else str(job.updated_at).strip()
    )
    if not observed_last_progress_at:
        return None
    prior_state = service._store.load(service._subject_kind, job.job_id)
    now = service._clock()
    next_state, decision = evaluate_heartbeat(
        policy=service._policy,
        subject_kind=service._subject_kind,
        subject_id=job.job_id,
        owner=job.agent_name,
        observed_last_progress_at=observed_last_progress_at,
        now=now,
        state=prior_state,
    )
    return HeartbeatTickContext(
        snapshot=snapshot,
        observed_last_progress_at=observed_last_progress_at,
        now=now,
        next_state=next_state,
        decision=decision,
    )


def handle_reset_heartbeat(service, dispatcher, job, context: HeartbeatTickContext) -> bool:
    service._store.save(context.next_state)
    dispatcher._append_event(
        job,
        'job_heartbeat_reset',
        {
            'subject_kind': service._subject_kind,
            'action': context.decision.action.value,
            'notice_count': context.decision.notice_count,
            'last_progress_at': context.decision.last_progress_at,
        },
        timestamp=context.now,
    )
    return True


def heartbeat_timeout_due(service, context: HeartbeatTickContext) -> bool:
    limit = getattr(service, '_terminal_notice_count', None)
    return limit is not None and int(context.decision.notice_count) >= int(limit)


def record_internal_heartbeat(service, dispatcher, job, context: HeartbeatTickContext) -> bool:
    mailbox_target = normalize_mailbox_target(
        job.request.from_actor,
        known_targets=known_mailbox_targets(dispatcher._config),
    )
    diagnostics = heartbeat_diagnostics(
        job,
        decision=context.decision,
        snapshot=context.snapshot,
        mailbox_target=mailbox_target,
        subject_kind=service._subject_kind,
    )
    service._store.save(context.next_state)
    dispatcher._append_event(
        job,
        'job_heartbeat_observed',
        diagnostics,
        timestamp=context.now,
    )
    return True


def terminalize_heartbeat_timeout(service, dispatcher, job, context: HeartbeatTickContext) -> bool:
    diagnostics = heartbeat_diagnostics(
        job,
        decision=context.decision,
        snapshot=context.snapshot,
        mailbox_target=normalize_mailbox_target(
            job.request.from_actor,
            known_targets=known_mailbox_targets(dispatcher._config),
        ),
        subject_kind=service._subject_kind,
    )
    service._store.save(context.next_state)
    dispatcher._append_event(
        job,
        'job_heartbeat_timeout',
        diagnostics,
        timestamp=context.now,
    )
    dispatcher.complete(
        job.job_id,
        heartbeat_timeout_decision(
            job,
            decision=context.decision,
            snapshot=context.snapshot,
            finished_at=context.now,
        ),
    )
    service._store.remove(service._subject_kind, job.job_id)
    return False


__all__ = [
    'build_heartbeat_tick_context',
    'heartbeat_timeout_due',
    'handle_reset_heartbeat',
    'record_internal_heartbeat',
    'terminalize_heartbeat_timeout',
    'tick_job_heartbeat',
]
