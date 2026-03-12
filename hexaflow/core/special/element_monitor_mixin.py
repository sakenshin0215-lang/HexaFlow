import asyncio
import json
import logging
import os
import re
from datetime import datetime

from hexaflow.browser.element_sidecar import ElementSidecarPanel


logger = logging.getLogger("HexaEngine")


class EngineElementMonitorMixin:
    def _start_sidecar_action_pump(self):
        task = getattr(self, "_element_sidecar_action_task", None)
        if task is not None and not task.done():
            return
        self._element_sidecar_action_task = asyncio.create_task(self._sidecar_action_pump_loop())

    def _ensure_element_sidecar(self, page):
        enabled = os.getenv("ENABLE_LIVE_ELEMENT_SIDECAR", "1") == "1"
        if not enabled:
            return
        if getattr(self, "_element_sidecar", None) is None:
            self._element_sidecar = ElementSidecarPanel()
            self._element_sidecar.start()
        self._element_sidecar_page = page
        self._start_sidecar_action_pump()

    async def _sidecar_action_pump_loop(self):
        try:
            while True:
                sidecar = getattr(self, "_element_sidecar", None)
                if sidecar is not None:
                    for action in sidecar.drain_actions():
                        if not isinstance(action, dict):
                            continue
                        if action.get("type") == "highlight":
                            hexa_id = str(action.get("hexa_id") or "").strip()
                            if hexa_id:
                                await self._highlight_hexa_id_for_sidecar(hexa_id)
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            return

    async def _highlight_hexa_id_for_sidecar(self, hexa_id: str):
        page = getattr(self, "_element_sidecar_page", None)
        if page is None:
            return
        selector = f'[hexa-id="{hexa_id}"]'
        try:
            await page.evaluate(
                """(sel) => {
                    const KEY = '__hexa_sidecar_hl__';
                    const old = window[KEY];
                    if (old && old.el) {
                        old.el.style.outline = old.outline || '';
                        old.el.style.boxShadow = old.shadow || '';
                        old.el.style.transition = old.transition || '';
                    }
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const prev = {
                        el: el,
                        outline: el.style.outline || '',
                        shadow: el.style.boxShadow || '',
                        transition: el.style.transition || '',
                    };
                    window[KEY] = prev;
                    el.style.transition = 'outline .18s ease, box-shadow .18s ease';
                    el.style.outline = '3px solid #22D3EE';
                    el.style.boxShadow = '0 0 0 4px rgba(34,211,238,.32)';
                    // 仅视觉高亮，短暂闪烁后恢复
                    setTimeout(() => {
                        const cur = window[KEY];
                        if (cur && cur.el === el) {
                            el.style.outline = prev.outline || '';
                            el.style.boxShadow = prev.shadow || '';
                            el.style.transition = prev.transition || '';
                            window[KEY] = null;
                        }
                    }, 1200);
                    return true;
                }""",
                selector,
            )
        except Exception:
            return

    def _init_element_monitor(self, run_key: str, task_name: str, mode: str):
        if not hasattr(self, "_element_monitors"):
            self._element_monitors = {}
        self._element_monitors[run_key] = {
            "run_key": run_key,
            "task_name": task_name,
            "mode": mode,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "steps": [],
            "summary": {
                "step_count": 0,
                "clickable_sum": 0,
                "input_sum": 0,
                "max_clickable": 0,
                "max_input": 0,
                "last_clickable": 0,
                "last_input": 0,
            },
        }

    @staticmethod
    def _parse_dom_snapshot_counts(dom_snapshot: str):
        clickable = 0
        input_count = 0
        sample_clickable = []
        sample_input = []

        if not dom_snapshot or "No interactive elements found" in dom_snapshot:
            return {
                "clickable": 0,
                "input_count": 0,
                "sample_clickable": [],
                "sample_input": [],
                "line_count": 0,
            }

        lines = [line.strip() for line in dom_snapshot.splitlines() if line.strip()]
        for line in lines:
            m_tag = re.search(r"<([a-zA-Z0-9_-]+)", line)
            tag = (m_tag.group(1).lower() if m_tag else "")
            m_text = re.search(r'text="([^"]+)"', line)
            text = m_text.group(1).strip() if m_text else ""
            m_aria = re.search(r'aria-label="([^"]+)"', line)
            aria = m_aria.group(1).strip() if m_aria else ""
            label = text or aria or tag or "unknown"

            if tag in {"input", "textarea", "select"}:
                input_count += 1
                if len(sample_input) < 5:
                    sample_input.append(label)
            else:
                clickable += 1
                if len(sample_clickable) < 5:
                    sample_clickable.append(label)

        return {
            "clickable": clickable,
            "input_count": input_count,
            "sample_clickable": sample_clickable,
            "sample_input": sample_input,
            "line_count": len(lines),
        }

    @staticmethod
    def _extract_items_from_snapshot(dom_snapshot: str, limit_per_type: int | None = None):
        clickable_items = []
        input_items = []
        if not dom_snapshot or "No interactive elements found" in dom_snapshot:
            return clickable_items, input_items

        lines = [line.strip() for line in dom_snapshot.splitlines() if line.strip()]
        for line in lines:
            m_tag = re.search(r"<([a-zA-Z0-9_-]+)", line)
            tag = (m_tag.group(1).lower() if m_tag else "")
            m_id = re.search(r"\[ID:\s*([^\]]+)\]", line)
            hexa_id = m_id.group(1).strip() if m_id else ""
            m_text = re.search(r'text="([^"]+)"', line)
            text = m_text.group(1).strip() if m_text else ""
            m_aria = re.search(r'aria-label="([^"]+)"', line)
            aria = m_aria.group(1).strip() if m_aria else ""
            m_testid = re.search(r'data-testid="([^"]+)"', line)
            testid = m_testid.group(1).strip() if m_testid else ""

            label = text or aria or testid or tag or "unknown"
            item = {"hexa_id": hexa_id, "tag": tag, "label": label, "line": line}
            if tag in {"input", "textarea", "select"}:
                if (limit_per_type is None) or (limit_per_type <= 0) or (len(input_items) < limit_per_type):
                    input_items.append(item)
            else:
                if (limit_per_type is None) or (limit_per_type <= 0) or (len(clickable_items) < limit_per_type):
                    clickable_items.append(item)
        return clickable_items, input_items

    def _update_element_monitor(
        self,
        run_key: str,
        step_label: str,
        dom_snapshot: str,
        current_url: str = "",
    ):
        if not hasattr(self, "_element_monitors"):
            return
        monitor = self._element_monitors.get(run_key)
        if not monitor:
            return

        parsed = self._parse_dom_snapshot_counts(dom_snapshot)
        summary = monitor["summary"]
        prev_clickable = summary["last_clickable"]
        prev_input = summary["last_input"]
        delta_clickable = parsed["clickable"] - prev_clickable
        delta_input = parsed["input_count"] - prev_input

        summary["step_count"] += 1
        summary["clickable_sum"] += parsed["clickable"]
        summary["input_sum"] += parsed["input_count"]
        summary["max_clickable"] = max(summary["max_clickable"], parsed["clickable"])
        summary["max_input"] = max(summary["max_input"], parsed["input_count"])
        summary["last_clickable"] = parsed["clickable"]
        summary["last_input"] = parsed["input_count"]

        monitor["steps"].append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "step": step_label,
                "url": current_url,
                "clickable": parsed["clickable"],
                "input_count": parsed["input_count"],
                "delta_clickable": delta_clickable,
                "delta_input": delta_input,
                "line_count": parsed["line_count"],
                "sample_clickable": parsed["sample_clickable"],
                "sample_input": parsed["sample_input"],
            }
        )

        logger.info(
            "📊 [ElementMonitor] %s cur(click=%d,input=%d) delta(click=%+d,input=%+d) sum(click=%d,input=%d)",
            step_label,
            parsed["clickable"],
            parsed["input_count"],
            delta_clickable,
            delta_input,
            summary["clickable_sum"],
            summary["input_sum"],
        )

        if parsed["clickable"] == 0 and parsed["input_count"] == 0:
            logger.warning("⚠️ [ElementMonitor] 当前快照未解析到可交互元素，请检查 DOM 抓取是否异常。")

    async def _update_live_element_overlay(
        self,
        page,
        run_key: str,
        step_label: str,
        dom_snapshot: str,
        current_url: str = "",
    ):
        self._ensure_element_sidecar(page)
        if not hasattr(self, "_element_monitors"):
            return
        monitor = self._element_monitors.get(run_key)
        if not monitor:
            return

        try:
            limit_per_type = int(os.getenv("ELEMENT_SIDECAR_MAX_ITEMS", "0"))
        except Exception:
            limit_per_type = 0
        clickable_items, input_items = self._extract_items_from_snapshot(
            dom_snapshot,
            limit_per_type=limit_per_type,
        )
        sidecar = getattr(self, "_element_sidecar", None)
        if sidecar is not None:
            sidecar.update(
                {
                    "step_label": step_label,
                    "url": current_url or "",
                    "clickable_items": clickable_items,
                    "input_items": input_items,
                }
            )

        enabled = os.getenv("ENABLE_LIVE_ELEMENT_OVERLAY", "0") == "1"
        if not enabled:
            return
        payload = {
            "title": f"HexaFlow Live Elements | {step_label}",
            "url": current_url or "",
            "clickable_count": len(clickable_items),
            "input_count": len(input_items),
            "clickable_items": clickable_items,
            "input_items": input_items,
        }
        payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if monitor.get("overlay_last_payload") == payload_str:
            return
        monitor["overlay_last_payload"] = payload_str

        await page.evaluate(
            """(data) => {
                const id = 'hexa-live-element-overlay';
                let root = document.getElementById(id);
                if (!root) {
                    root = document.createElement('div');
                    root.id = id;
                    root.style.position = 'fixed';
                    root.style.top = '12px';
                    root.style.right = '12px';
                    root.style.width = '420px';
                    root.style.maxHeight = '70vh';
                    root.style.overflow = 'hidden';
                    root.style.zIndex = '2147483647';
                    root.style.background = 'rgba(10,12,16,0.92)';
                    root.style.color = '#D7E2F0';
                    root.style.border = '1px solid rgba(130,160,210,0.45)';
                    root.style.borderRadius = '10px';
                    root.style.fontFamily = 'ui-monospace, SFMono-Regular, Menlo, monospace';
                    root.style.fontSize = '12px';
                    root.style.lineHeight = '1.4';
                    root.style.padding = '10px';
                    root.style.boxShadow = '0 8px 24px rgba(0,0,0,0.35)';
                    root.style.pointerEvents = 'none';
                    document.body.appendChild(root);
                }

                const esc = (s) => String(s || '')
                    .replaceAll('&', '&amp;')
                    .replaceAll('<', '&lt;')
                    .replaceAll('>', '&gt;');

                const renderItems = (items) => items.map((it, idx) =>
                    `<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                        <span style="color:#7FB3FF;">${idx + 1}.</span>
                        <span style="color:#9AE6B4;">${esc(it.tag || '')}</span>
                        <span style="color:#E2E8F0;">${esc(it.label || '')}</span>
                        <span style="color:#94A3B8;">${it.hexa_id ? '[' + esc(it.hexa_id) + ']' : ''}</span>
                    </div>`
                ).join('');

                root.innerHTML = `
                    <div style="font-weight:700;color:#F8FAFC;margin-bottom:6px;">${esc(data.title)}</div>
                    <div style="color:#A3B1C6;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(data.url)}</div>
                    <div style="display:flex;gap:8px;margin-bottom:8px;">
                        <div style="background:rgba(56,189,248,0.16);padding:3px 8px;border-radius:999px;">clickable: <b>${data.clickable_count}</b></div>
                        <div style="background:rgba(52,211,153,0.16);padding:3px 8px;border-radius:999px;">input: <b>${data.input_count}</b></div>
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                        <div style="min-width:0;">
                            <div style="color:#7FB3FF;font-weight:700;margin-bottom:4px;">Clickable</div>
                            <div style="max-height:45vh;overflow:auto;border:1px solid rgba(125,145,175,.35);padding:6px;border-radius:6px;background:rgba(15,23,42,.5);">
                                ${renderItems(data.clickable_items || []) || '<div style="color:#64748B;">(empty)</div>'}
                            </div>
                        </div>
                        <div style="min-width:0;">
                            <div style="color:#34D399;font-weight:700;margin-bottom:4px;">Input</div>
                            <div style="max-height:45vh;overflow:auto;border:1px solid rgba(125,145,175,.35);padding:6px;border-radius:6px;background:rgba(15,23,42,.5);">
                                ${renderItems(data.input_items || []) || '<div style="color:#64748B;">(empty)</div>'}
                            </div>
                        </div>
                    </div>
                `;
            }""",
            payload,
        )

    def _finalize_element_monitor(self, run_key: str):
        if not getattr(self, "save_reports", False):
            return {}
        if not hasattr(self, "_element_monitors"):
            return {}
        monitor = self._element_monitors.get(run_key)
        if not monitor:
            return {}

        monitor["ended_at"] = datetime.now().isoformat(timespec="seconds")
        summary = monitor["summary"]
        steps = max(1, summary["step_count"])
        monitor["summary"]["avg_clickable"] = round(summary["clickable_sum"] / steps, 2)
        monitor["summary"]["avg_input"] = round(summary["input_sum"] / steps, 2)

        report_dir = "workspace/reports"
        os.makedirs(report_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_run = str(run_key).replace("/", "_")
        json_path = os.path.join(report_dir, f"element_monitor_{safe_run}_{ts}.json")
        md_path = os.path.join(report_dir, f"element_monitor_{safe_run}_{ts}.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(monitor, f, ensure_ascii=False, indent=2)

        lines = [
            "# Element Monitor",
            "",
            f"- run_key: `{monitor['run_key']}`",
            f"- task_name: `{monitor['task_name']}`",
            f"- mode: `{monitor['mode']}`",
            f"- step_count: `{summary['step_count']}`",
            f"- clickable_sum: `{summary['clickable_sum']}`",
            f"- input_sum: `{summary['input_sum']}`",
            f"- avg_clickable: `{monitor['summary']['avg_clickable']}`",
            f"- avg_input: `{monitor['summary']['avg_input']}`",
            f"- max_clickable: `{summary['max_clickable']}`",
            f"- max_input: `{summary['max_input']}`",
            "",
            "## Last 10 Steps",
            "",
        ]
        for item in monitor["steps"][-10:]:
            lines.append(
                f"- {item['ts']} | {item['step']} | click={item['clickable']} "
                f"(Δ{item['delta_clickable']:+d}) | input={item['input_count']} "
                f"(Δ{item['delta_input']:+d})"
            )

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        logger.info(f"🧾 [ElementMonitor] 报告已生成: json={json_path} md={md_path}")
        return {"json": json_path, "md": md_path}
