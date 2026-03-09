import logging
import json
import re
import ast
import base64
import mimetypes
from typing import Optional
from pydantic import BaseModel, Field
import instructor
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ReActAgent")

# ==========================================
# 1. 动态单步决策 Schema
# ==========================================
class NextAction(BaseModel):
    thought: str = Field(default="", description="Step-by-step reasoning: What is the goal? What is on the screen right now? What should I do next?")
    action_type: str = Field(description="MUST be one of: [navigate, click, type, click_type_enter, press_enter, refresh, done]")
    target: Optional[str] = Field(None, description="If action is 'navigate', put URL here. If click/type/click_type_enter, put selector, e.g., '[hexa-id=\"hexa-5\"]'")
    input_value: Optional[str] = Field(None, description="Text to input if action_type is 'type' or 'click_type_enter'.")

class AnalyzedAction(BaseModel):
    thought: str = Field(description="Analyze why the user clicked this element to achieve the goal.")
    description: str = Field(description="A concise description of the step, e.g., '点击登录按钮' or '点击搜索框'")


class ReplayRepairDecision(BaseModel):
    thought: str = Field(description="Why the previous step failed and what fix is most reliable now.")
    strategy: str = Field(
        description=(
            "MUST be one of: [retry_with_new_selector, replace_action, skip_step, human_handoff, no_fix]"
        )
    )
    action_type: Optional[str] = Field(
        None, description="Used when strategy=replace_action. One of [navigate, click, type, click_type_enter, press_enter, refresh, wait_for_timeout, ensure_quote_token, click_relative]."
    )
    target: Optional[str] = Field(
        None, description="New selector/URL when strategy=retry_with_new_selector or replace_action."
    )
    input_value: Optional[str] = Field(None, description="Input value when action_type='type'.")
    skip_reason: Optional[str] = Field(None, description="Reason when strategy=skip_step.")
    confidence: float = Field(default=0.5, ge=0, le=1)

