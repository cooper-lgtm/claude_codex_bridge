from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from textwrap import dedent


AuxiliaryHandler = Callable[[Sequence[str]], int]
ManagementHandler = Callable[[argparse.Namespace], int]

_MANAGEMENT_COMMANDS = {"update", "version", "uninstall", "reinstall"}


def dispatch_auxiliary_command(
    argv: Sequence[str],
    *,
    droid_handler: AuxiliaryHandler,
) -> int | None:
    tokens = list(argv)
    if tokens and tokens[0] == "droid" and len(tokens) > 1 and tokens[1] in {"setup-delegation", "test-delegation"}:
        return droid_handler(tokens[1:])
    return None


def dispatch_management_command(
    argv: Sequence[str],
    *,
    update_handler: ManagementHandler,
    version_handler: ManagementHandler,
    uninstall_handler: ManagementHandler,
    reinstall_handler: ManagementHandler,
) -> int | None:
    tokens = list(argv)
    if not tokens or tokens[0] not in _MANAGEMENT_COMMANDS:
        return None

    parser = _build_management_parser()
    args = parser.parse_args(tokens)
    if args.command == "update":
        return update_handler(args)
    if args.command == "version":
        return version_handler(args)
    if args.command == "uninstall":
        return uninstall_handler(args)
    if args.command == "reinstall":
        return reinstall_handler(args)
    parser.print_help()
    return 1


def parse_start_args(argv: Sequence[str]) -> argparse.Namespace:
    return build_start_parser().parse_args(list(argv))


def print_start_help(*, file=None) -> None:
    print(
        dedent(
            """
            usage: ccb [-s] [-n]

            Primary workflow:
              ccb                  Start project agents from `.ccb/ccb.config`.
              ccb -s               Safe start. Disable CLI auto-permission override.
              ccb -n               Rebuild .ccb except ccb.config, then start fresh.
              ccb kill             Stop the current project's background runtime.
              ccb kill -f          Force cleanup project-owned runtime residue.
              ccb cleanup          Prune safe provider rebuildable caches after ccbd is stopped.

            Core commands:
              ccb ask <agent> [from <sender>] <message>
              ccb doctor

            Secondary control-plane status:
              ccb ping <agent|ccbd>

            Supplementary observer:
              ccb pend <agent|job_id> [N]
              ccb pend --watch <agent|job_id>
              ccb pend --inbox [--detail] <agent>
              ccb pend --queue [--detail] <agent|all>

            Advanced views:
              ccb queue [--detail] <agent|all>
              ccb trace <id>

            Advanced recovery:
              ccb repair <ack|retry|resubmit> ...

            Management:
              ccb version | ccb update | ccb uninstall | ccb reinstall
            """
        ).strip(),
        file=file,
    )


def print_kill_help(*, file=None) -> None:
    print(
        dedent(
            """
            usage: ccb kill [-f]

            Project runtime cleanup:
              ccb kill     Stop the current project's ccbd, agents, and tmux namespace.
              ccb kill -f  Force cleanup project-owned runtime residue before `ccb -n`.

            Notes:
              - `kill` is project-scoped. It does not bootstrap a missing `.ccb`.
              - `kill` still works when `.ccb` exists but `ccb.config` is missing or stale.
              - Use `ccb -n` after `ccb kill` when you want to rebuild `.ccb` but keep `ccb.config`.
            """
        ).strip(),
        file=file,
    )


def print_command_help(command_name: str, *, file=None) -> bool:
    text = _COMMAND_HELP.get(command_name)
    if text is None:
        return False
    print(dedent(text).strip(), file=file)
    return True


