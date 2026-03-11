import multiprocessing as mp
import os
import queue


def _run_sidecar_process(data_queue: "mp.Queue", action_queue: "mp.Queue"):
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return

    root = tk.Tk()
    root.title("HexaFlow Elements")
    root.geometry("560x760+1260+80")
    topmost = os.getenv("ELEMENT_SIDECAR_TOPMOST", "0") == "1"
    root.attributes("-topmost", topmost)

    top = ttk.Frame(root, padding=8)
    top.pack(fill=tk.BOTH, expand=True)

    var_step = tk.StringVar(value="step: -")
    var_url = tk.StringVar(value="url: -")
    var_counts = tk.StringVar(value="clickable: 0 | input: 0")

    ttk.Label(top, textvariable=var_step).pack(anchor="w")
    ttk.Label(top, textvariable=var_url, foreground="#444").pack(anchor="w")
    ttk.Label(top, textvariable=var_counts).pack(anchor="w", pady=(0, 6))

    notebook = ttk.Notebook(top)
    notebook.pack(fill=tk.BOTH, expand=True)

    click_frame = ttk.Frame(notebook)
    input_frame = ttk.Frame(notebook)
    notebook.add(click_frame, text="Clickable")
    notebook.add(input_frame, text="Input")

    def build_tree(parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(wrap, columns=("id", "tag", "label"), show="headings")
        tree.heading("id", text="hexa-id")
        tree.heading("tag", text="tag")
        tree.heading("label", text="label")
        tree.column("id", width=95, anchor="w")
        tree.column("tag", width=70, anchor="w")
        tree.column("label", width=180, anchor="w")
        ybar = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=ybar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ybar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    click_tree = build_tree(click_frame)
    input_tree = build_tree(input_frame)
    click_rows = {}
    input_rows = {}

    def render_tree(tree, items, row_cache: dict):
        tree.delete(*tree.get_children())
        row_cache.clear()
        for idx, it in enumerate(items, start=1):
            hexa_id = (it.get("hexa_id") or "").strip()
            tag = (it.get("tag") or "").strip()
            label = (it.get("label") or "").strip()
            iid = f"r{idx}"
            tree.insert("", tk.END, iid=iid, values=(hexa_id, tag, label))
            row_cache[iid] = it

    def on_tree_double_click(kind: str):
        tree = click_tree if kind == "click" else input_tree
        rows = click_rows if kind == "click" else input_rows
        selected = tree.selection()
        if not selected:
            return
        row = rows.get(selected[0])
        if not row:
            return
        hexa_id = (row.get("hexa_id") or "").strip()
        if not hexa_id:
            return
        try:
            action_queue.put_nowait({"type": "highlight", "hexa_id": hexa_id})
        except Exception:
            pass

    click_tree.bind("<Double-1>", lambda _e: on_tree_double_click("click"))
    input_tree.bind("<Double-1>", lambda _e: on_tree_double_click("input"))

    ttk.Label(
        top,
        text="双击列表项可在浏览器中高亮对应元素",
        foreground="#666",
    ).pack(anchor="w", pady=(6, 0))

    def tick():
        latest = None
        try:
            while True:
                latest = data_queue.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            pass

        if latest is not None:
            if latest.get("__cmd__") == "stop":
                root.destroy()
                return
            step = latest.get("step_label") or "-"
            url = latest.get("url") or "-"
            clickable = latest.get("clickable_items") or []
            inputs = latest.get("input_items") or []
            var_step.set(f"step: {step}")
            var_url.set(f"url: {url}")
            var_counts.set(f"clickable: {len(clickable)} | input: {len(inputs)}")
            render_tree(click_tree, clickable, click_rows)
            render_tree(input_tree, inputs, input_rows)

        root.after(180, tick)

    tick()
    root.mainloop()


class ElementSidecarPanel:
    def __init__(self):
        self._ctx = mp.get_context("spawn")
        self._data_queue = None
        self._action_queue = None
        self._proc = None

    def start(self):
        if self._proc is not None and self._proc.is_alive():
            return
        self._data_queue = self._ctx.Queue(maxsize=4)
        self._action_queue = self._ctx.Queue(maxsize=32)
        self._proc = self._ctx.Process(
            target=_run_sidecar_process,
            args=(self._data_queue, self._action_queue),
            daemon=True,
        )
        self._proc.start()

    def stop(self):
        if self._data_queue is not None:
            try:
                self._data_queue.put_nowait({"__cmd__": "stop"})
            except Exception:
                pass
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=1.0)

    def update(self, payload: dict):
        if self._data_queue is None:
            return
        try:
            self._data_queue.put_nowait(payload or {})
        except queue.Full:
            try:
                _ = self._data_queue.get_nowait()
            except Exception:
                pass
            try:
                self._data_queue.put_nowait(payload or {})
            except Exception:
                pass
        except Exception:
            pass

    def drain_actions(self):
        actions = []
        if self._action_queue is None:
            return actions
        try:
            while True:
                actions.append(self._action_queue.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        return actions
