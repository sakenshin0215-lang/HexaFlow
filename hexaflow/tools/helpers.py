import ast
import base64
import json
import mimetypes
import re
from urllib.parse import urlparse


def is_domain_allowed(target_url: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    host = (urlparse(target_url).hostname or "").lower()
    if not host:
        return False
    for domain in allowed_domains:
        d = domain.lower().strip()
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def contains_blocked_keyword(raw_text: str, blocked_keywords: list[str]) -> bool:
    if not blocked_keywords:
        return False
    lower_text = (raw_text or "").lower()
    return any(k.lower().strip() and k.lower().strip() in lower_text for k in blocked_keywords)


def build_goal_with_notes(spec) -> str:
    notes = getattr(spec, "notes", None) or []
    goal = getattr(spec, "goal", "") or ""
    if not notes:
        return goal
    notes_text = "\n".join([f"- {item}" for item in notes])
    return f"{goal}\n\n执行注意事项:\n{notes_text}"


def build_recent_steps_text(blueprint, current_index: int, window: int = 5) -> str:
    start = max(0, current_index - window)
    lines = []
    for idx in range(start, current_index):
        st = blueprint.steps[idx]
        lines.append(
            f"{st.step_id}: {st.action.action_type} -> {st.action.target} | {st.description}"
        )
    return "\n".join(lines) if lines else "No previous steps."


def image_to_data_url(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            binary = f.read()
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "image/png"
        b64 = base64.b64encode(binary).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return ""


def extract_json_object(text: str) -> dict:
    if not text:
        return {}
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        obj = ast.literal_eval(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}
