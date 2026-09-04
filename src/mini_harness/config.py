from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Mapping, MutableMapping

import yaml

from .profiles import normalize_profile_id
from .skills import normalize_skill_name


class ConfigError(ValueError):
    """Raised when project configuration is missing or invalid."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 120.0
    max_attempts: int = 3
    retry_backoff_seconds: float = 0.5

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than zero")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("max_attempts must be between 1 and 10")
        if (
            isinstance(self.retry_backoff_seconds, bool)
            or not isinstance(self.retry_backoff_seconds, (int, float))
            or not isfinite(self.retry_backoff_seconds)
            or self.retry_backoff_seconds < 0
        ):
            raise ValueError("retry_backoff_seconds must not be negative")


@dataclass(frozen=True)
class AgentConfig:
    system_prompt: str
    max_iterations: int
    workspace_root: Path


@dataclass(frozen=True)
class ProfileConfig:
    id: str


@dataclass(frozen=True)
class ContextConfig:
    enabled: bool
    max_input_tokens: int
    reserved_output_tokens: int
    approximate_chars_per_token: float
    max_tool_result_tokens: int
    recent_tool_tail_tokens: int
    min_recent_turns: int
    compaction_target_ratio: float


@dataclass(frozen=True)
class CompressionConfig:
    enabled: bool
    threshold_ratio: float
    target_ratio: float
    protect_first_turns: int
    tail_tokens: int
    max_output_tokens: int
    summary_model: str
    abort_on_summary_failure: bool
    cooldown_seconds: float
    lock_ttl_seconds: float
    min_savings_ratio: float
    anti_thrashing_limit: int
    in_place: bool


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool
    root_path: Path
    max_user_chars: int
    max_memory_chars: int
    lock_timeout_seconds: float


@dataclass(frozen=True)
class SkillsConfig:
    enabled: bool
    profile_root_path: Path
    external_dirs: tuple[Path, ...]
    bundled_dir: Path
    optional_dir: Path
    enabled_optional: tuple[str, ...]
    max_skill_chars: int
    max_index_description_chars: int


@dataclass(frozen=True)
class ToolsConfig:
    auto_repair_names: bool
    enable_write_file: bool
    max_result_chars: int
    name_repair_threshold: float
    same_call_limit: int
    max_calls_per_batch: int
    max_calls_per_turn: int
    timeout_seconds: float


@dataclass(frozen=True)
class StorageConfig:
    enabled: bool
    database_path: Path


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool
    event_log_path: Path


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderConfig
    agent: AgentConfig
    profile: ProfileConfig
    context: ContextConfig
    compression: CompressionConfig
    memory: MemoryConfig
    skills: SkillsConfig
    tools: ToolsConfig
    storage: StorageConfig
    observability: ObservabilityConfig


def load_dotenv(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load a deliberately small KEY=VALUE .env subset.

    Existing environment variables win. Shell expansion and command
    substitution are intentionally unsupported.
    """

    target = environ if environ is not None else os.environ
    if not path.exists():
        return

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {line_number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        target.setdefault(key, value)


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a YAML mapping")
    return value


def _require_text(mapping: Mapping[str, object], key: str, path: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def load_config(
    config_path: str | Path,
    *,
    dotenv_path: str | Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    env = environ if environ is not None else os.environ
    env_file = Path(dotenv_path).expanduser().resolve() if dotenv_path else path.parent / ".env"
    load_dotenv(env_file, environ=env)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    root = _require_mapping(raw, "config")
    provider = _require_mapping(root.get("provider"), "provider")
    agent = _require_mapping(root.get("agent"), "agent")
    profile = _require_mapping(root.get("profile", {}), "profile")
    context = _require_mapping(root.get("context", {}), "context")
    compression = _require_mapping(
        root.get("compression", {}),
        "compression",
    )
    memory = _require_mapping(root.get("memory", {}), "memory")
    skills = _require_mapping(root.get("skills", {}), "skills")
    tools = _require_mapping(root.get("tools", {}), "tools")
    storage = _require_mapping(root.get("storage", {}), "storage")
    observability = _require_mapping(
        root.get("observability", {}),
        "observability",
    )

    base_url = _require_text(provider, "base_url", "provider").rstrip("/")
    model = _require_text(provider, "model", "provider")
    if model == "replace-with-your-model":
        raise ConfigError("provider.model still contains the placeholder value")

    api_key_env = _require_text(provider, "api_key_env", "provider")
    api_key = env.get(api_key_env, "").strip()
    if not api_key:
        raise ConfigError(
            f"Missing API key: set {api_key_env} in the environment or {env_file}"
        )

    timeout_raw = provider.get("timeout_seconds", 120)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("provider.timeout_seconds must be a number") from exc
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ConfigError("provider.timeout_seconds must be greater than zero")

    max_attempts_raw = provider.get("max_attempts", 3)
    if isinstance(max_attempts_raw, bool):
        raise ConfigError("provider.max_attempts must be an integer")
    try:
        max_attempts = int(max_attempts_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("provider.max_attempts must be an integer") from exc
    if not 1 <= max_attempts <= 10:
        raise ConfigError("provider.max_attempts must be between 1 and 10")

    retry_backoff_raw = provider.get("retry_backoff_seconds", 0.5)
    if isinstance(retry_backoff_raw, bool):
        raise ConfigError("provider.retry_backoff_seconds must be a number")
    try:
        retry_backoff_seconds = float(retry_backoff_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "provider.retry_backoff_seconds must be a number"
        ) from exc
    if not isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
        raise ConfigError(
            "provider.retry_backoff_seconds must not be negative"
        )

    system_prompt = _require_text(agent, "system_prompt", "agent")

    max_iterations_raw = agent.get("max_iterations", 8)
    if isinstance(max_iterations_raw, bool):
        raise ConfigError("agent.max_iterations must be an integer")
    try:
        max_iterations = int(max_iterations_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("agent.max_iterations must be an integer") from exc
    if max_iterations <= 0:
        raise ConfigError("agent.max_iterations must be greater than zero")

    workspace_raw = agent.get("workspace_root", ".")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise ConfigError("agent.workspace_root must be a non-empty path")
    workspace_root = Path(workspace_raw).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = path.parent / workspace_root
    workspace_root = workspace_root.resolve()
    if not workspace_root.is_dir():
        raise ConfigError(f"agent.workspace_root is not a directory: {workspace_root}")

    try:
        profile_id = normalize_profile_id(profile.get("id", "default"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    context_enabled = context.get("enabled", True)
    if not isinstance(context_enabled, bool):
        raise ConfigError("context.enabled must be true or false")

    max_input_tokens_raw = context.get("max_input_tokens", 64_000)
    if isinstance(max_input_tokens_raw, bool):
        raise ConfigError("context.max_input_tokens must be an integer")
    try:
        max_input_tokens = int(max_input_tokens_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("context.max_input_tokens must be an integer") from exc
    if max_input_tokens < 256:
        raise ConfigError("context.max_input_tokens must be at least 256")

    reserved_output_tokens_raw = context.get("reserved_output_tokens", 4_000)
    if isinstance(reserved_output_tokens_raw, bool):
        raise ConfigError("context.reserved_output_tokens must be an integer")
    try:
        reserved_output_tokens = int(reserved_output_tokens_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.reserved_output_tokens must be an integer"
        ) from exc
    if reserved_output_tokens < 0:
        raise ConfigError(
            "context.reserved_output_tokens must not be negative"
        )
    if reserved_output_tokens >= max_input_tokens:
        raise ConfigError(
            "context.reserved_output_tokens must be less than "
            "context.max_input_tokens"
        )

    chars_per_token_raw = context.get("approximate_chars_per_token", 4.0)
    if isinstance(chars_per_token_raw, bool):
        raise ConfigError(
            "context.approximate_chars_per_token must be a number"
        )
    try:
        approximate_chars_per_token = float(chars_per_token_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.approximate_chars_per_token must be a number"
        ) from exc
    if approximate_chars_per_token <= 0:
        raise ConfigError(
            "context.approximate_chars_per_token must be greater than zero"
        )

    max_tool_result_tokens_raw = context.get(
        "max_tool_result_tokens",
        2_000,
    )
    if isinstance(max_tool_result_tokens_raw, bool):
        raise ConfigError(
            "context.max_tool_result_tokens must be an integer"
        )
    try:
        max_tool_result_tokens = int(max_tool_result_tokens_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.max_tool_result_tokens must be an integer"
        ) from exc
    if max_tool_result_tokens < 32:
        raise ConfigError(
            "context.max_tool_result_tokens must be at least 32"
        )

    recent_tool_tail_tokens_raw = context.get(
        "recent_tool_tail_tokens",
        12_000,
    )
    if isinstance(recent_tool_tail_tokens_raw, bool):
        raise ConfigError(
            "context.recent_tool_tail_tokens must be an integer"
        )
    try:
        recent_tool_tail_tokens = int(recent_tool_tail_tokens_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.recent_tool_tail_tokens must be an integer"
        ) from exc
    if recent_tool_tail_tokens < 32:
        raise ConfigError(
            "context.recent_tool_tail_tokens must be at least 32"
        )

    min_recent_turns_raw = context.get("min_recent_turns", 1)
    if isinstance(min_recent_turns_raw, bool):
        raise ConfigError("context.min_recent_turns must be an integer")
    try:
        min_recent_turns = int(min_recent_turns_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.min_recent_turns must be an integer"
        ) from exc
    if min_recent_turns < 1:
        raise ConfigError("context.min_recent_turns must be at least 1")

    target_ratio_raw = context.get("compaction_target_ratio", 0.8)
    if isinstance(target_ratio_raw, bool):
        raise ConfigError(
            "context.compaction_target_ratio must be a number"
        )
    try:
        compaction_target_ratio = float(target_ratio_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "context.compaction_target_ratio must be a number"
        ) from exc
    if not 0.5 <= compaction_target_ratio <= 1:
        raise ConfigError(
            "context.compaction_target_ratio must be between 0.5 and 1"
        )

    compression_enabled = _config_bool(
        compression,
        "enabled",
        True,
        "compression",
    )
    compression_threshold_ratio = _config_float(
        compression,
        "threshold_ratio",
        0.75,
        "compression",
        minimum=0,
        maximum=1,
        minimum_inclusive=False,
    )
    compression_target_ratio = _config_float(
        compression,
        "target_ratio",
        0.20,
        "compression",
        minimum=0,
        maximum=compression_threshold_ratio,
        minimum_inclusive=False,
        maximum_inclusive=False,
    )
    compression_protect_first_turns = _config_int(
        compression,
        "protect_first_turns",
        1,
        "compression",
        minimum=0,
    )
    compression_tail_tokens = _config_int(
        compression,
        "tail_tokens",
        12_000,
        "compression",
        minimum=1,
    )
    compression_max_output_tokens = _config_int(
        compression,
        "max_output_tokens",
        2_000,
        "compression",
        minimum=1,
    )
    summary_model_raw = compression.get("summary_model", "")
    if not isinstance(summary_model_raw, str):
        raise ConfigError("compression.summary_model must be a string")
    compression_summary_model = summary_model_raw.strip()
    compression_abort_on_failure = _config_bool(
        compression,
        "abort_on_summary_failure",
        True,
        "compression",
    )
    compression_cooldown_seconds = _config_float(
        compression,
        "cooldown_seconds",
        600,
        "compression",
        minimum=0,
    )
    compression_lock_ttl_seconds = _config_float(
        compression,
        "lock_ttl_seconds",
        120,
        "compression",
        minimum=0,
        minimum_inclusive=False,
    )
    compression_min_savings_ratio = _config_float(
        compression,
        "min_savings_ratio",
        0.10,
        "compression",
        minimum=0,
        maximum=1,
    )
    compression_anti_thrashing_limit = _config_int(
        compression,
        "anti_thrashing_limit",
        2,
        "compression",
        minimum=1,
    )
    compression_in_place = _config_bool(
        compression,
        "in_place",
        True,
        "compression",
    )
    if not compression_in_place:
        raise ConfigError(
            "compression.in_place=false is not supported by Mini Harness"
        )

    memory_enabled = memory.get("enabled", True)
    if not isinstance(memory_enabled, bool):
        raise ConfigError("memory.enabled must be true or false")
    memory_root_raw = memory.get(
        "root_path",
        ".mini-harness/profiles",
    )
    if not isinstance(memory_root_raw, str) or not memory_root_raw.strip():
        raise ConfigError("memory.root_path must be a non-empty path")
    memory_root_path = Path(memory_root_raw).expanduser()
    if not memory_root_path.is_absolute():
        memory_root_path = path.parent / memory_root_path
    memory_root_path = memory_root_path.resolve()

    max_user_chars_raw = memory.get("max_user_chars", 4_000)
    if isinstance(max_user_chars_raw, bool):
        raise ConfigError("memory.max_user_chars must be an integer")
    try:
        max_user_chars = int(max_user_chars_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("memory.max_user_chars must be an integer") from exc
    if max_user_chars < 256:
        raise ConfigError("memory.max_user_chars must be at least 256")

    max_memory_chars_raw = memory.get("max_memory_chars", 8_000)
    if isinstance(max_memory_chars_raw, bool):
        raise ConfigError("memory.max_memory_chars must be an integer")
    try:
        max_memory_chars = int(max_memory_chars_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("memory.max_memory_chars must be an integer") from exc
    if max_memory_chars < 256:
        raise ConfigError("memory.max_memory_chars must be at least 256")

    memory_lock_timeout_raw = memory.get("lock_timeout_seconds", 5)
    if isinstance(memory_lock_timeout_raw, bool):
        raise ConfigError("memory.lock_timeout_seconds must be a number")
    try:
        memory_lock_timeout_seconds = float(memory_lock_timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "memory.lock_timeout_seconds must be a number"
        ) from exc
    if memory_lock_timeout_seconds <= 0:
        raise ConfigError(
            "memory.lock_timeout_seconds must be greater than zero"
        )

    skills_enabled = skills.get("enabled", True)
    if not isinstance(skills_enabled, bool):
        raise ConfigError("skills.enabled must be true or false")

    skill_profile_root_raw = skills.get("profile_root_path")
    if skill_profile_root_raw is None:
        skill_profile_root_path = memory_root_path
    else:
        skill_profile_root_path = _resolve_config_path(
            skill_profile_root_raw,
            path.parent,
            "skills.profile_root_path",
        )

    external_dirs_raw = skills.get("external_dirs", [])
    if isinstance(external_dirs_raw, str):
        external_dirs_raw = [external_dirs_raw]
    if not isinstance(external_dirs_raw, list):
        raise ConfigError("skills.external_dirs must be a path list")
    external_dirs_list: list[Path] = []
    seen_external_dirs: set[Path] = set()
    for index, external_dir_raw in enumerate(external_dirs_raw):
        resolved_external = _resolve_config_path(
            external_dir_raw,
            path.parent,
            f"skills.external_dirs[{index}]",
        )
        if resolved_external in seen_external_dirs:
            continue
        seen_external_dirs.add(resolved_external)
        external_dirs_list.append(resolved_external)

    bundled_dir = _resolve_config_path(
        skills.get("bundled_dir", "skills"),
        path.parent,
        "skills.bundled_dir",
    )
    optional_dir = _resolve_config_path(
        skills.get("optional_dir", "optional-skills"),
        path.parent,
        "skills.optional_dir",
    )

    enabled_optional_raw = skills.get("enabled_optional", [])
    if not isinstance(enabled_optional_raw, list):
        raise ConfigError("skills.enabled_optional must be a skill-name list")
    enabled_optional_list: list[str] = []
    seen_optional: set[str] = set()
    for index, optional_name_raw in enumerate(enabled_optional_raw):
        try:
            optional_name = normalize_skill_name(optional_name_raw)
        except ValueError as exc:
            raise ConfigError(
                f"skills.enabled_optional[{index}]: {exc}"
            ) from exc
        if optional_name in seen_optional:
            continue
        seen_optional.add(optional_name)
        enabled_optional_list.append(optional_name)

    max_skill_chars_raw = skills.get("max_skill_chars", 40_000)
    if isinstance(max_skill_chars_raw, bool):
        raise ConfigError("skills.max_skill_chars must be an integer")
    try:
        max_skill_chars = int(max_skill_chars_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "skills.max_skill_chars must be an integer"
        ) from exc
    if max_skill_chars < 256:
        raise ConfigError("skills.max_skill_chars must be at least 256")

    max_index_description_chars_raw = skills.get(
        "max_index_description_chars",
        240,
    )
    if isinstance(max_index_description_chars_raw, bool):
        raise ConfigError(
            "skills.max_index_description_chars must be an integer"
        )
    try:
        max_index_description_chars = int(
            max_index_description_chars_raw
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "skills.max_index_description_chars must be an integer"
        ) from exc
    if max_index_description_chars < 32:
        raise ConfigError(
            "skills.max_index_description_chars must be at least 32"
        )

    auto_repair_names = tools.get("auto_repair_names", True)
    if not isinstance(auto_repair_names, bool):
        raise ConfigError("tools.auto_repair_names must be true or false")
    threshold_raw = tools.get("name_repair_threshold", 0.84)
    if isinstance(threshold_raw, bool):
        raise ConfigError("tools.name_repair_threshold must be a number")
    try:
        name_repair_threshold = float(threshold_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("tools.name_repair_threshold must be a number") from exc
    if not 0 < name_repair_threshold <= 1:
        raise ConfigError("tools.name_repair_threshold must be greater than 0 and at most 1")

    enable_write_file = tools.get("enable_write_file", False)
    if not isinstance(enable_write_file, bool):
        raise ConfigError("tools.enable_write_file must be true or false")

    max_result_chars_raw = tools.get("max_result_chars", 12_000)
    if isinstance(max_result_chars_raw, bool):
        raise ConfigError("tools.max_result_chars must be an integer")
    try:
        max_result_chars = int(max_result_chars_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("tools.max_result_chars must be an integer") from exc
    if max_result_chars < 256:
        raise ConfigError("tools.max_result_chars must be at least 256")

    same_call_limit_raw = tools.get("same_call_limit", 2)
    if isinstance(same_call_limit_raw, bool):
        raise ConfigError("tools.same_call_limit must be an integer")
    try:
        same_call_limit = int(same_call_limit_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("tools.same_call_limit must be an integer") from exc
    if same_call_limit < 1:
        raise ConfigError("tools.same_call_limit must be at least 1")

    max_calls_per_batch_raw = tools.get("max_calls_per_batch", 6)
    if isinstance(max_calls_per_batch_raw, bool):
        raise ConfigError("tools.max_calls_per_batch must be an integer")
    try:
        max_calls_per_batch = int(max_calls_per_batch_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "tools.max_calls_per_batch must be an integer"
        ) from exc
    if max_calls_per_batch < 1:
        raise ConfigError("tools.max_calls_per_batch must be at least 1")

    max_calls_per_turn_raw = tools.get("max_calls_per_turn", 16)
    if isinstance(max_calls_per_turn_raw, bool):
        raise ConfigError("tools.max_calls_per_turn must be an integer")
    try:
        max_calls_per_turn = int(max_calls_per_turn_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "tools.max_calls_per_turn must be an integer"
        ) from exc
    if max_calls_per_turn < 1:
        raise ConfigError("tools.max_calls_per_turn must be at least 1")

    tool_timeout_raw = tools.get("timeout_seconds", 30)
    if isinstance(tool_timeout_raw, bool):
        raise ConfigError("tools.timeout_seconds must be a number")
    try:
        tool_timeout_seconds = float(tool_timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("tools.timeout_seconds must be a number") from exc
    if tool_timeout_seconds <= 0:
        raise ConfigError("tools.timeout_seconds must be greater than zero")

    storage_enabled = storage.get("enabled", True)
    if not isinstance(storage_enabled, bool):
        raise ConfigError("storage.enabled must be true or false")
    database_path_raw = storage.get(
        "database_path",
        ".mini-harness/sessions.db",
    )
    if not isinstance(database_path_raw, str) or not database_path_raw.strip():
        raise ConfigError("storage.database_path must be a non-empty path")
    database_path = Path(database_path_raw).expanduser()
    if not database_path.is_absolute():
        database_path = path.parent / database_path
    database_path = database_path.resolve()

    observability_enabled = observability.get("enabled", True)
    if not isinstance(observability_enabled, bool):
        raise ConfigError("observability.enabled must be true or false")
    event_log_path_raw = observability.get(
        "event_log_path",
        ".mini-harness/events.jsonl",
    )
    if not isinstance(event_log_path_raw, str) or not event_log_path_raw.strip():
        raise ConfigError("observability.event_log_path must be a non-empty path")
    event_log_path = Path(event_log_path_raw).expanduser()
    if not event_log_path.is_absolute():
        event_log_path = path.parent / event_log_path
    event_log_path = event_log_path.resolve()

    return AppConfig(
        provider=ProviderConfig(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        ),
        agent=AgentConfig(
            system_prompt=system_prompt,
            max_iterations=max_iterations,
            workspace_root=workspace_root,
        ),
        profile=ProfileConfig(id=profile_id),
        context=ContextConfig(
            enabled=context_enabled,
            max_input_tokens=max_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            approximate_chars_per_token=approximate_chars_per_token,
            max_tool_result_tokens=max_tool_result_tokens,
            recent_tool_tail_tokens=recent_tool_tail_tokens,
            min_recent_turns=min_recent_turns,
            compaction_target_ratio=compaction_target_ratio,
        ),
        compression=CompressionConfig(
            enabled=compression_enabled,
            threshold_ratio=compression_threshold_ratio,
            target_ratio=compression_target_ratio,
            protect_first_turns=compression_protect_first_turns,
            tail_tokens=compression_tail_tokens,
            max_output_tokens=compression_max_output_tokens,
            summary_model=compression_summary_model,
            abort_on_summary_failure=compression_abort_on_failure,
            cooldown_seconds=compression_cooldown_seconds,
            lock_ttl_seconds=compression_lock_ttl_seconds,
            min_savings_ratio=compression_min_savings_ratio,
            anti_thrashing_limit=compression_anti_thrashing_limit,
            in_place=compression_in_place,
        ),
        memory=MemoryConfig(
            enabled=memory_enabled,
            root_path=memory_root_path,
            max_user_chars=max_user_chars,
            max_memory_chars=max_memory_chars,
            lock_timeout_seconds=memory_lock_timeout_seconds,
        ),
        skills=SkillsConfig(
            enabled=skills_enabled,
            profile_root_path=skill_profile_root_path,
            external_dirs=tuple(external_dirs_list),
            bundled_dir=bundled_dir,
            optional_dir=optional_dir,
            enabled_optional=tuple(enabled_optional_list),
            max_skill_chars=max_skill_chars,
            max_index_description_chars=max_index_description_chars,
        ),
        tools=ToolsConfig(
            auto_repair_names=auto_repair_names,
            enable_write_file=enable_write_file,
            max_result_chars=max_result_chars,
            name_repair_threshold=name_repair_threshold,
            same_call_limit=same_call_limit,
            max_calls_per_batch=max_calls_per_batch,
            max_calls_per_turn=max_calls_per_turn,
            timeout_seconds=tool_timeout_seconds,
        ),
        storage=StorageConfig(
            enabled=storage_enabled,
            database_path=database_path,
        ),
        observability=ObservabilityConfig(
            enabled=observability_enabled,
            event_log_path=event_log_path,
        ),
    )


def _config_bool(
    mapping: Mapping[str, object],
    key: str,
    default: bool,
    section: str,
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be true or false")
    return value


def _config_int(
    mapping: Mapping[str, object],
    key: str,
    default: int,
    section: str,
    *,
    minimum: int,
) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{section}.{key} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{section}.{key} must be at least {minimum}")
    return parsed


def _config_float(
    mapping: Mapping[str, object],
    key: str,
    default: float,
    section: str,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
    maximum_inclusive: bool = True,
) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{section}.{key} must be a number") from exc
    minimum_ok = parsed >= minimum if minimum_inclusive else parsed > minimum
    maximum_ok = (
        True
        if maximum is None
        else parsed <= maximum
        if maximum_inclusive
        else parsed < maximum
    )
    if not minimum_ok or not maximum_ok:
        bounds = f"{minimum} and {maximum}" if maximum is not None else f"{minimum}"
        raise ConfigError(
            f"{section}.{key} must be within the configured bounds ({bounds})"
        )
    return parsed


def _resolve_config_path(
    raw_value: object,
    config_directory: Path,
    field: str,
) -> Path:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigError(f"{field} must be a non-empty path")
    resolved = Path(raw_value).expanduser()
    if not resolved.is_absolute():
        resolved = config_directory / resolved
    return resolved.resolve()
