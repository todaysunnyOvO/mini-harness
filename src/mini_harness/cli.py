from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from .agent import Agent, AgentLoopError
from .config import ConfigError, load_config
from .context_budget import ContextBudgetPolicy
from .context_compression import CompressionManager, CompressionPolicy
from .events import JsonlEventSink
from .memory import MemoryStore, render_memory_snapshot
from .provider import OpenAICompatibleProvider, ProviderError, ToolCall
from .session_store import RecoveryReport, SessionDB, SessionError
from .skills import (
    SkillCatalog,
    SkillError,
    SkillInvocation,
    render_skills_index,
)
from .tools import (
    ToolDefinition,
    ToolExecutionResult,
    build_file_registry,
    register_memory_tools,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-harness",
        description="Run the educational text-only Mini Harness agent.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--message",
        help="Send one message and exit instead of starting interactive mode.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without calling the provider.",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session",
        help="Use this session ID, creating it when it does not exist.",
    )
    session_group.add_argument(
        "--resume",
        help="Resume an existing session ID; fail if it does not exist.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List recent persistent sessions and exit.",
    )
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="List the metadata-only startup Skill index and exit.",
    )
    parser.add_argument(
        "--skill",
        metavar="NAME",
        help="Load one complete Skill for the --message turn.",
    )
    parser.add_argument(
        "--compress",
        nargs="?",
        const="",
        metavar="FOCUS",
        help=(
            "Compress the persistent history selected by --resume, optionally "
            "focusing the summary, then exit."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(
            f"Configuration OK: provider={config.provider.base_url} "
            f"model={config.provider.model}"
        )
        return 0
    if args.skill and args.message is None:
        print(
            "Configuration error: --skill requires --message",
            file=sys.stderr,
        )
        return 2
    if args.compress is not None and not args.resume:
        print(
            "Configuration error: --compress requires --resume",
            file=sys.stderr,
        )
        return 2
    if args.compress is not None and args.message is not None:
        print(
            "Configuration error: --compress cannot be combined with --message",
            file=sys.stderr,
        )
        return 2

    session_db: SessionDB | None = None
    try:
        if config.storage.enabled:
            session_db = SessionDB(config.storage.database_path)
        elif args.session or args.resume or args.list_sessions:
            print(
                "Configuration error: persistent storage is disabled",
                file=sys.stderr,
            )
            return 2

        if args.list_sessions:
            _print_sessions(session_db, profile_id=config.profile.id)
            return 0

        if args.resume and session_db is not None:
            if not session_db.session_exists(args.resume):
                print(f"Session not found: {args.resume}", file=sys.stderr)
                return 2

        selected_session = args.resume or args.session
        resuming_session = (
            selected_session is not None
            and session_db is not None
            and session_db.session_exists(selected_session)
        )
        event_sink = (
            JsonlEventSink(config.observability.event_log_path)
            if config.observability.enabled
            else None
        )
        skill_catalog = None
        if config.skills.enabled:
            skill_catalog = SkillCatalog(
                profile_root=config.skills.profile_root_path,
                profile_id=config.profile.id,
                external_dirs=config.skills.external_dirs,
                bundled_dir=config.skills.bundled_dir,
                optional_dir=config.skills.optional_dir,
                enabled_optional=config.skills.enabled_optional,
                max_skill_chars=config.skills.max_skill_chars,
                max_index_description_chars=(
                    config.skills.max_index_description_chars
                ),
            )
            if event_sink is not None:
                event_sink.emit(
                    "skills.index_built",
                    details={
                        "skill_count": len(skill_catalog.entries),
                        "conflict_count": len(skill_catalog.conflicts),
                        "diagnostic_count": len(skill_catalog.diagnostics),
                    },
                )
        if args.list_skills:
            _print_skills(skill_catalog)
            return 0

        requested_invocation = None
        if args.skill:
            if skill_catalog is None:
                print(
                    "Skill error: Skills are disabled",
                    file=sys.stderr,
                )
                return 2
            try:
                requested_invocation = skill_catalog.build_invocation(
                    args.skill,
                    args.message or "",
                )
            except SkillError as exc:
                print(f"Skill error: {exc}", file=sys.stderr)
                return 2

        system_prompt = config.agent.system_prompt
        memory_store = None
        if config.memory.enabled:
            memory_store = MemoryStore(
                config.memory.root_path,
                profile_id=config.profile.id,
                max_user_chars=config.memory.max_user_chars,
                max_memory_chars=config.memory.max_memory_chars,
                lock_timeout_seconds=config.memory.lock_timeout_seconds,
            )
            if not resuming_session:
                system_prompt = render_memory_snapshot(
                    system_prompt,
                    memory_store.snapshot(),
                )
        if skill_catalog is not None and not resuming_session:
            system_prompt = render_skills_index(
                system_prompt,
                skill_catalog.entries,
            )
        registry = build_file_registry(
            config.agent.workspace_root,
            auto_repair_names=config.tools.auto_repair_names,
            enable_write_file=config.tools.enable_write_file,
            max_result_chars=config.tools.max_result_chars,
            name_repair_threshold=config.tools.name_repair_threshold,
            timeout_seconds=config.tools.timeout_seconds,
            approval_callback=_approve_tool,
        )
        if memory_store is not None:
            register_memory_tools(registry, memory_store)
        main_provider = OpenAICompatibleProvider(config.provider)
        compression_manager = None
        if session_db is not None:
            summary_config = replace(
                config.provider,
                model=(
                    config.compression.summary_model
                    or config.provider.model
                ),
            )
            compression_manager = CompressionManager(
                session_db,
                OpenAICompatibleProvider(summary_config),
                policy=CompressionPolicy(
                    enabled=config.compression.enabled,
                    threshold_ratio=config.compression.threshold_ratio,
                    target_ratio=config.compression.target_ratio,
                    protect_first_turns=(
                        config.compression.protect_first_turns
                    ),
                    tail_tokens=config.compression.tail_tokens,
                    max_output_tokens=config.compression.max_output_tokens,
                    max_input_tokens=config.context.max_input_tokens,
                    reserved_output_tokens=(
                        config.context.reserved_output_tokens
                    ),
                    approximate_chars_per_token=(
                        config.context.approximate_chars_per_token
                    ),
                    abort_on_summary_failure=(
                        config.compression.abort_on_summary_failure
                    ),
                    cooldown_seconds=(
                        config.compression.cooldown_seconds
                    ),
                    lock_ttl_seconds=(
                        config.compression.lock_ttl_seconds
                    ),
                    min_savings_ratio=(
                        config.compression.min_savings_ratio
                    ),
                    anti_thrashing_limit=(
                        config.compression.anti_thrashing_limit
                    ),
                    in_place=config.compression.in_place,
                ),
            )
        agent = Agent(
            main_provider,
            system_prompt=system_prompt,
            tools=registry,
            max_iterations=config.agent.max_iterations,
            same_call_limit=config.tools.same_call_limit,
            max_calls_per_batch=config.tools.max_calls_per_batch,
            max_calls_per_turn=config.tools.max_calls_per_turn,
            session_db=session_db,
            session_id=selected_session,
            event_sink=event_sink,
            tool_observer=_print_tool_event,
            context_budget=(
                ContextBudgetPolicy(
                    max_input_tokens=config.context.max_input_tokens,
                    reserved_output_tokens=(
                        config.context.reserved_output_tokens
                    ),
                    approximate_chars_per_token=(
                        config.context.approximate_chars_per_token
                    ),
                    max_tool_result_tokens=(
                        config.context.max_tool_result_tokens
                    ),
                    recent_tool_tail_tokens=(
                        config.context.recent_tool_tail_tokens
                    ),
                    min_recent_turns=config.context.min_recent_turns,
                    compaction_target_ratio=(
                        config.context.compaction_target_ratio
                    ),
                )
                if config.context.enabled
                else None
            ),
            compression_manager=compression_manager,
            profile_id=config.profile.id,
        )
        if agent.session_id is not None:
            print(f"session> {agent.session_id}", file=sys.stderr)
        _print_recovery_report(agent.last_recovery_report)

        if args.compress is not None:
            result = agent.compress_context(args.compress, force=True)
            _print_compression_result(result)
            return 0 if result.status in {"committed", "no_middle"} else 1
        if args.message is not None:
            if requested_invocation is not None:
                return _run_once_with_skill(agent, requested_invocation)
            return _run_once(agent, args.message)
        return _run_interactive(agent, skill_catalog)
    except SessionError as exc:
        print(f"Session error: {exc}", file=sys.stderr)
        return 2
    finally:
        if session_db is not None:
            session_db.close()


def _run_once(agent: Agent, message: str) -> int:
    try:
        answer = agent.chat(message)
        print(answer)
        _print_turn_status(agent)
        return 0
    except (ProviderError, AgentLoopError, ValueError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


def _run_once_with_skill(agent: Agent, invocation: SkillInvocation) -> int:
    try:
        answer = agent.chat_with_skill(invocation)
        print(answer)
        _print_turn_status(agent)
        return 0
    except (ProviderError, AgentLoopError, ValueError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


def _print_tool_event(call: ToolCall, result: ToolExecutionResult) -> None:
    status = "ok" if result.ok else "error"
    display_name = call.name
    if result.name_repaired and result.executed_name:
        display_name = f"{call.name} -> {result.executed_name}"
    print(
        f"tool> {display_name} [{status}, {result.effect_disposition}]",
        file=sys.stderr,
    )


def _approve_tool(
    definition: ToolDefinition,
    arguments: Mapping[str, object],
) -> bool:
    if definition.name == "write_file":
        path = arguments.get("path")
        content = arguments.get("content")
        character_count = len(content) if isinstance(content, str) else "unknown"
        prompt = (
            f"approval> write_file path={path!r}, "
            f"characters={character_count}. Approve? [y/N] "
        )
    elif definition.name == "memory_update":
        operation = arguments.get("operation")
        document = arguments.get("document")
        content = arguments.get("content")
        character_count = len(content) if isinstance(content, str) else 0
        prompt = (
            f"approval> memory_update operation={operation!r}, "
            f"document={document!r}, characters={character_count}. "
            "This changes future Sessions. Approve? [y/N] "
        )
    else:
        return False
    answer = input(prompt)
    return answer.strip().lower() in {"y", "yes"}


def _print_turn_status(agent: Agent) -> None:
    if not agent.last_turn_completed:
        print(
            f"status> partial turn ({agent.last_stop_reason}); tools were stopped "
            "and the model produced a no-tools summary",
            file=sys.stderr,
        )


def _print_recovery_report(report: RecoveryReport | None) -> None:
    if report is None or not report.changed:
        return
    print(
        "recovery> "
        f"unknown_results={report.inserted_unknown_results}, "
        f"not_executed_results={report.inserted_not_executed_results}, "
        f"orphan_results_removed={report.deactivated_orphan_results}, "
        f"ids_repaired={report.repaired_tool_call_ids}, "
        f"same_role_merged={report.merged_same_role_messages}",
        file=sys.stderr,
    )


def _print_sessions(
    session_db: SessionDB | None,
    *,
    profile_id: str,
) -> None:
    if session_db is None:
        return
    sessions = session_db.list_sessions(profile_id=profile_id)
    if not sessions:
        print("No sessions.")
        return
    for session in sessions:
        print(
            f"{session['id']}  profile={session['profile_id']}  "
            f"messages={session['message_count']}  "
            f"updated={session['updated_at']}"
        )


def _print_skills(catalog: SkillCatalog | None) -> None:
    if catalog is None:
        print("Skills are disabled.")
        return
    if not catalog.entries:
        print("No active Skills.")
        return
    for entry in catalog.entries:
        print(f"{entry.name}  [{entry.source}]  {entry.description}")
    if catalog.conflicts:
        print(
            f"conflicts> {len(catalog.conflicts)} shadowed Skill(s)",
            file=sys.stderr,
        )
    if catalog.diagnostics:
        print(
            f"diagnostics> {len(catalog.diagnostics)} invalid Skill(s) skipped",
            file=sys.stderr,
        )


def _run_interactive(
    agent: Agent,
    skill_catalog: SkillCatalog | None = None,
) -> int:
    print(
        "Mini Harness (guarded tools + profile Memory + Skills). "
        "Type /exit to quit."
    )
    while True:
        try:
            message = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if message.strip().lower() in {"/exit", "/quit"}:
            return 0
        if not message.strip():
            continue
        if message.strip().lower() == "/skills":
            _print_skills(skill_catalog)
            continue
        if message.strip().lower() == "/context":
            _print_context_status(agent)
            continue
        if message.strip().lower() == "/compress" or message.strip().lower().startswith(
            "/compress "
        ):
            focus = message.strip()[len("/compress") :].strip()
            try:
                result = agent.compress_context(focus, force=True)
            except (ValueError, SessionError) as exc:
                print(f"compression error> {exc}", file=sys.stderr)
                continue
            _print_compression_result(result)
            continue
        try:
            invocation = _resolve_skill_command(message, skill_catalog)
        except SkillError as exc:
            print(f"skill error> {exc}", file=sys.stderr)
            continue
        try:
            answer = (
                agent.chat_with_skill(invocation)
                if invocation is not None
                else agent.chat(message)
            )
        except (ProviderError, AgentLoopError) as exc:
            print(f"error> {exc}", file=sys.stderr)
            continue
        print(f"assistant> {answer}")
        _print_turn_status(agent)


def _resolve_skill_command(
    message: str,
    catalog: SkillCatalog | None,
) -> SkillInvocation | None:
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    command, _, remainder = stripped.partition(" ")
    command_name = command[1:].strip().lower()
    if command_name == "skill":
        skill_name, separator, instruction = remainder.strip().partition(" ")
        if not skill_name:
            raise SkillError("Usage: /skill NAME [instruction]")
        if catalog is None:
            raise SkillError("Skills are disabled")
        return catalog.build_invocation(
            skill_name,
            instruction if separator else "",
        )
    if catalog is None:
        return None
    try:
        catalog.get(command_name)
    except SkillError:
        return None
    return catalog.build_invocation(command_name, remainder)


def _print_compression_result(result) -> None:
    saved = (
        result.trigger_tokens - result.estimated_after_tokens
        if result.estimated_after_tokens is not None
        else None
    )
    details = [
        f"status={result.status}",
        f"before≈{result.trigger_tokens}",
    ]
    if result.estimated_after_tokens is not None:
        details.append(f"after≈{result.estimated_after_tokens}")
    if saved is not None:
        details.append(f"saved≈{saved}")
    details.append(f"archived={result.compacted_message_count}")
    if result.used_fallback:
        details.append("fallback=yes")
    if result.error_type:
        details.append(f"error={result.error_type}")
    print("compression> " + "  ".join(details))


def _print_context_status(agent: Agent) -> None:
    status = agent.context_status()
    if not status.get("available"):
        print("context> persistent compression unavailable")
        return
    lock = status.get("lock_owner") or "none"
    print(
        "context> "
        f"runs={status['run_count']}  "
        f"auto_paused={bool(status['auto_paused'])}  "
        f"cooldown_until={status['cooldown_until']}  "
        f"lock={lock}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