# ==========================================
# 2. 动态大脑核心逻辑
# ==========================================
class ReActAgent:
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.model_name = model_name
        self.raw_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.client = instructor.from_openai(self.raw_client)
        self.system_prompt = """
        You are an autonomous Web RPA Agent. 
        You will be given a User Goal, action history, current URL, and interactive elements.
        
        CRITICAL RULES:
        1. If the current page matches the goal, output action_type='done'.
        2. Prefer semantic selectors by visible text/role first, e.g.:
           - button:has-text("行情")
           - a:has-text("OKX Boost")
           - [role="button"]:has-text("连接钱包")
        3. Avoid fragile hashed class selectors and avoid nth-child when possible.
        4. Use hexa-id selector only when semantic selectors are ambiguous.
        4. Explain your logic in the 'thought' field before acting.
        5. For input boxes that require submit, prefer action_type='click_type_enter'.
        6. Use action_type='refresh' when page is stale or blocked by transient UI state.
        7. If a modal/dialog/popover is already open, DO NOT click the opener again. Act inside the modal.
        8. If the previous 1-2 steps already opened a panel/modal, choose the next control inside it, not the old entry button.
        """

    @staticmethod
    def _image_to_data_url(path: str) -> str:
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

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        if not text:
            return {}
        raw = text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start:end + 1]
        try:
            return json.loads(raw)
        except Exception:
            pass
        # 兼容单引号字典 / Python字面量格式
        try:
            obj = ast.literal_eval(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return {}

    @staticmethod
    def _extract_action_from_text(text: str) -> dict:
        if not text:
            return {}
        lower = text.lower()
        action = None
        for a in ["click_type_enter", "press_enter", "refresh", "navigate", "click", "type", "done"]:
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
        if not isinstance(payload, dict):
            return {}
        normalized = dict(payload)
        if not normalized.get("action_type"):
            normalized["action_type"] = normalized.get("action")
        if not normalized.get("target"):
            normalized["target"] = normalized.get("selector")
        if normalized.get("input_value") is None:
            for key in ("input", "text", "value"):
                if normalized.get(key) is not None:
                    normalized["input_value"] = str(normalized.get(key))
                    break
        target = normalized.get("target")
        if isinstance(target, str):
            normalized["target"] = target.strip().strip("`")
        return normalized

    @staticmethod
    def _is_invalid_target(target: Optional[str]) -> bool:
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
        return False

    @staticmethod
    def _recover_target_from_context(payload: dict, raw_hint: str) -> dict:
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
            if mention in {"当前页面", "连接钱包页面", "钱包连接", "交易页面"}:
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
        entries = []
        for line in (dom_snapshot or "").splitlines():
            id_match = re.search(r"\[ID:\s*(hexa-\d+)\]", line)
            if not id_match:
                continue
            text_match = re.search(r'text="([^"]*)"', line)
            tag_match = re.search(r"<([a-zA-Z0-9]+)\b", line)
            href_match = re.search(r'href="([^"]*)"', line)
            aria_match = re.search(r'aria-label="([^"]*)"', line)
            text = (text_match.group(1).strip() if text_match else "")
            entries.append(
                {
                    "id": id_match.group(1),
                    "text": text,
                    "tag": (tag_match.group(1).lower() if tag_match else ""),
                    "href": (href_match.group(1) if href_match else ""),
                    "aria_label": (aria_match.group(1) if aria_match else ""),
                    "raw": line,
                }
            )
        return entries

    @staticmethod
    def _extract_semantic_mentions(text: str) -> list[str]:
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
        generic_terms = {
            "当前页面", "下一步", "用户目标", "代币详情页", "详情页", "页面", "目标", "步骤",
            "进入", "点击", "代币", "币种", "当前", "需要", "因此", "应该", "榜单", "列表",
            "json", "object", "action_type", "target", "input_value", "thought",
        }
        for item in mentions:
            if not item or item in dedup:
                continue
            if item in generic_terms:
                continue
            dedup.append(item)
        return dedup

    @classmethod
    def _select_best_dom_target(cls, thought: str, dom_snapshot: str) -> Optional[str]:
        entries = cls._parse_dom_snapshot_entries(dom_snapshot)
        if not entries:
            return None

        mentions = cls._extract_semantic_mentions(thought)
        if not mentions:
            return None

        thought_lower = (thought or "").lower()
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

            if any(k in thought_lower for k in ["最高", "第一", "详情", "币价", "最高价"]):
                if re.fullmatch(r"[A-Z][A-Z0-9._/-]{2,20}", text or ""):
                    score += 10
                if "buy" in raw_lower or "买入" in raw_lower:
                    score += 4

            # Generic intent signals: when thought asks to connect/start/confirm, prefer actionable controls.
            if any(k in thought_lower for k in ["connect", "连接", "start", "开始", "confirm", "确认"]):
                if entry["tag"] in ("button", "a"):
                    score += 3
                if "button" in raw_lower or "role=\"button\"" in raw_lower:
                    score += 2
            if entry["tag"] == "button":
                score += 1
            if entry["href"]:
                score += 1

            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry and best_score > 0:
            return f'[hexa-id="{best_entry["id"]}"]'
        return None

    @classmethod
    def _recover_target_from_dom_snapshot(cls, payload: dict, dom_snapshot: str) -> dict:
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

    async def _chat_json(self, system_prompt: str, user_prompt: str, screenshot_path: str = "") -> dict:
        content = [{"type": "text", "text": user_prompt + "\nReturn ONLY one JSON object."}]
        if screenshot_path:
            data_url = self._image_to_data_url(screenshot_path)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        resp = await self.raw_client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
        )
        raw = (resp.choices[0].message.content or "").strip()
        payload = self._extract_json_object(raw)
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
        retry_resp = await self.raw_client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Return ONLY strict JSON object with keys: thought, action_type, target, input_value."},
                {"role": "assistant", "content": raw[:1200]},
            ],
            temperature=0.0,
        )
        retry_raw = (retry_resp.choices[0].message.content or "").strip()
        payload = self._extract_json_object(retry_raw)
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

    async def decide_next_action(
        self,
        goal: str,
        history: str,
        current_url: str,
        dom_snapshot: str,
        screenshot_path: str = "",
    ) -> NextAction:
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
        
        call_kwargs = {
            "model": self.model_name,
            "response_model": NextAction,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        
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
        """AI 旁观者：分析用户的物理点击动作"""
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
        
        call_kwargs = {
            "model": self.model_name,
            "response_model": AnalyzedAction,
            "messages": [
                {"role": "system", "content": "You are a helpful RPA action analyzer."},
                {"role": "user", "content": prompt}
            ]
        }
        
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
    ) -> ReplayRepairDecision:
        prompt = f"""
        TASK NAME: {task_name}
        RECENT REPLAY STEPS:
        {recent_steps}

        FAILED STEP:
        - description: {failed_step_desc}
        - action_type: {failed_action_type}
        - target: {failed_target}
        - error: {last_error}

        CURRENT URL: {current_url}
        CURRENT INTERACTIVE DOM:
        {dom_snapshot}

        Decide the best single recovery strategy now.
        """

        system_prompt = """
        You are a replay-repair agent for web automation.
        Your task is to recover from a failed deterministic step with minimal risk.

        STRICT RULES:
        1) Prefer retry_with_new_selector when intent is still same but selector changed.
        2) Use replace_action when current page state changed and another action is needed.
        3) Use skip_step only when this step is clearly optional/redundant.
        4) Use human_handoff for login/captcha/2FA/wallet unlock/signature scenarios.
        5) If no reliable fix, use no_fix.
        6) Never invent impossible selectors; prefer concise text selectors or robust css.
        7) For token dropdown mismatch, prefer replace_action with action_type='ensure_quote_token',
           input_value='<TARGET_SYMBOL>' and optional target='<dropdown button selector>'.
        8) If an input action must submit, prefer action_type='click_type_enter'.
        """

        try:
            payload = await self._chat_json(
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
            decision = ReplayRepairDecision.model_validate(payload)
            logger.info(
                f"🧠 [ReplayRepair] strategy={decision.strategy} confidence={decision.confidence:.2f} thought={decision.thought}"
            )
            return decision
        except Exception as e:
            logger.error(f"❌ [ReplayRepair] 决策失败: {e}")
            return ReplayRepairDecision(
                thought="repair model failed",
                strategy="no_fix",
                confidence=0.0,
            )
