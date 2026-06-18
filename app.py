"""
app.py
Java Thread Dump Analyzer — desktop GUI (Tkinter)

A local, offline alternative to fastthread.io: load a .txt thread dump
(jstack / jcmd / kill -3 output), get an instant summary dashboard with
charts, deadlock/blocked-thread alerts, and a searchable thread table
with full stack trace detail.

Run with:  python app.py
Requires:  matplotlib  (pip install matplotlib)
           google-generativeai (pip install google-generativeai)
           tkinter is part of the Python standard library on Windows/macOS
           installers; on Linux install it via your package manager, e.g.
           sudo apt-get install python3-tk
"""

import os
import json
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

os.environ["GEMINI_API_KEY"] = "AQ.Ab8RN6J1rPYetLsGNfZ3IEHSMr8NQGIIkIzbKYrR0e1zjrsSDQ"

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from parser import (
    parse_thread_dump_file,
    top_blocking_frames,
    group_by_stack_signature,
    thread_pool_buckets,
    Snapshot,
)
from diagnosis import diagnose, Diagnosis, Finding
from ai_analyzer import get_ai_analysis, LANGUAGE_NAMES

# Tempat penyimpanan cache lokal API Key Gemini agar tidak hilang saat aplikasi ditutup
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".thread_dump_analyzer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

SEVERITY_COLORS = {
    "CRITICAL": "#b71c1c",
    "HIGH": "#e65100",
    "MEDIUM": "#f9a825",
    "LOW": "#1565c0",
    "INFO": "#616161",
}
SEVERITY_ICON = {
    "CRITICAL": "⛔",
    "HIGH": "🔴",
    "MEDIUM": "🟠",
    "LOW": "🔵",
    "INFO": "ℹ",
}

APP_TITLE = "Thread Dump Analyzer (Gemini Edition)"
STATE_COLORS = {
    "RUNNABLE": "#2e7d32",
    "WAITING": "#f9a825",
    "TIMED_WAITING": "#fb8c00",
    "BLOCKED": "#c62828",
    "NEW": "#1565c0",
    "TERMINATED": "#616161",
    "UNKNOWN": "#9e9e9e",
}


def _load_saved_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


class ThreadDumpAnalyzerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x820")
        self.minsize(1000, 650)

        self.snapshots = []          
        self.snapshot_sources = []   
        self.loaded_filenames = []   
        self.current_snapshot_idx = 0
        self.current_snapshot = None     
        self.current_diagnosis = None    
        self.loaded_filename = None
        self._thread_lookup = {}     
        self._highlighted_thread_names = None  

        # Memuat API Key Gemini yang tersimpan dari run sebelumnya
        saved = _load_saved_config()
        if saved.get("gemini_api_key") and not os.environ.get("GEMINI_API_KEY"):
            os.environ["GEMINI_API_KEY"] = saved["gemini_api_key"]

        self._build_menu()
        self._build_layout()
        self._show_empty_state()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open Thread Dump...", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Add Another Thread Dump...", command=self.add_file, accelerator="Ctrl+Shift+O")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_command(label="Gemini API Key...", command=self._open_api_key_dialog)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        self.config(menu=menubar)
        self.bind("<Control-o>", lambda e: self.open_file())
        self.bind("<Control-O>", lambda e: self.add_file())

    def _open_api_key_dialog(self):
        current = os.environ.get("GEMINI_API_KEY", "")
        masked = ("•" * 8 + current[-4:]) if current else ""
        key = simpledialog.askstring(
            "Gemini API Key",
            "Enter your Gemini API key (used only for the AI Analysis tab).\n"
            "Get one at https://aistudio.google.com/\n"
            + (f"\nCurrently set: {masked}" if current else ""),
            show="*",
            parent=self,
        )
        if key is None:
            return  
        key = key.strip()
        if not key:
            return
        os.environ["GEMINI_API_KEY"] = key
        cfg = _load_saved_config()
        cfg["gemini_api_key"] = key
        _save_config(cfg)
        messagebox.showinfo("Gemini API Key", "Gemini API key saved.")

    def _build_layout(self):
        top_bar = ttk.Frame(self, padding=(10, 8))
        top_bar.pack(side="top", fill="x")

        ttk.Button(top_bar, text="📂 Open Thread Dump (.txt)", command=self.open_file).pack(side="left")
        ttk.Button(top_bar, text="➕ Add Another File", command=self.add_file).pack(side="left", padx=(6, 0))

        self.file_label = ttk.Label(top_bar, text="No file loaded", foreground="#666")
        self.file_label.pack(side="left", padx=12)

        self.snapshot_var = tk.StringVar()
        self.snapshot_combo = ttk.Combobox(
            top_bar, textvariable=self.snapshot_var, state="readonly", width=28
        )
        self.snapshot_combo.pack(side="right")
        self.snapshot_combo.bind("<<ComboboxSelected>>", self._on_snapshot_change)
        self.snapshot_label = ttk.Label(top_bar, text="")
        self.snapshot_label.pack(side="right", padx=8)

        self.verdict_banner = tk.Frame(self, bg="#eeeeee")
        self.verdict_banner.pack(side="top", fill="x")
        self.verdict_label = tk.Label(
            self.verdict_banner, text="Open a thread dump to get a diagnosis.",
            bg="#eeeeee", fg="#444", font=("", 11, "bold"), anchor="w", padx=14, pady=8,
        )
        self.verdict_label.pack(side="left", fill="x", expand=True)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))

        self.tab_diagnosis = ttk.Frame(self.notebook)
        self.tab_summary = ttk.Frame(self.notebook)
        self.tab_threads = ttk.Frame(self.notebook)
        self.tab_issues = ttk.Frame(self.notebook)
        self.tab_ai = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_diagnosis, text="  🩺 Diagnosis  ")
        self.notebook.add(self.tab_summary, text="  Summary  ")
        self.notebook.add(self.tab_threads, text="  Threads  ")
        self.notebook.add(self.tab_issues, text="  Issues & Deadlocks  ")
        self.notebook.add(self.tab_ai, text="  🤖 AI Analysis  ")

        self._build_diagnosis_tab()
        self._build_summary_tab()
        self._build_threads_tab()
        self._build_issues_tab()
        self._build_ai_tab()

        self.status_var = tk.StringVar(value="Ready.")
        status_bar = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 4))
        status_bar.pack(side="bottom", fill="x")

    def _build_diagnosis_tab(self):
        outer = ttk.Frame(self.tab_diagnosis, padding=14)
        outer.pack(fill="both", expand=True)

        self.diag_card = tk.Frame(outer, bg="#2e7d32", padx=18, pady=16)
        self.diag_card.pack(fill="x", pady=(0, 16))

        self.diag_icon_label = tk.Label(self.diag_card, text="✅", bg="#2e7d32", fg="white", font=("", 28))
        self.diag_icon_label.pack(side="left", padx=(0, 14))

        diag_text_frame = tk.Frame(self.diag_card, bg="#2e7d32")
        diag_text_frame.pack(side="left", fill="x", expand=True)
        self.diag_verdict_label = tk.Label(
            diag_text_frame, text="Open a thread dump to get a diagnosis.",
            bg="#2e7d32", fg="white", font=("", 14, "bold"), anchor="w", justify="left", wraplength=900,
        )
        self.diag_verdict_label.pack(anchor="w", fill="x")
        self.diag_subtitle_label = tk.Label(
            diag_text_frame, text="", bg="#2e7d32", fg="#e8f5e9", font=("", 9), anchor="w", justify="left",
        )
        self.diag_subtitle_label.pack(anchor="w", fill="x", pady=(4, 0))

        ttk.Label(outer, text="Findings, ranked by severity", font=("", 11, "bold")).pack(anchor="w", pady=(0, 6))

        findings_container = ttk.Frame(outer)
        findings_container.pack(fill="both", expand=True)

        self.findings_canvas = tk.Canvas(findings_container, highlightthickness=0, bg="#fafafa")
        findings_vsb = ttk.Scrollbar(findings_container, orient="vertical", command=self.findings_canvas.yview)
        self.findings_canvas.configure(yscrollcommand=findings_vsb.set)
        self.findings_canvas.pack(side="left", fill="both", expand=True)
        findings_vsb.pack(side="right", fill="y")

        self.findings_inner = tk.Frame(self.findings_canvas, bg="#fafafa")
        self._findings_inner_id = self.findings_canvas.create_window((0, 0), window=self.findings_inner, anchor="nw")

        self.findings_inner.bind(
            "<Configure>",
            lambda e: self.findings_canvas.configure(scrollregion=self.findings_canvas.bbox("all"))
        )
        self.findings_canvas.bind(
            "<Configure>",
            lambda e: self.findings_canvas.itemconfig(self._findings_inner_id, width=e.width)
        )

        def _on_mousewheel(event):
            delta = -1 * (event.delta // 120) if event.delta else (1 if event.num == 5 else -1)
            self.findings_canvas.yview_scroll(delta, "units")

        self.findings_canvas.bind("<Enter>", lambda e: self._bind_findings_scroll(_on_mousewheel))
        self.findings_canvas.bind("<Leave>", lambda e: self._unbind_findings_scroll())

    def _bind_findings_scroll(self, handler):
        self.findings_canvas.bind_all("<MouseWheel>", handler)
        self.findings_canvas.bind_all("<Button-4>", handler)
        self.findings_canvas.bind_all("<Button-5>", handler)

    def _unbind_findings_scroll(self):
        self.findings_canvas.unbind_all("<MouseWheel>")
        self.findings_canvas.unbind_all("<Button-4>")
        self.findings_canvas.unbind_all("<Button-5>")

    def _build_summary_tab(self):
        container = ttk.Frame(self.tab_summary)
        container.pack(fill="both", expand=True)

        left = ttk.Frame(container, padding=10)
        left.pack(side="left", fill="y")

        self.stat_frame = ttk.Frame(left)
        self.stat_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(left, text="Thread State Breakdown", font=("", 11, "bold")).pack(anchor="w", pady=(6, 4))
        self.state_table = ttk.Treeview(left, columns=("state", "count", "pct"), show="headings", height=7)
        self.state_table.heading("state", text="State")
        self.state_table.heading("count", text="Count")
        self.state_table.heading("pct", text="%")
        self.state_table.column("state", width=140)
        self.state_table.column("count", width=70, anchor="center")
        self.state_table.column("pct", width=70, anchor="center")
        self.state_table.pack(fill="x")

        ttk.Label(left, text="Top Thread Pools / Groups", font=("", 11, "bold")).pack(anchor="w", pady=(16, 4))
        self.pool_table = ttk.Treeview(left, columns=("pool", "count"), show="headings", height=8)
        self.pool_table.heading("pool", text="Pool / Thread Group")
        self.pool_table.heading("count", text="Threads")
        self.pool_table.column("pool", width=220)
        self.pool_table.column("count", width=70, anchor="center")
        self.pool_table.pack(fill="x")

        right = ttk.Frame(container, padding=10)
        right.pack(side="left", fill="both", expand=True)

        self.fig = Figure(figsize=(7.5, 7), dpi=96)
        self.ax_pie = self.fig.add_subplot(211)
        self.ax_bar = self.fig.add_subplot(212)
        self.fig.tight_layout(pad=3)

        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_threads_tab(self):
        toolbar = ttk.Frame(self.tab_threads, padding=(10, 8))
        toolbar.pack(side="top", fill="x")

        ttk.Label(toolbar, text="Filter by state:").pack(side="left")
        self.state_filter_var = tk.StringVar(value="ALL")
        self.state_filter_combo = ttk.Combobox(
            toolbar, textvariable=self.state_filter_var, state="readonly", width=16,
            values=["ALL", "RUNNABLE", "WAITING", "TIMED_WAITING", "BLOCKED", "NEW", "TERMINATED", "UNKNOWN"]
        )
        self.state_filter_combo.pack(side="left", padx=(6, 16))
        self.state_filter_combo.bind("<<ComboboxSelected>>", lambda e: self._on_manual_filter_change())

        ttk.Label(toolbar, text="Search name/stack:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=6)
        search_entry.bind("<KeyRelease>", lambda e: self._on_manual_filter_change())

        self.clear_filter_btn = ttk.Button(toolbar, text="✕ Show All Threads", command=self._clear_thread_highlight)

        self.thread_count_label = ttk.Label(toolbar, text="")
        self.thread_count_label.pack(side="right")

        paned = ttk.PanedWindow(self.tab_threads, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        table_frame = ttk.Frame(paned)
        detail_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=3)
        paned.add(detail_frame, weight=2)

        columns = ("name", "state", "daemon", "prio", "top_frame")
        self.thread_table = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.thread_table.heading("name", text="Thread Name")
        self.thread_table.heading("state", text="State")
        self.thread_table.heading("daemon", text="Daemon")
        self.thread_table.heading("prio", text="Prio")
        self.thread_table.heading("top_frame", text="Top Stack Frame")
        self.thread_table.column("name", width=240)
        self.thread_table.column("state", width=110)
        self.thread_table.column("daemon", width=70, anchor="center")
        self.thread_table.column("prio", width=50, anchor="center")
        self.thread_table.column("top_frame", width=480)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.thread_table.yview)
        self.thread_table.configure(yscrollcommand=vsb.set)
        self.thread_table.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.thread_table.bind("<<TreeviewSelect>>", self._on_thread_selected)
        self.thread_table.tag_configure("BLOCKED", background="#fde2e2")
        self.thread_table.tag_configure("WAITING", background="#fff6e0")
        self.thread_table.tag_configure("TIMED_WAITING", background="#fff1d6")
        self.thread_table.tag_configure("PROBLEM", background="#ffd6d6", foreground="#7a0000")

        ttk.Label(detail_frame, text="Full Stack Trace", font=("", 10, "bold")).pack(anchor="w", padx=4, pady=(4, 2))
        self.detail_text = tk.Text(detail_frame, wrap="none", font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4")
        detail_vsb = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail_text.yview)
        detail_hsb = ttk.Scrollbar(detail_frame, orient="horizontal", command=self.detail_text.xview)
        self.detail_text.configure(yscrollcommand=detail_vsb.set, xscrollcommand=detail_hsb.set)
        self.detail_text.pack(side="left", fill="both", expand=True, padx=(4, 0))
        detail_vsb.pack(side="right", fill="y")
        detail_hsb.pack(side="bottom", fill="x")

    def _build_issues_tab(self):
        outer = ttk.Frame(self.tab_issues, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="⚠ Deadlocks", font=("", 12, "bold")).pack(anchor="w")
        self.deadlock_text = tk.Text(outer, height=10, wrap="word", font=("Consolas", 10),
                                      bg="#2b1414", fg="#ffb3b3")
        self.deadlock_text.pack(fill="x", pady=(4, 14))
        self.deadlock_text.configure(state="disabled")

        ttk.Label(outer, text="🔥 Top Contention Points (most common BLOCKED/WAITING frames)",
                  font=("", 12, "bold")).pack(anchor="w")
        self.contention_table = ttk.Treeview(outer, columns=("frame", "count"), show="headings", height=8)
        self.contention_table.heading("frame", text="Stack Frame")
        self.contention_table.heading("count", text="Threads stuck here")
        self.contention_table.column("frame", width=820)
        self.contention_table.column("count", width=140, anchor="center")
        self.contention_table.pack(fill="x", pady=(4, 14))

        ttk.Label(outer, text="👥 Thread Groups Stuck on Identical Stacks (possible pool exhaustion / leak)",
                  font=("", 12, "bold")).pack(anchor="w")
        group_frame = ttk.Frame(outer)
        group_frame.pack(fill="both", expand=True)
        self.group_table = ttk.Treeview(group_frame, columns=("count", "threads", "stack"), show="headings")
        self.group_table.heading("count", text="#")
        self.group_table.heading("threads", text="Thread Names")
        self.group_table.heading("stack", text="Shared Stack Signature (top frames)")
        self.group_table.column("count", width=50, anchor="center")
        self.group_table.column("threads", width=260)
        self.group_table.column("stack", width=600)
        gvsb = ttk.Scrollbar(group_frame, orient="vertical", command=self.group_table.yview)
        self.group_table.configure(yscrollcommand=gvsb.set)
        self.group_table.pack(side="left", fill="both", expand=True)
        gvsb.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # File loading & Appending logic
    # ------------------------------------------------------------------

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open Java Thread Dump",
            filetypes=[("Text files", "*.txt"), ("Log files", "*.log"), ("All files", "*.*")],
        )
        if not path:
            return
        self._load_file(path)

    def add_file(self):
        """Menambahkan file thread dump baru ke dalam urutan snapshots aplikasi."""
        path = filedialog.askopenfilename(
            title="Add Another Java Thread Dump",
            filetypes=[("Text files", "*.txt"), ("Log files", "*.log"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            messagebox.showerror("Error reading file", str(e))
            return

        self.status_var.set("Parsing...")
        self.update_idletasks()

        new_snapshots = parse_thread_dump_file(text)

        if not new_snapshots:
            messagebox.showwarning(
                "No thread dump found",
                "This file doesn't look like a Java thread dump."
            )
            self.status_var.set("Ready.")
            return

        start_idx = len(self.snapshots)
        for i, s in enumerate(new_snapshots):
            s.index = start_idx + i
            self.snapshots.append(s)
            self.snapshot_sources.append(path)
            
        if path not in self.loaded_filenames:
            self.loaded_filenames.append(path)

        self.file_label.config(text=f"{os.path.basename(path)} (+{start_idx} snapshots)", foreground="#000")

        values = list(self.snapshot_combo["values"])
        for s in new_snapshots:
            label = f"Snapshot {s.index + 1}"
            if s.timestamp_line:
                label += f"  ({s.timestamp_line})"
            values.append(label)
            
        self.snapshot_combo.configure(values=values)
        self.snapshot_combo.current(start_idx)
        self.current_snapshot_idx = start_idx

        self.snapshot_label.config(text=f"{len(self.snapshots)} snapshot(s) total")
        self._render_snapshot(self.snapshots[start_idx])
        self.status_var.set(f"Added {os.path.basename(path)} — total {len(self.snapshots)} snapshot(s)")

    def _load_file(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            messagebox.showerror("Error reading file", str(e))
            return

        self.status_var.set("Parsing...")
        self.update_idletasks()

        snapshots = parse_thread_dump_file(text)

        if not snapshots:
            messagebox.showwarning("No thread dump found", "This file doesn't look like a Java thread dump.")
            self.status_var.set("Ready.")
            return

        self.snapshots = snapshots
        self.current_snapshot_idx = 0
        self.loaded_filename = path
        self.loaded_filenames = [path]
        self.snapshot_sources = [path] * len(snapshots)

        self.file_label.config(text=os.path.basename(path), foreground="#000")

        values = []
        for s in snapshots:
            label = f"Snapshot {s.index + 1}"
            if s.timestamp_line:
                label += f"  ({s.timestamp_line})"
            values.append(label)
        self.snapshot_combo.configure(values=values)
        self.snapshot_combo.current(0)
        self.snapshot_label.config(text=f"{len(snapshots)} snapshot(s) found")

        self._render_snapshot(snapshots[0])
        self.status_var.set(f"Loaded {os.path.basename(path)} — {len(snapshots)} snapshot(s)")

    def _on_snapshot_change(self, event=None):
        idx = self.snapshot_combo.current()
        if 0 <= idx < len(self.snapshots):
            self.current_snapshot_idx = idx
            self._render_snapshot(self.snapshots[idx])

    def _show_empty_state(self):
        self.ax_pie.clear()
        self.ax_bar.clear()
        self.ax_pie.text(0.5, 0.5, "Open a thread dump to see charts", ha="center", va="center", fontsize=11, color="#888")
        self.ax_pie.axis("off")
        self.ax_bar.axis("off")
        self.canvas.draw()

    # ------------------------------------------------------------------
    # Rendering interface logic
    # ------------------------------------------------------------------

    def _render_snapshot(self, snap: Snapshot):
        self._render_diagnosis(snap)
        self._render_stat_cards(snap)
        self._render_state_table(snap)
        self._render_pool_table(snap)
        self._render_charts(snap)
        self._refresh_thread_table()
        self._render_issues_tab(snap)

    def _render_diagnosis(self, snap: Snapshot):
        diagnosis: Diagnosis = diagnose(snap)

        if diagnosis.is_healthy:
            banner_bg = "#e8f5e9"
            banner_fg = "#1b5e20"
            banner_text = "✅ " + diagnosis.verdict
        else:
            top = diagnosis.top_finding
            banner_bg = {"CRITICAL": "#fdecea", "HIGH": "#fff3e0"}.get(top.severity, "#fff8e1")
            banner_fg = {"CRITICAL": "#b71c1c", "HIGH": "#e65100"}.get(top.severity, "#8d6e00")
            banner_text = f"{SEVERITY_ICON.get(top.severity, '⚠')} {diagnosis.verdict}"
        self.verdict_banner.configure(bg=banner_bg)
        self.verdict_label.configure(bg=banner_bg, fg=banner_fg, text=banner_text)

        if diagnosis.is_healthy:
            card_bg = "#2e7d32"
            icon = "✅"
            subtitle = f"{len(snap.threads)} threads analyzed — no issues found."
        else:
            top = diagnosis.top_finding
            card_bg = SEVERITY_COLORS.get(top.severity, "#e65100")
            icon = SEVERITY_ICON.get(top.severity, "⚠")
            n_findings = len(diagnosis.findings)
            subtitle = f"{n_findings} issue(s) found in this snapshot."
        self.diag_card.configure(bg=card_bg)
        self.diag_icon_label.configure(bg=card_bg, text=icon)
        self.diag_verdict_label.configure(bg=card_bg, text=diagnosis.verdict)
        self.diag_subtitle_label.configure(bg=card_bg, text=subtitle)
        for child in self.diag_card.winfo_children():
            child.configure(bg=card_bg)

        for w in self.findings_inner.winfo_children():
            w.destroy()

        if not diagnosis.findings:
            tk.Label(self.findings_inner, bg="#fafafa", fg="#555", text="No issues to report.", font=("", 10)).pack(fill="x", padx=10, pady=10)
        else:
            for i, finding in enumerate(diagnosis.findings, 1):
                self._render_finding_card(finding, i)

    def _render_finding_card(self, finding: Finding, rank: int):
        color = SEVERITY_COLORS.get(finding.severity, "#616161")
        icon = SEVERITY_ICON.get(finding.severity, "⚠")

        card = tk.Frame(self.findings_inner, bg="white", highlightbackground=color, highlightthickness=2, padx=12, pady=10)
        card.pack(fill="x", padx=4, pady=6)

        header = tk.Frame(card, bg="white")
        header.pack(fill="x")
        tk.Label(header, text=f"{icon} #{rank}  {finding.title}", bg="white", fg="#111", font=("", 11, "bold")).pack(side="left")
        tk.Label(header, text=finding.severity, bg=color, fg="white", font=("", 8, "bold"), padx=8, pady=2).pack(side="right")

        tk.Label(card, text=finding.detail, bg="white", fg="#333", font=("", 9), anchor="w", justify="left", wraplength=950).pack(fill="x", pady=(6, 0))

        if finding.affected_threads:
            shown = finding.affected_threads[:10]
            more = len(finding.affected_threads) - len(shown)
            thread_str = ", ".join(shown) + (f"  (+{more} more)" if more > 0 else "")
            tk.Label(card, text=f"Affected threads: {thread_str}", bg="white", fg="#777", font=("", 8, "italic"), anchor="w", justify="left", wraplength=950).pack(fill="x", pady=(6, 0))

            action_bar = tk.Frame(card, bg="white")
            action_bar.pack(fill="x", pady=(8, 0))
            btn = tk.Button(
                action_bar, text="🔍 Lihat Titik Masalah & Stack Trace",
                bg=color, fg="white", activebackground=color, activeforeground="white",
                relief="flat", padx=10, pady=4, font=("", 9, "bold"), cursor="hand2",
                command=lambda names=tuple(finding.affected_threads): self._jump_to_threads(names),
            )
            btn.pack(side="left")

    def _jump_to_threads(self, thread_names):
        if not thread_names:
            return
        self.notebook.select(self.tab_threads)
        self.search_var.set("")  
        self._refresh_thread_table(highlight_only=set(thread_names))

        primary_name = thread_names[0]
        target_iid = None
        for iid, t in self._thread_lookup.items():
            if t.name == primary_name:
                target_iid = iid
                break

        if target_iid:
            self.thread_table.selection_set(target_iid)
            self.thread_table.see(target_iid)
            self.thread_table.focus(target_iid)
            self._on_thread_selected()

    def _render_stat_cards(self, snap: Snapshot):
        for w in self.stat_frame.winfo_children():
            w.destroy()

        cards = [
            ("Total Threads", len(snap.threads), "#1565c0"),
            ("Blocked", snap.state_counts.get("BLOCKED", 0), "#c62828"),
            ("Waiting", snap.state_counts.get("WAITING", 0) + snap.state_counts.get("TIMED_WAITING", 0), "#f9a825"),
            ("Daemon", snap.daemon_count, "#616161"),
            ("Deadlocks", len(snap.deadlocks), "#ad1457" if snap.deadlocks else "#2e7d32"),
        ]
        for i, (label, value, color) in enumerate(cards):
            card = tk.Frame(self.stat_frame, bg=color, padx=14, pady=10)
            card.grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="ew")
            tk.Label(card, text=str(value), bg=color, fg="white", font=("", 18, "bold")).pack(anchor="w")
            tk.Label(card, text=label, bg=color, fg="white", font=("", 9)).pack(anchor="w")
        self.stat_frame.grid_columnconfigure(0, weight=1)
        self.stat_frame.grid_columnconfigure(1, weight=1)

    def _render_state_table(self, snap: Snapshot):
        self.state_table.delete(*self.state_table.get_children())
        total = max(len(snap.threads), 1)
        for state, count in snap.state_counts.most_common():
            pct = f"{(count / total) * 100:.1f}%"
            self.state_table.insert("", "end", values=(state, count, pct))

    def _render_pool_table(self, snap: Snapshot):
        self.pool_table.delete(*self.pool_table.get_children())
        buckets = thread_pool_buckets(snap)
        for pool, count in buckets.most_common(15):
            self.pool_table.insert("", "end", values=(pool, count))

    def _render_charts(self, snap: Snapshot):
        self.ax_pie.clear()
        self.ax_bar.clear()

        counts = snap.state_counts
        if counts:
            labels = list(counts.keys())
            sizes = list(counts.values())
            colors = [STATE_COLORS.get(s, "#9e9e9e") for s in labels]
            self.ax_pie.pie(sizes, labels=labels, autopct="%1.0f%%", colors=colors, textprops={"fontsize": 8}, startangle=90)
            self.ax_pie.set_title("Thread State Distribution", fontsize=10)
        else:
            self.ax_pie.axis("off")

        buckets = thread_pool_buckets(snap).most_common(10)
        if buckets:
            names = [b[0][:22] for b in buckets][::-1]
            values = [b[1] for b in buckets][::-1]
            bars = self.ax_bar.barh(names, values, color="#1565c0")
            self.ax_bar.set_title("Top Thread Pools by Count", fontsize=10)
            self.ax_bar.tick_params(axis="y", labelsize=7)
        else:
            self.ax_bar.axis("off")

        self.fig.tight_layout(pad=3)
        self.canvas.draw()

    def _refresh_thread_table(self, highlight_only=None):
        if not self.snapshots:
            return
        snap = self.snapshots[self.current_snapshot_idx]
        self.thread_table.delete(*self.thread_table.get_children())

        if highlight_only is not None:
            self._highlighted_thread_names = set(highlight_only)

        in_highlight_mode = bool(self._highlighted_thread_names)
        state_filter = self.state_filter_var.get()
        search = self.search_var.get().strip().lower()

        shown = 0
        self._thread_lookup = {}
        for idx, t in enumerate(snap.threads):
            if in_highlight_mode:
                if t.name not in self._highlighted_thread_names:
                    continue
            else:
                if state_filter != "ALL" and t.state != state_filter:
                    continue
                if search:
                    haystack = (t.name + " " + " ".join(t.stack)).lower()
                    if search not in haystack:
                        continue
            iid = f"thread-{idx}"
            tags = (t.state,)
            if in_highlight_mode:
                tags = tags + ("PROBLEM",)
            self.thread_table.insert(
                "", "end", iid=iid,
                values=(t.name, t.state, "Yes" if t.daemon else "No", t.priority or "-", t.top_frame),
                tags=tags,
            )
            self._thread_lookup[iid] = t
            shown += 1

        total = len(snap.threads)
        if in_highlight_mode:
            self.thread_count_label.config(text=f"Showing {shown} flagged thread(s) of {total} total")
            self.clear_filter_btn.pack(side="right", padx=(0, 10))
        else:
            self.thread_count_label.config(text=f"Showing {shown} of {total} threads")
            self.clear_filter_btn.pack_forget()

    def _on_manual_filter_change(self):
        self._highlighted_thread_names = None
        self._refresh_thread_table()

    def _clear_thread_highlight(self):
        self._highlighted_thread_names = None
        self.search_var.set("")
        self.state_filter_var.set("ALL")
        self._refresh_thread_table()

    def _on_thread_selected(self, event=None):
        sel = self.thread_table.selection()
        if not sel:
            return
        t = self._thread_lookup.get(sel[0])
        if t:
            self._render_thread_detail(t)

    def _render_thread_detail(self, t):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")

        self.detail_text.tag_configure("hdr", foreground="#4fc3f7", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("state_blocked", foreground="#ff6b6b", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("state_waiting", foreground="#ffd166", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("state_runnable", foreground="#8fd694", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("state_other", foreground="#cfd8dc", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("waiting_on", foreground="#ff6b6b", font=("Consolas", 10, "bold"))
        self.detail_text.tag_configure("problem_frame", background="#5a2a2a", foreground="#ffcccc", font=("Consolas", 10, "bold"))

        state_tag = {"BLOCKED": "state_blocked", "WAITING": "state_waiting", "TIMED_WAITING": "state_waiting", "RUNNABLE": "state_runnable"}.get(t.state, "state_other")

        self.detail_text.insert("end", f'"{t.name}"\n', "hdr")
        self.detail_text.insert("end", f"  state: {t.state}\n", state_tag)
        
        if t.waiting_on:
            self.detail_text.insert("end", f"⚠ STUCK HERE — waiting on: {t.waiting_on}\n\n", "waiting_on")

        for i, frame in enumerate(t.stack):
            tag = "problem_frame" if i == 0 and (t.is_blocked or t.is_waiting) else "frame"
            self.detail_text.insert("end", f"    at {frame}\n", tag)

        self.detail_text.configure(state="disabled")

    def _render_issues_tab(self, snap: Snapshot):
        self.deadlock_text.configure(state="normal")
        self.deadlock_text.delete("1.0", "end")
        if snap.deadlocks:
            for i, dl in enumerate(snap.deadlocks, 1):
                self.deadlock_text.insert("end", f"Deadlock #{i} — threads involved: {', '.join(dl.threads_involved)}\n")
                self.deadlock_text.insert("end", dl.raw_text + "\n\n")
        else:
            self.deadlock_text.insert("end", "No deadlocks detected in this snapshot. ✅")
        self.deadlock_text.configure(state="disabled")

        self.contention_table.delete(*self.contention_table.get_children())
        for frame, count in top_blocking_frames(snap, limit=15):
            self.contention_table.insert("", "end", values=(frame, count))

        self.group_table.delete(*self.group_table.get_children())
        groups = group_by_stack_signature(snap, depth=4)
        for sig, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            stack_str = "  →  ".join(sig) if sig else "(empty stack)"
            self.group_table.insert("", "end", values=(len(names), ", ".join(names[:6]), stack_str))

    def _build_ai_tab(self):
        """Membangun layout untuk Tab AI Analysis"""
        outer = ttk.Frame(self.tab_ai, padding=14)
        outer.pack(fill="both", expand=True)

        # Header / Kontrol
        ctrl_frame = ttk.Frame(outer)
        ctrl_frame.pack(fill="x", pady=(0, 10))

        self.btn_analyze = ttk.Button(
            ctrl_frame, 
            text="🤖 Jalankan Analisis Gemini AI", 
            command=self._start_ai_analysis
        )
        self.btn_analyze.pack(side="left")

        # Area Output teks hasil AI
        ttk.Label(outer, text="AI Deep-Dive Report:", font=("", 10, "bold")).pack(anchor="w", pady=(10, 2))
        
        self.ai_text_area = tk.Text(
            outer, wrap="word", font=("Consolas", 10), 
            bg="#252526", fg="#d4d4d4", insertbackground="white"
        )
        self.ai_text_area.pack(fill="both", expand=True)
        self.ai_text_area.configure(state="disabled")

    def _start_ai_analysis(self):
        """Dipanggil saat tombol diklik. Memulai thread agar UI tetap responsif."""
        if not self.snapshots or self.current_snapshot_idx is None:
            messagebox.showwarning("Perhatian", "Silakan buka file thread dump terlebih dahulu.")
            return

        # Ambil snapshot aktif saat ini
        snap = self.snapshots[self.current_snapshot_idx]
        diag = diagnose(snap)  # Menghasilkan Diagnosis objek riil

        # Siapkan UI untuk mode loading
        self.btn_analyze.configure(state="disabled")
        self.status_var.set("Menghubungi Gemini AI untuk deep-dive analysis...")
        
        self.ai_text_area.configure(state="normal")
        self.ai_text_area.delete("1.0", "end")
        self.ai_text_area.insert("end", "[Memproses data dump dan menganalisis... Mohon tunggu]\n")
        self.ai_text_area.configure(state="disabled")

        # Jalankan pekerja di background thread
        worker = threading.Thread(target=self._ai_analysis_worker, args=(snap, diag), daemon=True)
        worker.start()

    def _ai_analysis_worker(self, snap, diag):
        """Worker thread yang bertugas melakukan operasi I/O intensif ke API"""
        try:
            # 1. Panggil fungsi analyzer (misalnya get_ai_analysis dari ai_analyzer.py)
            # Di sini Anda bisa mengarahkannya ke fungsi wrapper Gemini Anda
            analysis_result = get_ai_analysis(snap, diag, language="id")

            # 2. Jika sukses, perbarui UI lewat Main Thread secara aman
            def _success_ui():
                self.ai_text_area.configure(state="normal")
                self.ai_text_area.delete("1.0", "end")
                self.ai_text_area.insert("end", analysis_result)
                self.ai_text_area.configure(state="disabled")
                
                self.btn_analyze.configure(state="normal")
                self.status_var.set("Analisis AI selesai.")

            self.after(0, _success_ui)

        except Exception as e:
            # Pola penanganan error thread-safe Anda diadopsi di sini:
            error_msg = str(e)  # Amankan pesan error ke variabel string terlebih dahulu!
            
            def _error_ui():
                messagebox.showerror("Gemini AI Error", error_msg)
                
                # Kembalikan state UI ke semula agar user bisa mencoba lagi
                self.ai_text_area.configure(state="normal")
                self.ai_text_area.insert("end", f"\n\n[PROSES GAGAL]: {error_msg}")
                self.ai_text_area.configure(state="disabled")
                
                self.btn_analyze.configure(state="normal")
                self.status_var.set("Gagal melakukan analisis AI.")
                
            self.after(0, _error_ui)


def main():
    app = ThreadDumpAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()