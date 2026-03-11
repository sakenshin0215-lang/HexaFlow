import json
import os
from copy import deepcopy


DEFAULT_RUNTIME_CONFIG = {
    "browser": {
        "headless": False,
        "use_cdp": True,
        "cdp_start_url": "https://www.okx.com/web3",
        "user_data_dir": "/Users/kenshinnb/pw-profiles/okx-chrome",
        "profile_directory": "Default",
    },
    "agent_profile": "ollama",
    "agent_ollama": {
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    },
    "agent_openai": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-4.1-mini",
    },
    # Backward-compatible default agent (will be overwritten by selected profile)
    "agent": {
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    },
    "ai": {
        "task_spec_path": "workspace/task_specs/okx_web3_ai_token.json",
        "ai_repair_only": True,
        "decision_use_vision": True,
    },
    "replay": {
        "trace_path": "workspace/traces/Task_20260311_195022_20260311_195246.json",
        "enable_ai_repair": True,
        "ai_repair_only": False,
        "repair_context_window": 5,
    },
    "loop": {
        "trace_path": "workspace/traces/ManualTask_20260311_150422_20260311_150615.json",
        "task_spec_path": "workspace/task_specs/okx_web3_demo.json",
        "enable_ai_repair": True,
        "ai_repair_only": True,
    },
    "record": {
        "goal": "在加密货币交易所查看实时行情数据",
        "start_url": "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    },
}


def _deep_merge(dst: dict, src: dict):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip()
    if value == "":
        return default
    return value


def load_runtime_config() -> tuple[dict, str]:
    cfg = deepcopy(DEFAULT_RUNTIME_CONFIG)
    cfg_path = os.getenv("RUNTIME_CONFIG_PATH", "workspace/config/runtime.json")

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            if isinstance(file_cfg, dict):
                _deep_merge(cfg, file_cfg)
        except Exception:
            pass

    # Optional env overrides (keep minimal)
    cfg["browser"]["use_cdp"] = _env_bool("USE_CDP", cfg["browser"]["use_cdp"])
    cfg["browser"]["cdp_start_url"] = _env_str("CDP_START_URL", cfg["browser"]["cdp_start_url"])
    cfg["browser"]["user_data_dir"] = _env_str("CHROME_USER_DATA_DIR", cfg["browser"]["user_data_dir"])
    cfg["browser"]["profile_directory"] = _env_str(
        "CHROME_PROFILE_DIRECTORY", cfg["browser"]["profile_directory"]
    )

    # Agent profile selection and per-profile overrides
    raw_profile = os.getenv("AGENT_PROFILE", str(cfg.get("agent_profile", "ollama"))).strip().lower()
    if raw_profile in {"agent_ollama", "ollama"}:
        selected_profile_key = "agent_ollama"
        cfg["agent_profile"] = "ollama"
    elif raw_profile in {"agent_openai", "openai"}:
        selected_profile_key = "agent_openai"
        cfg["agent_profile"] = "openai"
    else:
        selected_profile_key = "agent_ollama"
        cfg["agent_profile"] = "ollama"

    cfg["agent_ollama"]["api_key"] = _env_str(
        "AGENT_OLLAMA_API_KEY", cfg["agent_ollama"].get("api_key", "ollama")
    )
    cfg["agent_ollama"]["base_url"] = _env_str(
        "AGENT_OLLAMA_BASE_URL", cfg["agent_ollama"].get("base_url", "http://localhost:11434/v1")
    )
    cfg["agent_ollama"]["model_name"] = _env_str(
        "AGENT_OLLAMA_MODEL", cfg["agent_ollama"].get("model_name", "qwen3-vl:8b-instruct")
    )

    cfg["agent_openai"]["api_key"] = _env_str(
        "AGENT_OPENAI_API_KEY", cfg["agent_openai"].get("api_key", "")
    )
    cfg["agent_openai"]["base_url"] = _env_str(
        "AGENT_OPENAI_BASE_URL", cfg["agent_openai"].get("base_url", "https://api.openai.com/v1")
    )
    cfg["agent_openai"]["model_name"] = _env_str(
        "AGENT_OPENAI_MODEL", cfg["agent_openai"].get("model_name", "gpt-4.1-mini")
    )

    selected_agent = deepcopy(cfg.get(selected_profile_key, {}))
    if not selected_agent and isinstance(cfg.get("agent"), dict):
        selected_agent = deepcopy(cfg["agent"])
    # Keep old generic env vars compatible, applied to selected profile
    selected_agent["api_key"] = _env_str("AI_API_KEY", selected_agent.get("api_key", ""))
    selected_agent["base_url"] = _env_str("AI_BASE_URL", selected_agent.get("base_url", ""))
    selected_agent["model_name"] = _env_str("AI_MODEL", selected_agent.get("model_name", ""))
    cfg["agent"] = selected_agent

    cfg["ai"]["task_spec_path"] = _env_str("AI_TASK_SPEC_PATH", cfg["ai"]["task_spec_path"])
    cfg["ai"]["ai_repair_only"] = _env_bool("AI_REPAIR_ONLY", cfg["ai"]["ai_repair_only"])
    cfg["ai"]["decision_use_vision"] = _env_bool(
        "AI_DECISION_USE_VISION", cfg["ai"]["decision_use_vision"]
    )

    cfg["replay"]["trace_path"] = _env_str("REPLAY_TRACE_PATH", cfg["replay"]["trace_path"])
    cfg["replay"]["enable_ai_repair"] = _env_bool(
        "ENABLE_REPLAY_AI_REPAIR", cfg["replay"]["enable_ai_repair"]
    )
    cfg["replay"]["ai_repair_only"] = _env_bool("AI_REPAIR_ONLY", cfg["replay"]["ai_repair_only"])
    cfg["replay"]["repair_context_window"] = _env_int(
        "REPAIR_CONTEXT_WINDOW", cfg["replay"]["repair_context_window"]
    )

    cfg["loop"]["trace_path"] = _env_str("LOOP_TRACE_PATH", cfg["loop"]["trace_path"])
    cfg["loop"]["task_spec_path"] = _env_str("LOOP_TASK_SPEC_PATH", cfg["loop"]["task_spec_path"])
    cfg["loop"]["enable_ai_repair"] = _env_bool(
        "ENABLE_REPLAY_AI_REPAIR", cfg["loop"]["enable_ai_repair"]
    )
    cfg["loop"]["ai_repair_only"] = _env_bool("AI_REPAIR_ONLY", cfg["loop"]["ai_repair_only"])

    cfg["record"]["goal"] = _env_str("RECORD_GOAL", cfg["record"]["goal"])
    cfg["record"]["start_url"] = _env_str("RECORD_START_URL", cfg["record"]["start_url"])

    return cfg, cfg_path
