from __future__ import annotations

from pathlib import Path
import queue
import threading

from .lifecycle import listen_server, shutdown_server
from .loop import serve_forever as serve_forever_impl, stop_maintenance_worker, stop_worker
from .protocol import handle_connection


_CONNECTION_QUEUE_MAXSIZE = 128


class CcbdSocketServer:
    _MUTATING_OPS = frozenset({
        'submit',
        'cancel',
        'attach',
        'start',
        'restore',
        'ack',
        'resubmit',
        'retry',
        'comms_recover',
        'project_restart_panes',
        'stop-all',
    })

    def __init__(self, socket_path: str | Path) -> None:
        self._socket_path = Path(socket_path)
        self._handlers: dict[str, callable] = {}
        self._request_guard = None
        self._server = None
        self._connection_queue = queue.Queue(maxsize=_CONNECTION_QUEUE_MAXSIZE)
        self._worker_sentinel = object()
        self._worker_thread: threading.Thread | None = None
        self._maintenance_thread: threading.Thread | None = None
        self._worker_error: BaseException | None = None
        self._bound_socket_stat: tuple[int, int] | None = None
        self._maintenance_state_lock = threading.Lock()
        self._after_response_actions: list[callable] = []
        self._pending_maintenance_ticks = 0
        self._maintenance_pending_event = threading.Event()
        self._stop_event = threading.Event()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def register_handler(self, op: str, handler) -> None:
        if op in self._handlers:
            raise ValueError(f'duplicate handler for op {op!r}')
        self._handlers[op] = handler

    def set_request_guard(self, guard) -> None:
        self._request_guard = guard

    def listen(self) -> None:
        listen_server(self)

    def serve_forever(self, *, poll_interval: float = 0.2, on_tick=None) -> None:
        serve_forever_impl(self, poll_interval=poll_interval, on_tick=on_tick)

    def request_shutdown(self) -> None:
        shutdown_server(self)

    def shutdown(self) -> None:
        self.request_shutdown()
        stop_worker(self)
        stop_maintenance_worker(self)

    def request_maintenance_ticks(self, handled_op: str | None) -> int:
        if handled_op not in self._MUTATING_OPS:
            return 0
        return 1

    def queue_maintenance_ticks(self, count: int) -> None:
        if count > 0:
            with self._maintenance_state_lock:
                # Post-request maintenance is a dirty signal, not a counted work queue.
                self._pending_maintenance_ticks = 1
                self._maintenance_pending_event.set()
                pending_ticks = self._pending_maintenance_ticks
            self._record_pending_maintenance_ticks_value(pending_ticks)

    def queue_periodic_maintenance_tick(self) -> None:
        self.queue_maintenance_ticks(1)

    def take_queued_maintenance_ticks(self) -> int:
        with self._maintenance_state_lock:
            count = self._pending_maintenance_ticks
            self._pending_maintenance_ticks = 0
            if not self._after_response_actions:
                self._maintenance_pending_event.clear()
            pending_ticks = self._pending_maintenance_ticks
        self._record_pending_maintenance_ticks_value(pending_ticks)
        return count

    def queue_post_request_maintenance(self, handled_op: str | None) -> None:
        self.queue_maintenance_ticks(self.request_maintenance_ticks(handled_op))

    def take_pending_maintenance_ticks(self) -> int:
        return max(0, int(self.take_queued_maintenance_ticks()))

    def maintenance_pending(self) -> bool:
        with self._maintenance_state_lock:
            return self._pending_maintenance_ticks > 0 or bool(self._after_response_actions)

    def queue_after_response_action(self, action) -> None:
        if callable(action):
            with self._maintenance_state_lock:
                self._after_response_actions.append(action)
                self._maintenance_pending_event.set()

    def pop_after_response_actions(self) -> tuple[callable, ...]:
        with self._maintenance_state_lock:
            actions = tuple(self._after_response_actions)
            self._after_response_actions.clear()
            if self._pending_maintenance_ticks <= 0:
                self._maintenance_pending_event.clear()
        return actions

    def _record_pending_maintenance_ticks_value(self, value: int) -> None:
        callback = getattr(self, '_record_pending_maintenance_ticks', None)
        if callable(callback):
            try:
                callback(value)
            except Exception:
                pass

    def _handle_connection(self, conn) -> str | None:
        return handle_connection(self, conn)


__all__ = ['CcbdSocketServer']
