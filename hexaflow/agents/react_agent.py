import logging
import json
import re
from typing import Optional
from dotenv import load_dotenv
from hexaflow.agents.llm_gateway import LLMConfig, LLMGateway
from hexaflow.agents.special_agents import SpecialAgentRouter
from hexaflow.agents.schemas import (
    AnalyzedAction,
    ClickCoordinateDecision,
    NextAction,
    ReplayRepairDecision,
)
from hexaflow.tools.helpers import extract_json_object

load_dotenv()
logger = logging.getLogger("ReActAgent")


class ReActAgent:
    """通用网页自动化 Agent：负责动作决策、回放修复决策与辅助总结。"""
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        provider: str = "openai_compatible",
        enabled_skill_ids: Optional[list[str]] = None,
    ):
        self.llm = LLMGateway(
            LLMConfig(
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                provider=provider,
            )
        )
        self.model_name = model_name
        skill_ids = tuple(enabled_skill_ids) if enabled_skill_ids is not None else ("popup",)
        self.special_router = SpecialAgentRouter.default(enabled_skill_ids=skill_ids)
        self.system_prompt = """
        You are an autonomous Web RPA Agent. 
        You will be given a User Goal, action history, current URL, and interactive elements.
        
        CRITICAL RULES:
        1. If the current page matches the goal, output action_type='done'.
        2. Prefer semantic selectors by visible text/role first, e.g.:
           - button:has-text("Submit")
           - a:has-text("Details")
           - [role="button"]:has-text("Confirm")
        3. Avoid fragile hashed class selectors and avoid nth-child when possible.
        4. If DOM snapshot provides [ID: hexa-*] or data-testid, prefer those stable anchors.
        5. NEVER build selector text from dynamic metrics/values that can change every refresh
           (percentages, prices, counters, timestamps, balances, etc.).
           Prefer stable anchors: [hexa-id], [data-testid], role/text labels without dynamic suffix.
        6. Explain your logic in the 'thought' field before acting.
        5. For input boxes that require submit, prefer action_type='click_type_enter'.
        6. Use action_type='refresh' when page is stale or blocked by transient UI state.
        7. If a modal/dialog/popover is already open, DO NOT click the opener again. Act inside the modal.
        8. If the previous 1-2 steps already opened a panel/modal, choose the next control inside it, not the old entry button.
        9. If history contains markers like [CONTEXT_GUARD] / BLOCKED_REPEATED_FAILURE, NEVER choose those selectors/URLs again in this run.
        10. If user goal asks for analysis/report/summary (e.g., summarize trend or page text), use
            action_type='call_tool' with target='summarize_page' instead of clicking.
        11. If history already contains [SUMMARY_DONE] for current page state, DO NOT summarize again.
            Choose the next actionable step, or output done if no further step is needed.
        """

    def register_special_agent(self, agent) -> None:
        """Register a special-purpose agent at runtime (progressive disclosure)."""
        self.special_router.agents.append(agent)

    def set_enabled_skills(self, enabled_skill_ids: list[str]) -> None:
        """Replace current skill set by registry ids, e.g. ['popup'] or [] (disable all)."""
        self.special_router = SpecialAgentRouter.default(enabled_skill_ids=tuple(enabled_skill_ids))

    @staticmethod
    def _extract_action_from_text(text: str) -> dict:
        """从非 JSON 自然语言中尽量抽取 action/target/input 结构。"""
        if not text:
            return {}
        lower = text.lower()
        action = None
        for a in ["call_tool", "click_type_enter", "press_enter", "refresh", "summarize", "navigate", "click", "type", "done"]:
            if re.search(rf"\b{re.escape(a)}\b", lower):
                action = a
                break

        target = None
        # 常见 selector/URL 提取
        m_url = re.search(r"https?://[^\s'\"`]+", text)
        m_sel = re.search(r"(\[[^\]]+\]|[a-zA-Z0-9_\-]+:has-text\([^)]+\)|text=[\"'][^\"']+[\"'])", text)
        if m_url:
            target = m_url.group(0)
            if not action:
                action = "navigate"
        elif m_sel:
            target = m_sel.group(1)

        m_input = re.search(r"(?:input_value|input|text)\s*[:=]\s*[\"']([^\"']+)[\"']", text, re.IGNORECASE)
        input_value = m_input.group(1) if m_input else None
        if action:
            return {
                "thought": "parsed from non-json model output",
                "action_type": action,
                "target": target,
                "input_value": input_value,
            }
        return {}

    @staticmethod
    def _normalize_payload(payload: dict) -> dict:
        """统一字段命名并做轻量 selector 归一化，减少模型输出方言差异。"""
        if not isinstance(payload, dict):
            return {}
        normalized = dict(payload)
        if not normalized.get("action_type"):
            normalized["action_type"] = normalized.get("action")
        if not normalized.get("target"):
            normalized["target"] = normalized.get("selector")
        if not normalized.get("target"):
            normalized["target"] = normalized.get("tool_name") or normalized.get("tool")
        if normalized.get("input_value") is None:
            for key in ("input", "text", "value"):
                if normalized.get(key) is not None:
                    normalized["input_value"] = str(normalized.get(key))
                    break
        target = normalized.get("target")
        if isinstance(target, str):
            normalized["target"] = target.strip().strip("`")
            normalized["target"] = ReActAgent._canonicalize_selector_target(
                normalized.get("target")
            )
        return normalized

    @staticmethod
    def _canonicalize_selector_target(target: Optional[str]) -> Optional[str]:
        """将常见的错误 selector 写法修正为可执行形式。"""
        if target is None:
            return None
        t = str(target).strip()
        if not t:
            return None

        # 兼容模型常见输出: [ID: hexa-19] / ID: hexa-19 / hexa-19
        m = re.search(r"\bhexa-(\d+)\b", t, re.IGNORECASE)
        if m:
            return f'[hexa-id="hexa-{m.group(1)}"]'

        # 常见误写: [id="hexa-19"] -> hexa-id
        m2 = re.search(r"""\[\s*id\s*=\s*["'](hexa-\d+)["']\s*\]""", t, re.IGNORECASE)
        if m2:
            return f'[hexa-id="{m2.group(1)}"]'

        # 常见误写: a:text('xxx')，转为 Playwright 可识别 has-text
        m3 = re.match(r"""^\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*text\((['"])(.+?)\2\)\s*$""", t)
        if m3:
            tag = m3.group(1)
            txt = m3.group(3).replace('"', '\\"')
            return f'{tag}:has-text("{txt}")'
        # 常见误写: [text='xxx'] -> text="xxx"
        m4 = re.match(r"""^\s*\[\s*text\s*=\s*(['"])(.+?)\1\s*\]\s*$""", t, re.IGNORECASE)
        if m4:
            txt = (m4.group(2) or "").replace('"', '\\"').strip()
            if txt:
                return f'text="{txt}"'

        return t

    @staticmethod
    def _is_invalid_target(target: Optional[str]) -> bool:
        """过滤明显不可执行/被 JSON 污染的 target。"""
        if not target:
            return True
        t = str(target).strip()
        if not t:
            return True
        low = t.lower()
        invalid_tokens = [
            "```", "json", "action_type", "input_value", "thought", "target",
            "{", "}", "return only",
        ]
        if any(tok in low for tok in invalid_tokens):
            # allow regular CSS attr selectors containing braces? usually none here.
            if not (low.startswith("[") and low.endswith("]")):
                return True
        if "\n" in t and ("{" in t or "}" in t):
            return True
        m = re.match(r'^text=["\']\s*([^"\']{0,2})\s*["\']$', t)
        if m:
            inner = (m.group(1) or "").strip()
            if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", inner):
                return True
        # [ID: hexa-xx] 不是合法 Playwright selector，应当在归一化阶段被转换
        if re.search(r"\[\s*id\s*:\s*hexa-\d+\s*\]", t, re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _extract_target_text_hint(target: Optional[str]) -> str:
        """从 selector 中提取文本提示，用于冲突检测。"""
        if not target:
            return ""
        t = str(target)
        m = re.search(r"has-text\((['\"])(.*?)\1\)", t, re.IGNORECASE)
        if m:
            return (m.group(2) or "").strip()
        m2 = re.search(r"text\s*=\s*(['\"])(.*?)\1", t, re.IGNORECASE)
        if m2:
            return (m2.group(2) or "").strip()
        m3 = re.search(r"aria-label\s*=\s*(['\"])(.*?)\1", t, re.IGNORECASE)
        if m3:
            return (m3.group(2) or "").strip()
        return ""

    @classmethod
    def _is_thought_action_contradictory(cls, thought: str, action_type: str, target: Optional[str]) -> bool:
        """检测 thought 与 action 是否自相矛盾（例如说跳过却仍点击同对象）。"""
        if (action_type or "").strip().lower() not in ("click", "type", "click_type_enter"):
            return False
        hint = cls._extract_target_text_hint(target)
        if not hint:
            return False
        th = (thought or "").lower()
        hint_l = hint.lower()
        # 常见“否定 + 继续找/下一个”的语义，说明不应点击当前被提及对象
        negative_markers = [
            "不符合", "不满足", "不对", "不是", "非", "排除", "跳过",
            "not match", "not on", "doesn't match", "does not match", "exclude", "skip",
        ]
        continue_markers = [
            "继续", "向下", "下一个", "继续查找", "继续寻找", "继续向下查找",
            "continue", "next", "keep searching", "search down",
        ]
        has_negative = any(m in th for m in negative_markers)
        has_continue = any(m in th for m in continue_markers)
        # thought 提到了该目标且语义是“排除并继续”，则判定冲突
        mentions_target = hint_l and hint_l in th
        return bool(mentions_target and has_negative and has_continue)

    @staticmethod
    def _recover_target_from_context(payload: dict, raw_hint: str) -> dict:
        """当 target 缺失时，从原始模型文本中恢复可执行 target。"""
        if not isinstance(payload, dict):
            return payload
        action = (payload.get("action_type") or "").strip().lower()
        target = payload.get("target")
        if action not in ("click", "type", "click_type_enter") or target:
            return payload

        hint = raw_hint or ""
        m = re.search(r"\bhexa-(\d+)\b", hint, re.IGNORECASE)
        if m:
            hid = f"hexa-{m.group(1)}"
            payload["target"] = f'[hexa-id="{hid}"]'
            return payload

        # 兼容 "ID: hexa-2" / "id hexa-2"
        m2 = re.search(r"\b(id|ID)\s*[:=]?\s*(hexa-\d+)\b", hint)
        if m2:
            hid = m2.group(2)
            payload["target"] = f'[hexa-id="{hid}"]'
            return payload

        # 优先解析结构化文本里的 selector/target 字段
        m_selector = re.search(r'["\'](?:selector|target)["\']\s*[:=]\s*["\']([^"\n]+)["\']', hint, re.IGNORECASE)
        if m_selector:
            candidate = m_selector.group(1).strip()
            if candidate:
                payload["target"] = candidate
                return payload

        # 优先使用 thought 中更具体的引号文本/代币名，而不是固定词表里的入口按钮。
        for mention in ReActAgent._extract_semantic_mentions(hint):
            if len(mention) > 20:
                continue
            if mention.lower() in {"json", "object", "action_type", "target", "input_value", "thought"}:
                continue
            if any(c in mention for c in ["{", "}", "`"]):
                continue
            payload["target"] = f'text="{mention}"'
            return payload
        return payload

    @staticmethod
    def _parse_dom_snapshot_entries(dom_snapshot: str) -> list[dict]:
        """解析 DOM 快照为结构化候选项，供后续打分选择。"""
        entries = []
        for line in (dom_snapshot or "").splitlines():
            id_match = re.search(r"\[ID:\s*(hexa-\d+)\]", line)
            if not id_match:
                continue
            text_match = re.search(r'text="([^"]*)"', line)
            tag_match = re.search(r"<([a-zA-Z0-9]+)\b", line)
            href_match = re.search(r'href="([^"]*)"', line)
            aria_match = re.search(r'aria-label="([^"]*)"', line)
            testid_match = re.search(r'data-testid="([^"]*)"', line)
            text = (text_match.group(1).strip() if text_match else "")
            entries.append(
                {
                    "id": id_match.group(1),
                    "text": text,
                    "tag": (tag_match.group(1).lower() if tag_match else ""),
                    "href": (href_match.group(1) if href_match else ""),
                    "aria_label": (aria_match.group(1) if aria_match else ""),
                    "testid": (testid_match.group(1) if testid_match else ""),
                    "raw": line,
                }
            )
        return entries

    @staticmethod
    def _extract_semantic_mentions(text: str) -> list[str]:
        """抽取 thought 中可用于定位的语义片段（去重+去噪）。"""
        source = text or ""
        mentions = []

        for item in re.findall(r"[\"'`]{1}([^\"'`]{1,60})[\"'`]{1}", source):
            item = item.strip()
            if item:
                mentions.append(item)

        for item in re.findall(r"\b[A-Z][A-Z0-9._/-]{2,20}\b", source):
            mentions.append(item.strip())

        for item in re.findall(r"[\u4e00-\u9fff]{2,12}", source):
            mentions.append(item.strip())

        dedup = []
        for item in mentions:
            if not item or item in dedup:
                continue
            token = item.strip()
            token_l = token.lower()
            # 通用噪音过滤：剔除 schema/代码键名和纯符号
            if token_l in {
                "json", "object", "action_type", "target", "input_value", "thought",
                "selector", "action", "input", "output", "null", "none",
            }:
                continue
            if re.fullmatch(r"[\W_]+", token):
                continue
            if len(token) <= 1:
                continue
            dedup.append(token)
        return dedup

    @classmethod
    def _select_best_dom_target(cls, thought: str, dom_snapshot: str) -> Optional[str]:
        """基于通用文本重合度，在 DOM 快照里选最可能目标（无业务特化规则）。"""
        entries = cls._parse_dom_snapshot_entries(dom_snapshot)
        if not entries:
            return None

        mentions = cls._extract_semantic_mentions(thought)
        if not mentions:
            return None

        best_entry = None
        best_score = 0

        for entry in entries:
            text = entry["text"]
            text_lower = text.lower()
            raw_lower = entry["raw"].lower()
            score = 0

            for mention in mentions:
                mention_lower = mention.lower()
                if not mention_lower:
                    continue
                if text_lower == mention_lower:
                    score += 14
                elif mention_lower in text_lower:
                    score += max(8, len(mention_lower) + 2)
                elif mention_lower in raw_lower:
                    score += max(5, len(mention_lower))
            if entry["tag"] == "button":
                score += 1
            if entry["href"]:
                score += 1

            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry and best_score > 0:
            if best_entry.get("testid"):
                return f'[data-testid="{best_entry["testid"]}"]'
            return f'[hexa-id="{best_entry["id"]}"]'
        return None

    @classmethod
    def _recover_target_from_dom_snapshot(cls, payload: dict, dom_snapshot: str) -> dict:
        """在模型缺少 target 时，利用当前 DOM 快照补齐目标。"""
        if not isinstance(payload, dict):
            return payload
        action = (payload.get("action_type") or "").strip().lower()
        target = payload.get("target")
        if action not in ("click", "type", "click_type_enter") or target:
            return payload

        thought = payload.get("thought", "") or ""
        recovered = cls._select_best_dom_target(thought, dom_snapshot)
        if recovered:
            payload["target"] = recovered
            return payload

        # 如果 thought 提到了 hexa-id，但 target 还没补上，再从 DOM 确认一次
        m2 = re.search(r"\bhexa-(\d+)\b", thought, re.IGNORECASE)
        if m2:
            hid = f"hexa-{m2.group(1)}"
            if hid in (dom_snapshot or ""):
                payload["target"] = f'[hexa-id="{hid}"]'
                return payload
        return payload

    @classmethod
    def _align_hexa_target_with_thought(cls, payload: dict, dom_snapshot: str) -> dict:
        """校验 hexa-id 与 thought 一致性，不一致则按通用语义重选。"""
        if not isinstance(payload, dict):
            return payload
        action = (payload.get("action_type") or "").strip().lower()
        target = str(payload.get("target") or "").strip()
        if action not in ("click", "type", "click_type_enter"):
            return payload
        if "hexa-id" not in target:
            return payload

        thought = str(payload.get("thought") or "")
        entries = cls._parse_dom_snapshot_entries(dom_snapshot)
        if not entries:
            return payload

        m = re.search(r'hexa-id\s*=\s*["\'](hexa-\d+)["\']', target)
        if not m:
            return payload
        current_hid = m.group(1)
        cur_entry = next((e for e in entries if e.get("id") == current_hid), None)
        cur_text = (cur_entry.get("text") if cur_entry else "") or ""

        mentions = cls._extract_semantic_mentions(thought)
        if not mentions:
            return payload

        cur_text_norm = cur_text.strip().lower()
        if cur_text_norm and any(m.lower() in cur_text_norm for m in mentions):
            return payload

        # 重新打分：优先文本重合高、具备 testid/hexa 稳定锚点的元素
        best_entry = None
        best_score = 0
        for entry in entries:
            txt = (entry.get("text") or "").strip().lower()
            raw = (entry.get("raw") or "").lower()
            if not txt and not raw:
                continue
            score = 0
            for m in mentions:
                ml = m.lower().strip()
                if not ml:
                    continue
                if txt == ml:
                    score += 12
                elif ml in txt:
                    score += max(6, len(ml))
                elif ml in raw:
                    score += max(4, len(ml) // 2)
            if entry.get("testid"):
                score += 2
            if entry.get("id"):
                score += 1
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry and best_score > 0:
            if best_entry.get("testid"):
                payload["target"] = f'[data-testid="{best_entry["testid"]}"]'
            else:
                payload["target"] = f'[hexa-id="{best_entry["id"]}"]'
            return payload

        return payload

    async def _chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str = "",
        screenshot_paths: Optional[list[str]] = None,
    ) -> dict:
        """统一模型调用入口：优先 JSON 解析，失败时做容错重试。"""
        raw = await self.llm.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt + "\nReturn ONLY one JSON object.",
            screenshot_path=screenshot_path,
            screenshot_paths=screenshot_paths,
            temperature=0.1,
        )
        payload = extract_json_object(raw)
        if payload:
            payload = self._normalize_payload(payload)
            payload = self._recover_target_from_context(payload, raw)
            if not payload.get("thought"):
                payload["thought"] = raw[:220] or "model returned no thought"
            return payload
        # 兼容半结构化自然语言输出
        parsed = self._extract_action_from_text(raw)
        if parsed:
            parsed = self._recover_target_from_context(parsed, raw)
            if not parsed.get("thought"):
                parsed["thought"] = raw[:220] or "parsed from non-json model output"
            return parsed
        # 最后做一次更强约束重试，避免直接触发 refresh 兜底
        retry_raw = await self.llm.complete(
            system_prompt=system_prompt,
            user_prompt=(
                "Return ONLY strict JSON object with keys: thought, action_type, target, input_value.\n"
                f"Previous model output:\n{raw[:1200]}"
            ),
            screenshot_path=screenshot_path,
            screenshot_paths=screenshot_paths,
            temperature=0.0,
        )
        payload = extract_json_object(retry_raw)
        if payload:
            payload = self._normalize_payload(payload)
            payload = self._recover_target_from_context(payload, retry_raw)
            if not payload.get("thought"):
                payload["thought"] = retry_raw[:220] or "model returned no thought"
            return payload
        parsed = self._extract_action_from_text(retry_raw)
        parsed = self._recover_target_from_context(parsed, retry_raw)
        if isinstance(parsed, dict) and not parsed.get("thought"):
            parsed["thought"] = retry_raw[:220] or "model returned no thought"
        return parsed

    async def summarize_page(
        self,
        goal: str,
        history: str,
        current_url: str,
        dom_snapshot: str,
        instruction: str = "",
        screenshot_path: str = "",
    ) -> str:
        """生成页面文字总结（用于纯分析/汇报类任务）。"""
        prompt = f"""
        USER GOAL: {goal}
        OPTIONAL INSTRUCTION: {instruction or "(none)"}
        ACTION HISTORY:
        {history if history else "No actions taken yet."}
        CURRENT URL: {current_url}
        CURRENT SCREEN (Interactive Elements):
        {dom_snapshot}

        Task:
        - Provide a concise Chinese summary for user reading.
        - If discussing token/price trend, include key observation points and risk hints.
        - Keep it practical and short (4-8 lines).
        """
        try:
            return await self.llm.complete(
                system_prompt="You are a concise web-page summarizer for RPA.",
                user_prompt=prompt,
                screenshot_path=screenshot_path,
                temperature=0.2,
            )
        except Exception as e:
            logger.warning(f"⚠️ [Agent] 页面总结失败: {e}")
            return ""

    async def decide_next_action(
        self,
        goal: str,
        history: str,
        current_url: str,
        dom_snapshot: str,
        screenshot_path: str = "",
    ) -> NextAction:
        """主决策入口：基于目标、历史、URL、DOM（可含截图）输出下一步动作。"""
        history_lines = [line.strip() for line in (history or "").splitlines() if line.strip()]
        recent_two = "\n".join(history_lines[-2:]) if history_lines else "No recent actions."
        prompt = f"""
        USER GOAL: {goal}
        
        ACTION HISTORY SO FAR:
        {history if history else "No actions taken yet."}

        MOST RECENT 2 ACTIONS:
        {recent_two}
        
        CURRENT URL: {current_url}
        
        CURRENT SCREEN (Interactive Elements):
        {dom_snapshot}
        
        Based on the above, what is the SINGLE next action to take?
        Do not repeat a previously successful opener button if the modal/panel it opened is already visible now.
        """
        
        try:
            payload = await self._chat_json(
                system_prompt=self.system_prompt,
                user_prompt=prompt,
                screenshot_path=screenshot_path,
            )
            payload = self._normalize_payload(payload if isinstance(payload, dict) else {})
            if self._is_invalid_target(payload.get("target")):
                payload["target"] = None
            payload = self._recover_target_from_dom_snapshot(payload, dom_snapshot)
            payload = self._align_hexa_target_with_thought(payload, dom_snapshot)
            if self._is_thought_action_contradictory(
                payload.get("thought", ""),
                payload.get("action_type", ""),
                payload.get("target"),
            ):
                logger.warning("⚠️ [Agent] 检测到 thought 与 action 冲突，触发一次重决策。")
                repair_prompt = (
                    prompt
                    + "\n\nYour previous output was contradictory: you said current token should be skipped, "
                      "but still clicked it. Re-plan now. If token mismatches conditions, DO NOT click it."
                )
                repaired = await self._chat_json(
                    system_prompt=self.system_prompt,
                    user_prompt=repair_prompt,
                    screenshot_path=screenshot_path,
                )
                repaired = self._normalize_payload(repaired if isinstance(repaired, dict) else {})
                if self._is_invalid_target(repaired.get("target")):
                    repaired["target"] = None
                repaired = self._recover_target_from_dom_snapshot(repaired, dom_snapshot)
                repaired = self._align_hexa_target_with_thought(repaired, dom_snapshot)
                contradiction_after_repair = self._is_thought_action_contradictory(
                    repaired.get("thought", ""),
                    repaired.get("action_type", ""),
                    repaired.get("target"),
                )
                if not contradiction_after_repair:
                    payload = repaired
                else:
                    # 二次重决策仍冲突：强制丢弃该目标，避免继续点击同一错误对象
                    payload["thought"] = (
                        (payload.get("thought") or "")
                        + " | contradictory target rejected by guard; fallback to refresh"
                    ).strip()
                    payload["action_type"] = "refresh"
                    payload["target"] = None
                    payload["input_value"] = None
            payload.setdefault("thought", "model returned no thought")
            payload.setdefault("action_type", "refresh")
            if payload.get("action_type") in ("click", "type", "click_type_enter") and not payload.get("target"):
                # 最后一层补救：从 thought 中再提取 hexa-id
                payload = self._recover_target_from_context(payload, payload.get("thought", ""))
                payload = self._recover_target_from_dom_snapshot(payload, dom_snapshot)
            if self._is_invalid_target(payload.get("target")):
                payload["target"] = None
            if payload.get("action_type") in ("click", "type", "click_type_enter") and not payload.get("target"):
                payload["thought"] = (payload.get("thought") or "") + " | missing target, fallback to refresh"
                payload["action_type"] = "refresh"
                payload["target"] = None
                payload["input_value"] = None
                logger.warning("⚠️ [Agent] 缺少可执行 target，已降级为 refresh")
            action = NextAction.model_validate(payload)
            logger.info(f"🧠 [Agent 思考]: {action.thought}")
            return action
        except Exception as e:
            logger.error(f"❌ [Agent 决策失败]: {e}")
            raise

    async def analyze_manual_action(self, goal: str, action_data: dict) -> AnalyzedAction:
        """录制阶段分析人工动作意图，生成可读步骤描述。"""
        fp = action_data.get('fingerprint', {})
        action_type = action_data.get('action_type', 'click')
        input_value = action_data.get('input_value', '')
        prompt = f"""
        USER GOAL: {goal}
        
        The user just manually performed an action:
        - Action type: {action_type}
        - Input value: {input_value if input_value else "(none)"}
        
        The target element has the following properties:
        - Tag: {fp.get('tag_name')}
        - Text: {fp.get('text')}
        - Aria-label: {fp.get('aria_label')}
        - Classes: {fp.get('classes')}
        
        Analyze why the user made this action to achieve the goal, and provide a short step description in Chinese.
        If action_type is 'type', the description should mention input content intention.
        """
        
        try:
            logger.info("🧠 [Agent] 正在分析用户的操作意图...")
            payload = await self._chat_json(
                system_prompt="You are a helpful RPA action analyzer. Return JSON with keys: thought, description.",
                user_prompt=prompt,
            )
            analysis = AnalyzedAction.model_validate(payload)
            return analysis
        except Exception as e:
            logger.error(f"❌ AI 分析失败: {e}")
            return AnalyzedAction(thought="Failed to analyze.", description="执行点击操作")

    async def decide_click_coordinate(
        self,
        goal: str,
        history: str,
        current_url: str,
        selector: str,
        candidates: list[dict],
        screenshot_path: str = "",
    ) -> ClickCoordinateDecision:
        """当 selector 存在多目标歧义时，返回坐标或候选索引做消歧。"""
        prompt = f"""
        USER GOAL: {goal}

        ACTION HISTORY:
        {history if history else "No history."}

        CURRENT URL: {current_url}
        AMBIGUOUS SELECTOR: {selector}

        MULTIPLE MATCH CANDIDATES (JSON):
        {json.dumps(candidates, ensure_ascii=False)}

        Task:
        - These candidates are different elements with the same/similar text.
        - Choose the single most correct one for the goal.
        - Prefer returning coordinates (x_ratio/y_ratio in [0,1]) for stable click.
        - If not confident with coordinate, return candidate_index.
        - If none is reliable, set use_coordinate=false and candidate_index=null.
        """

        system_prompt = """
        You are a coordinate disambiguation assistant for browser automation.
        Return ONLY JSON with keys:
        - thought: string
        - use_coordinate: boolean
        - x_ratio: number|null
        - y_ratio: number|null
        - candidate_index: integer|null
        - confidence: number (0~1)

        Rules:
        1) If screenshot shows clear target, set use_coordinate=true with x_ratio/y_ratio.
        2) If coordinate uncertain but a candidate item is clearly correct, set candidate_index.
        3) Never guess randomly.
        """
        try:
            payload = await self._chat_json(
                system_prompt=system_prompt,
                user_prompt=prompt,
                screenshot_path=screenshot_path,
            )
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("thought", "coordinate decision fallback")
            payload.setdefault("use_coordinate", False)
            payload.setdefault("x_ratio", None)
            payload.setdefault("y_ratio", None)
            payload.setdefault("candidate_index", None)
            payload.setdefault("confidence", 0.0)
            return ClickCoordinateDecision.model_validate(payload)
        except Exception:
            return ClickCoordinateDecision(
                thought="coordinate decision failed",
                use_coordinate=False,
                confidence=0.0,
            )

    async def repair_failed_replay_step(
        self,
        task_name: str,
        recent_steps: str,
        current_url: str,
        dom_snapshot: str,
        failed_step_desc: str,
        failed_action_type: str,
        failed_target: str,
        last_error: str,
        screenshot_path: str = "",
        screenshot_paths: Optional[list[str]] = None,
    ) -> ReplayRepairDecision:
        """回放失败修复决策入口：给出重试/替换/跳过/人工接管等策略。"""
        routed = self.special_router.route_repair(
            last_error=last_error,
            dom_snapshot=dom_snapshot,
            failed_target=failed_target,
        )
        if routed:
            logger.info(
                f"🧩 [SpecialAgent] strategy={routed.strategy} confidence={routed.confidence:.2f} thought={routed.thought}"
            )
            return routed

        screenshot_paths = screenshot_paths or []
        if screenshot_path and screenshot_path not in screenshot_paths:
            screenshot_paths.insert(0, screenshot_path)
        screenshot_paths = screenshot_paths[:4]

        prompt = f"""
        TASK NAME: {task_name}
        RECENT STEPS:
        {recent_steps}

        FAILED STEP:
        - description: {failed_step_desc}
        - action_type: {failed_action_type}
        - target: {failed_target}
        - error + attempts context: {last_error}
        - screenshot_path: {screenshot_path}
        - screenshot_paths: {screenshot_paths}

        CURRENT URL: {current_url}
        CURRENT INTERACTIVE DOM:
        {dom_snapshot}

        Decide one best recovery action now and return ONLY ONE JSON object:
        {{
          "thought": "...",
          "strategy": "retry_with_new_selector|replace_action|skip_step|human_handoff|no_fix",
          "action_type": "navigate|click|type|click_type_enter|press_enter|refresh|call_tool|wait_for_timeout|ensure_quote_token|click_relative|null",
          "target": "selector-or-url-or-null",
          "input_value": "value-or-null",
          "skip_reason": "reason-or-null",
          "confidence": 0.0
        }}
        """

        system_prompt = """
        You are an autonomous runtime-heal agent for web RPA.
        Goal: recover safely with minimal side effects.

        MUST FOLLOW:
        1) Prefer retry_with_new_selector if intent unchanged.
        2) Use replace_action if page state changed and another action is needed.
        3) Use skip_step only if clearly optional/redundant.
        4) Use human_handoff for login/captcha/2FA/wallet unlock/signature.
        5) If no reliable action, return no_fix.
        6) If previous attempts already failed, avoid repeating the same weak fix.
        7) Prefer selector-based actions. Use click_relative only as a last fallback
           when selector is impossible; input_value format: "x_ratio,y_ratio" in [0,1].
        8) If a tutorial/onboarding modal blocks the page, prefer closing it via top-right
           close button (X/关闭/跳过) instead of clicking 下一步 repeatedly.
        """

        try:
            payload = await self._chat_json(
                system_prompt=system_prompt,
                user_prompt=prompt,
                screenshot_path=screenshot_path,
                screenshot_paths=screenshot_paths,
            )
            decision = ReplayRepairDecision.model_validate(payload)
            logger.info(
                f"🧠 [AIHeal] strategy={decision.strategy} confidence={decision.confidence:.2f} thought={decision.thought}"
            )
            return decision
        except Exception as e:
            logger.error(f"❌ [AIHeal] 决策失败: {e}")
            return ReplayRepairDecision(
                thought="repair model failed",
                strategy="no_fix",
                confidence=0.0,
            )