_COMMAND_HELP = {
    "ping": """
        usage: ccb ping <agent|all|ccbd>

        Light control-plane status:
          ccb ping <agent>   Show cached runtime status for one named agent.
          ccb ping all       Show cached mounted-agent status across the project.
          ccb ping ccbd      Show cached project daemon status.
    """,
    "pend": """
        usage: ccb pend [--watch|--inbox|--queue] [--detail] <agent|job_id|all> [N]

        Weak observer surface:
          Primary weak observer entrypoint:
            ccb pend <agent>                    Show a non-authoritative observer snapshot for one agent.
            ccb pend <job_id>                   Show a non-authoritative observer snapshot for one submitted job.
            ccb pend --watch <agent|job_id>     Stream non-authoritative observer events via the converged observer entrypoint.
            ccb pend --inbox <agent>            Show a non-authoritative inbox summary via the converged observer entrypoint.
            ccb pend --inbox --detail <agent>   Expand inbox-item detail via the converged observer entrypoint.
            ccb pend --queue <agent|all>        Show the same non-authoritative backlog summary exposed by `ccb queue`.
            ccb pend --queue --detail <agent>   Expand queued-event detail through the observer entrypoint.
            ccb pend <target> N                 Show the latest N observer snapshot items.
          Prefer `ccb ask --wait` / `ccb ask wait <job_id>` for terminal results and `ccb trace <id>` for lineage.
    """,
    "watch": """
        usage: ccb watch <agent|job_id>

        Weak observer compatibility entrypoint:
          ccb watch <agent>   Stream non-authoritative observer events for one agent.
          ccb watch <job_id>  Stream non-authoritative observer events for one job until terminal completion or timeout.
          Prefer `ccb pend --watch <agent|job_id>` as the converged observer entrypoint.
          Do not treat non-terminal watch output as authoritative completion.
          Prefer `ccb ask --wait` / `ccb ask wait <job_id>` for terminal results and `ccb trace <id>` for lineage.
    """,
    "queue": """
        usage: ccb queue [--detail] <agent_name|all>

        Advanced backlog view:
          ccb queue <agent_name>            Show a non-authoritative observer summary for one agent.
          ccb queue --detail <agent_name>   Expand queued-event details for one agent.
          ccb queue all                     Show non-authoritative observer backlog state across the project.
          `ccb pend --queue [--detail] <agent|all>` remains the equivalent weak-observer form.
          Prefer `ccb ask --wait` / `ccb ask wait <job_id>` for terminal results and `ccb trace <id>` for lineage.
    """,
    "trace": """
        usage: ccb trace <submission_id|message_id|attempt_id|reply_id|job_id>

        Advanced lineage view:
          ccb trace <id>   Show the full job/message/reply lineage for one id.
    """,
    "inbox": """
        usage: ccb inbox [--detail] <agent_name>

        Weak observer compatibility entrypoint:
          ccb inbox <agent_name>            Show a non-authoritative observer summary for one agent.
          ccb inbox --detail <agent_name>   Expand inbox-item detail for one agent.
          Prefer `ccb pend --inbox [--detail] <agent>` as the converged observer entrypoint.
          Prefer `ccb ask --wait` / `ccb ask wait <job_id>` for terminal results and `ccb trace <id>` for lineage.
    """,
    "logs": """
        usage: ccb logs <agent>

        Runtime diagnostics compatibility view:
          ccb logs <agent>   Tail the current runtime/session log for one agent.
          Prefer `ccb doctor logs <agent>` as the converged diagnostics entrypoint.
    """,
    "doctor-logs": """
        usage: ccb doctor logs <agent>

        Runtime log diagnostics subview:
          ccb doctor logs <agent>   Tail the current runtime/session log for one agent through the primary diagnostics entrypoint.
          `ccb logs <agent>` remains a compatibility alias.
    """,
    "ps": """
        usage: ccb ps

        Runtime diagnostics compatibility view:
          ccb ps   Show known runtime/session/workspace bindings.
          Prefer `ccb doctor ps` as the converged diagnostics entrypoint.
    """,
    "doctor-ps": """
        usage: ccb doctor ps

        Runtime diagnostics subview:
          ccb doctor ps   Show known runtime/session/workspace bindings through the primary diagnostics entrypoint.
          `ccb ps` remains a compatibility alias.
    """,
    "doctor-storage": """
        usage: ccb doctor storage [--json]

        Storage diagnostics subview:
          ccb doctor storage        Show .ccb storage class totals and largest entries.
          ccb doctor storage --json Emit full storage classification payload.
    """,
    "cleanup": """
        usage: ccb cleanup

        Storage cleanup:
          ccb cleanup   Prune safe provider rebuildable caches after ccbd is stopped.

        Safety:
          - Refuses to run while ccbd is active or ask jobs are pending/running.
          - Keeps Claude versions currently referenced by managed homes.
          - Does not remove provider sessions, auth, plugin bundles, mailbox data, or runtime authority.
          - Use `ccb doctor storage` before cleanup to inspect storage classes.
    """,
    "doctor": """
        usage: ccb doctor [ps|logs <agent>|storage] [--output [PATH]]

        Deep diagnostics:
          ccb doctor               Print project diagnostic summary.
          ccb doctor ps            Show the runtime/session/workspace diagnostics subview.
          ccb doctor logs <agent>  Tail the runtime/session log diagnostics subview for one agent.
          ccb doctor storage       Show .ccb storage class totals.
          ccb doctor --output      Export a support bundle to the default path.
          ccb doctor --output PATH Export a support bundle to PATH.
          `ccb ps` and `ccb logs <agent>` remain compatibility entrypoints.
    """,
    "cancel": """
        usage: ccb cancel <job_id>

        Job control view:
          ccb cancel <job_id>   Request cancellation for one submitted job.
    """,
    "ack": """
        usage: ccb ack <agent_name> [inbound_event_id]

        Advanced recovery compatibility entrypoint:
          ccb ack <agent_name> [inbound_event_id]   Acknowledge reply/inbox progress for one agent.
          Prefer `ccb repair ack <agent_name> [inbound_event_id]` as the converged recovery entrypoint.
    """,
    "repair-ack": """
        usage: ccb repair ack <agent_name> [inbound_event_id]

        Advanced recovery subcommand:
          ccb repair ack <agent_name> [inbound_event_id]   Acknowledge reply/inbox progress for one agent.
          `ccb ack <agent_name> [inbound_event_id]` remains a compatibility alias.
    """,
    "retry": """
        usage: ccb retry <job_id|attempt_id>

        Advanced recovery compatibility entrypoint:
          ccb retry <job_id|attempt_id>   Retry one failed or incomplete job/attempt lineage.
          Prefer `ccb repair retry <job_id|attempt_id>` as the converged recovery entrypoint.
    """,
    "repair-retry": """
        usage: ccb repair retry <job_id|attempt_id>

        Advanced recovery subcommand:
          ccb repair retry <job_id|attempt_id>   Retry one failed or incomplete job/attempt lineage.
          `ccb retry <job_id|attempt_id>` remains a compatibility alias.
    """,
    "resubmit": """
        usage: ccb resubmit <message_id>

        Advanced recovery compatibility entrypoint:
          ccb resubmit <message_id>   Create a fresh submission from one prior message lineage.
          Prefer `ccb repair resubmit <message_id>` as the converged recovery entrypoint.
    """,
    "repair-resubmit": """
        usage: ccb repair resubmit <message_id>

        Advanced recovery subcommand:
          ccb repair resubmit <message_id>   Create a fresh submission from one prior message lineage.
          `ccb resubmit <message_id>` remains a compatibility alias.
    """,
    "repair": """
        usage: ccb repair <ack|retry|resubmit> ...

        Advanced recovery:
          ccb repair ack <agent_name> [inbound_event_id]   Acknowledge reply/inbox progress for one agent.
          ccb repair retry <job_id|attempt_id>             Retry one failed or incomplete job/attempt lineage.
          ccb repair resubmit <message_id>                 Create a fresh submission from one prior message lineage.
          Legacy `ack` / `retry` / `resubmit` commands remain compatibility entrypoints.
    """,
    "config": """
        usage: ccb config validate

        Config validation:
          ccb config validate   Validate `.ccb/ccb.config` for the current project.
    """,
}


def _build_management_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccb", description="Claude AI unified launcher", add_help=True)
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    update_parser = subparsers.add_parser("update", help="Update to latest or specified version")
    update_parser.add_argument("target", nargs="?", help="version like '4', '4.1', '4.1.3'")

    subparsers.add_parser("version", help="Show version and check for updates")
    subparsers.add_parser("uninstall", help="Uninstall ccb and clean configs")
    subparsers.add_parser("reinstall", help="Reinstall ccb and refresh configs")
    return parser


def build_start_parser() -> argparse.ArgumentParser:
    start_parser = argparse.ArgumentParser(
        prog="ccb",
        description="Claude AI unified launcher",
        add_help=False,
    )
    start_parser.add_argument("-s", "--safe", action="store_true", default=False, help=argparse.SUPPRESS)
    start_parser.add_argument(
        "-n",
        "--new-context",
        dest="new_context",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return start_parser
