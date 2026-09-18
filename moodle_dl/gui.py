"""Desktop GUI - a flat, minimal front-end over the same sync engine the CLI
uses.

All Moodle work (login, course discovery, syncing) runs in worker threads so
the window never freezes; results come back through a queue polled with Tk's
after().
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from . import __version__, history, theme as t
from .config import Config, load_config
from .courses import Course, fetch_courses, preselect
from .folders import count_files
from .main import sync
from .session import MoodleSession
from .setup_wizard import DEFAULT_URL, write_config

ctk.set_appearance_mode("system")


def _font(size: int = 13, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family=t.FONT, size=size,
                       weight="bold" if bold else "normal")


def _mono(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=t.MONO, size=size)


def _card(parent, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(parent, fg_color=t.CARD, border_color=t.BORDER,
                        border_width=1, corner_radius=t.RADIUS_CARD, **kw)


def _rule(parent) -> ctk.CTkFrame:
    line = ctk.CTkFrame(parent, height=1, fg_color=t.BORDER, corner_radius=0)
    line.pack(fill="x")
    return line


def _primary(parent, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, command=command, height=38,
                         corner_radius=t.RADIUS_CTL, font=_font(14),
                         fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
                         text_color=t.ON_ACCENT, **kw)


def _ghost(parent, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, command=command, height=34,
                         corner_radius=t.RADIUS_CTL, font=_font(13),
                         fg_color="transparent", hover_color=t.GHOST_HOVER,
                         text_color=t.TEXT, border_width=1,
                         border_color=t.BORDER_STRONG, **kw)


def _label(parent, text, size=13, color=None, bold=False, **kw):
    return ctk.CTkLabel(parent, text=text, font=_font(size, bold),
                        text_color=color or t.TEXT, **kw)


def problem_card(parent, problem, on_retry=None, on_copy=None) -> ctk.CTkFrame:
    """A failure, said in full: what happened, what to do, and the raw text.

    Built as a panel rather than a status line because the status line was
    genuinely missed - small, grey, top-left, above a screen whose middle was
    empty and calm. This sits where the missing content should have been.
    """
    card = ctk.CTkFrame(parent, fg_color=t.DANGER_WASH,
                        corner_radius=t.RADIUS_CARD,
                        border_width=1, border_color=t.DANGER_BORDER)

    head = ctk.CTkFrame(card, fg_color="transparent")
    head.pack(fill="x", padx=16, pady=(14, 2))
    _label(head, "!", size=15, bold=True, color=t.DANGER).pack(side="left",
                                                               padx=(0, 9))
    _label(head, problem.title, size=14, bold=True, color=t.DANGER_TEXT,
           anchor="w", justify="left", wraplength=560).pack(
               side="left", fill="x", expand=True)

    for i, step in enumerate(problem.steps, 1):
        line = ctk.CTkFrame(card, fg_color="transparent")
        line.pack(fill="x", padx=16, pady=1)
        _label(line, f"{i}.", size=13, color=t.TEXT_SECONDARY,
               width=18, anchor="w").pack(side="left")
        # justify matters once a step wraps: without it the second line
        # centres itself under the first, which looks like a bug because it is.
        _label(line, step, size=13, color=t.TEXT_SECONDARY, anchor="w",
               justify="left", wraplength=540).pack(side="left", fill="x",
                                                    expand=True)

    row = ctk.CTkFrame(card, fg_color="transparent")
    row.pack(fill="x", padx=16, pady=(10, 14))
    if on_retry is not None and problem.retry:
        ctk.CTkButton(row, text="Try again", command=on_retry, width=104,
                      height=32, corner_radius=t.RADIUS_CTL, font=_font(13, True),
                      fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
                      text_color=t.ON_ACCENT).pack(side="left")

    box = ctk.CTkTextbox(card, height=92, font=_mono(11), fg_color=t.CARD,
                         text_color=t.TEXT_SECONDARY, border_width=1,
                         border_color=t.DANGER_BORDER,
                         corner_radius=t.RADIUS_CTL, wrap="word")
    box.insert("1.0", problem.details or "(no further detail)")
    box.configure(state="disabled")

    shown = {"on": False}

    def toggle() -> None:
        shown["on"] = not shown["on"]
        if shown["on"]:
            box.pack(fill="x", padx=16, pady=(0, 14))
            more.configure(text="Hide technical details")
        else:
            box.pack_forget()
            more.configure(text="Technical details")

    more = ctk.CTkButton(row, text="Technical details", command=toggle,
                         width=132, height=32, corner_radius=t.RADIUS_CTL,
                         font=_font(12), fg_color="transparent",
                         hover_color=t.GHOST_HOVER, text_color=t.TEXT_SECONDARY,
                         border_width=1, border_color=t.DANGER_BORDER)
    more.pack(side="left", padx=8)

    def copy() -> None:
        parent.clipboard_clear()
        parent.clipboard_append(problem.details)
        copy_btn.configure(text="Copied")
        parent.after(1800, lambda: copy_btn.winfo_exists()
                     and copy_btn.configure(text="Copy details"))
        if on_copy is not None:
            on_copy()

    copy_btn = ctk.CTkButton(row, text="Copy details", command=copy, width=110,
                             height=32, corner_radius=t.RADIUS_CTL,
                             font=_font(12), fg_color="transparent",
                             hover_color=t.GHOST_HOVER,
                             text_color=t.TEXT_SECONDARY, border_width=0)
    copy_btn.pack(side="left")
    return card


class _QueueWriter(io.TextIOBase):
    """Redirects the sync engine's prints into the GUI log."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text: str) -> int:
        if text.strip():
            self.q.put(("log", text.rstrip()))
        return len(text)


class App(ctk.CTk):
    def __init__(self, config_path: Path):
        super().__init__()
        self.config_path = config_path
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.courses: list[Course] = []
        self.course_vars: list[ctk.BooleanVar] = []
        self.cancel_event: threading.Event | None = None
        self.log_box = None
        self.status_label = None

        self.title("Moodle Downloader")
        self.geometry("680x620")
        self.minsize(600, 540)
        self.configure(fg_color=t.PAGE)

        if config_path.exists():
            self._build_dashboard()
        else:
            self._build_setup()
        self.after(120, self._poll)

    # ------------------------------------------------------------------ utils

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self.log_box = None
        self.status_label = None

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "courses":
                    self._show_courses(payload)
                elif kind == "error":
                    self._show_problem(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _clear_problem(self) -> None:
        """Take down a previous failure before trying the same thing again."""
        host = getattr(self, "problem_host", None)
        if host is not None and host.winfo_exists():
            for w in host.winfo_children():
                w.destroy()

    def _show_problem(self, exc) -> None:
        """Put a failure where the user is already looking, in their words.

        The old path wrote the message to the log box and told the user to
        read it. On the setup screen there is no log box, so `_append_log`
        returned without doing anything and the only trace of the failure was
        a grey line in the corner - the message itself was thrown away, and
        the Load button sat reading "Loading..." for ever.
        """
        from .problems import diagnose

        self.busy = False
        self._set_buttons(enabled=True)
        problem = diagnose(exc)
        self._append_log(f"! {problem.details}")   # still logged where there is one

        retry = getattr(self, "_retry_action", None)
        for attr in ("course_frame", "problem_host"):
            host = getattr(self, attr, None)
            if host is None or not host.winfo_exists():
                continue
            for w in host.winfo_children():
                w.destroy()
            problem_card(host, problem, on_retry=retry).pack(
                fill="x", padx=6, pady=6)
            break

        # A button still reading "Loading..." after the load failed is its own
        # small lie, and it was on screen in the report that prompted this.
        for name, label in (("fetch_btn", "Load my courses from Moodle"),
                            ("sync_btn", "Sync now")):
            btn = getattr(self, name, None)
            if btn is not None and btn.winfo_exists():
                btn.configure(text=label)
        self._set_status(problem.title, t.DANGER)

    def _append_log(self, line: str) -> None:
        if self.log_box is None or not self.log_box.winfo_exists():
            return
        stamp = time.strftime("%H:%M")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp}  {line}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text: str, color=None) -> None:
        if self.status_label is not None and self.status_label.winfo_exists():
            self.status_label.configure(text=text,
                                        text_color=color or t.TEXT_SECONDARY)

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for name in ("sync_btn", "setup_btn", "finish_btn", "fetch_btn"):
            btn = getattr(self, name, None)
            if btn is not None and btn.winfo_exists():
                btn.configure(state=state)

    # -------------------------------------------------------------- dashboard

    def _build_dashboard(self) -> None:
        self._clear()
        cfg = load_config(self.config_path)

        page = ctk.CTkFrame(self, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=22, pady=20)

        card = _card(page)
        card.pack(fill="both", expand=True)

        head = ctk.CTkFrame(card, fg_color="transparent", height=48)
        head.pack(fill="x", padx=18, pady=(14, 12))
        _label(head, "Moodle Downloader", size=16, bold=True).pack(side="left")
        self.dot = _label(head, "●", size=11, color=t.SUCCESS)
        self.dot.pack(side="left", padx=(12, 5))
        self.status_label = _label(head, self._last_sync_text(cfg), size=12,
                                   color=t.TEXT_SECONDARY)
        self.status_label.pack(side="left")
        _rule(card)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=16)
        self.sync_btn = _primary(actions, "Sync now", self._start_sync)
        self.sync_btn.pack(side="left", expand=True, fill="x")
        self.cancel_btn = _ghost(actions, "Stop", self._cancel_sync, width=76)
        self.cancel_btn.configure(height=38, text_color=t.DANGER,
                                  border_color=t.DANGER)
        self.auto_var = ctk.BooleanVar(value=self._autosync_on())
        sw = ctk.CTkSwitch(actions, text="Auto-sync", variable=self.auto_var,
                           command=self._toggle_autosync, font=_font(13),
                           text_color=t.TEXT_SECONDARY, width=48,
                           switch_width=40, switch_height=20,
                           fg_color=t.BORDER_STRONG, progress_color=t.SUCCESS,
                           button_color=("#FFFFFF", "#FFFFFF"),
                           button_hover_color=("#FFFFFF", "#FFFFFF"))
        sw.pack(side="left", padx=16)
        _ghost(actions, "Open folder",
               lambda: self._open_folder(cfg)).pack(side="left")

        self.stats = ctk.CTkFrame(card, fg_color="transparent")
        self.stats.pack(fill="x", padx=18, pady=(0, 14))
        self._render_stats(cfg)

        # Empty until something fails, then the whole explanation.
        self.problem_host = ctk.CTkFrame(card, fg_color="transparent")
        self.problem_host.pack(fill="x", padx=12)

        self.log_box = ctk.CTkTextbox(
            card, state="disabled", font=_mono(12), fg_color=t.SUBTLE,
            text_color=t.TEXT_SECONDARY, border_width=0,
            corner_radius=t.RADIUS_CTL, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self._show_recent_runs()

        _rule(card)
        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.pack(fill="x", padx=18, pady=10)
        self.setup_btn = ctk.CTkButton(
            foot, text="Change courses", command=self._build_setup,
            height=26, width=110, corner_radius=t.RADIUS_CTL, font=_font(12),
            fg_color="transparent", hover_color=t.GHOST_HOVER,
            text_color=t.TEXT_SECONDARY, border_width=0)
        self.setup_btn.pack(side="left")
        _label(foot, f"Every {cfg.sync_interval_hours:g} hours", size=12,
               color=t.TEXT_MUTED).pack(side="left", padx=14)
        _label(foot, f"v{__version__}", size=12,
               color=t.TEXT_MUTED).pack(side="right")

    def _render_stats(self, cfg: Config, new_counts: dict | None = None) -> None:
        for w in self.stats.winfo_children():
            w.destroy()
        codes = [u.code for u in cfg.units]
        if not codes and cfg.root_dir.exists():
            codes = sorted(p.name for p in cfg.root_dir.iterdir()
                           if p.is_dir() and not p.name.startswith("."))
        for i, code in enumerate(codes[:6]):
            self.stats.grid_columnconfigure(i, weight=1, uniform="stat")
            tile = ctk.CTkFrame(self.stats, fg_color=t.SUBTLE,
                                corner_radius=t.RADIUS_CTL)
            tile.grid(row=0, column=i, padx=(0 if i == 0 else 8, 0), sticky="ew")
            _label(tile, code, size=12, color=t.TEXT_MUTED,
                   anchor="w").pack(fill="x", padx=12, pady=(10, 0))
            n = count_files(cfg.root_dir / code)
            extra = (new_counts or {}).get(code, 0)
            _label(tile, f"{n} files", size=14, bold=True,
                   anchor="w").pack(fill="x", padx=12, pady=(0, 2))
            _label(tile, f"+{extra} new" if extra else " ", size=11,
                   color=t.SUCCESS if extra else t.TEXT_MUTED,
                   anchor="w").pack(fill="x", padx=12, pady=(0, 10))

    def _show_recent_runs(self) -> None:
        """Background syncs have no console - replay their outcome here."""
        runs = history.load()[-8:]
        if not runs:
            return
        self._append_raw("Recent automatic syncs")
        for r in runs:
            self._append_raw("  " + history.describe(r))
        self._append_raw("")

    def _append_raw(self, line: str) -> None:
        if self.log_box is None or not self.log_box.winfo_exists():
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _last_sync_text(self, cfg: Config) -> str:
        run = history.last()
        if run:
            return "Last sync: " + history.describe(run).split("— ", 1)[-1] \
                   + time.strftime(" (%a %H:%M)", time.localtime(run["at"]))
        try:
            ts = cfg.manifest_path.stat().st_mtime
            return "Last synced " + time.strftime("%a %H:%M", time.localtime(ts))
        except OSError:
            return "Not synced yet"

    def _open_folder(self, cfg: Config) -> None:
        try:
            os.startfile(str(cfg.root_dir))
        except OSError:
            self._append_log("The download folder doesn't exist yet - "
                             "run a sync first.")

    def _autosync_on(self) -> bool:
        if sys.platform != "win32":
            return False
        from .schedule_win import TASK_NAME, _run, autosync_enabled
        if autosync_enabled():
            return True
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0

    def _toggle_autosync(self) -> None:
        from .schedule_win import disable, enable
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if self.auto_var.get():
                hours = 3
                try:
                    hours = max(1, round(load_config(self.config_path)
                                         .sync_interval_hours))
                except Exception:
                    pass
                self.auto_var.set(enable(hours))
            else:
                disable()
        for line in buf.getvalue().splitlines():
            self._append_log(line)

    def _start_sync(self) -> None:
        if self.busy:
            return
        self._retry_action = self._start_sync
        self._clear_problem()
        self.busy = True
        self.cancel_event = threading.Event()
        self._set_buttons(enabled=False)
        self.sync_btn.configure(text="Syncing...")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.dot.configure(text_color=t.TEXT_MUTED)
        self._set_status("Checking Moodle for new files")

        def work():
            try:
                cfg = load_config(self.config_path)
                with contextlib.redirect_stdout(_QueueWriter(self.q)), \
                        contextlib.redirect_stderr(_QueueWriter(self.q)):
                    sync(cfg, cancel=self.cancel_event)
                self.q.put(("done", "sync"))
            except Exception as e:
                self.q.put(("error", e))

        self._run_bg(work)

    def _cancel_sync(self) -> None:
        """Ask the sync to stop. It finishes the item it is on first, so
        nothing is left half-written."""
        if not self.busy or self.cancel_event is None:
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled", text="Stopping")
        self._set_status("Stopping after the current item...")

    def _on_done(self, what: str) -> None:
        self.busy = False
        self.cancel_event = None
        self._set_buttons(enabled=True)
        if self.cancel_btn.winfo_exists():
            self.cancel_btn.pack_forget()
            self.cancel_btn.configure(state="normal", text="Stop")
        if what != "sync":
            return
        if self.sync_btn.winfo_exists():
            self.sync_btn.configure(text="Sync now")
        self.dot.configure(text_color=t.SUCCESS)
        try:
            cfg = load_config(self.config_path)
            self._render_stats(cfg)
            self._set_status(self._last_sync_text(cfg))
        except Exception:
            self._set_status("Sync finished")

    # ------------------------------------------------------------------ setup

    def _build_setup(self) -> None:
        self._clear()
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=22, pady=20)

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.pack(fill="x")
        _label(head, "Set up Moodle Downloader", size=16, bold=True,
               anchor="w").pack(side="left")
        if self.config_path.exists():
            # Only offer a way back once there are settings to go back to.
            ctk.CTkButton(
                head, text="Back", command=self._build_dashboard,
                width=70, height=28, corner_radius=t.RADIUS_CTL,
                font=_font(12), fg_color="transparent",
                hover_color=t.GHOST_HOVER, text_color=t.TEXT_SECONDARY,
                border_width=1, border_color=t.BORDER_STRONG).pack(side="right")
        self.status_label = _label(
            page, "Choose where files go, then load your course list.",
            size=12, color=t.TEXT_SECONDARY, anchor="w", wraplength=600,
            justify="left")
        self.status_label.pack(fill="x", pady=(4, 14))

        form = _card(page)
        form.pack(fill="x")
        inner = ctk.CTkFrame(form, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        _label(inner, "Moodle address", size=12, color=t.TEXT_SECONDARY,
               anchor="w").pack(fill="x")
        self.url_entry = ctk.CTkEntry(
            inner, height=36, corner_radius=t.RADIUS_CTL, font=_font(13),
            fg_color=t.SUBTLE, border_color=t.BORDER, text_color=t.TEXT)
        self.url_entry.insert(0, self._current("base_url", DEFAULT_URL))
        self.url_entry.pack(fill="x", pady=(4, 12))

        _label(inner, "Save course files to", size=12, color=t.TEXT_SECONDARY,
               anchor="w").pack(fill="x")
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        self.dir_entry = ctk.CTkEntry(
            row, height=36, corner_radius=t.RADIUS_CTL, font=_font(13),
            fg_color=t.SUBTLE, border_color=t.BORDER, text_color=t.TEXT)
        self.dir_entry.insert(0, self._current(
            "root_dir", str(self.config_path.parent / "MoodleFiles")))
        self.dir_entry.pack(side="left", expand=True, fill="x")
        _ghost(row, "Browse", self._browse, width=90).pack(side="left",
                                                           padx=(8, 0))

        self.fetch_btn = _ghost(page, "Load my courses from Moodle",
                                self._start_fetch)
        self.fetch_btn.configure(height=38, font=_font(13))
        self.fetch_btn.pack(fill="x", pady=14)

        list_card = _card(page)
        list_card.pack(fill="both", expand=True)
        self.course_frame = ctk.CTkScrollableFrame(
            list_card, fg_color="transparent", corner_radius=t.RADIUS_CARD)
        self.course_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.course_hint = _label(
            self.course_frame,
            "Your courses appear here once loaded.",
            size=12, color=t.TEXT_MUTED, anchor="w")
        self.course_hint.pack(fill="x", padx=10, pady=10)

        self.star_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            page, text="Always sync whatever I star on Moodle",
            variable=self.star_var, font=_font(12),
            text_color=t.TEXT_SECONDARY, checkbox_width=18, checkbox_height=18,
            corner_radius=4, border_width=1, border_color=t.BORDER_STRONG,
            fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
            checkmark_color=t.ON_ACCENT).pack(anchor="w", pady=(12, 6))

        self._build_ai_row(page)

        self.finish_btn = _primary(page, "Finish and run first sync",
                                   self._finish)
        self.finish_btn.configure(state="disabled")
        self.finish_btn.pack(fill="x")

    INTERVALS = {"1 hour": 1, "3 hours": 3, "6 hours": 6, "12 hours": 12,
                 "24 hours": 24}

    @classmethod
    def _interval_label(cls, hours) -> str:
        try:
            h = round(float(hours))
        except (TypeError, ValueError):
            h = 3
        for label, value in cls.INTERVALS.items():
            if value == h:
                return label
        return "3 hours"

    # Label shown in the dropdown -> value stored in the config
    AI_CHOICES = {
        "Off — nothing is sent anywhere": "",
        "Google Gemini (free tier)": "gemini",
        "Claude (Anthropic)": "anthropic",
        "OpenAI": "openai",
        "DeepSeek": "deepseek",
        "Kimi (Moonshot)": "moonshot",
        "GLM (Zhipu)": "zhipu",
        "Qwen (Alibaba)": "qwen",
        "Ollama — runs locally, no key": "ollama",
    }

    def _build_ai_row(self, page) -> None:
        """AI settings belong in the window, not in a text file the user is
        expected to find and edit."""
        box = _card(page)
        box.pack(fill="x", pady=(0, 10))
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)

        freq = ctk.CTkFrame(inner, fg_color="transparent")
        freq.pack(fill="x", pady=(0, 10))
        _label(freq, "Check for new files every", size=12,
               color=t.TEXT_SECONDARY).pack(side="left")
        self.interval_var = ctk.StringVar(
            value=self._interval_label(self._current("sync_interval_hours", "3")))
        ctk.CTkOptionMenu(
            freq, values=list(self.INTERVALS), variable=self.interval_var,
            width=110, height=30, corner_radius=t.RADIUS_CTL, font=_font(12),
            fg_color=t.SUBTLE, button_color=t.SUBTLE,
            button_hover_color=t.GHOST_HOVER, text_color=t.TEXT,
            dropdown_fg_color=t.CARD, dropdown_text_color=t.TEXT,
            dropdown_hover_color=t.GHOST_HOVER,
            dropdown_font=_font(12)).pack(side="left", padx=8)
        _label(freq, "while auto-sync is on", size=12,
               color=t.TEXT_MUTED).pack(side="left")

        _label(inner, "Summarise lecture transcripts with AI (optional)",
               size=12, color=t.TEXT_SECONDARY, anchor="w").pack(fill="x")

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))

        current = self._current("ai_provider", "")
        label_for = {v: k for k, v in self.AI_CHOICES.items()}
        self.ai_var = ctk.StringVar(
            value=label_for.get(current, list(self.AI_CHOICES)[0]))
        ctk.CTkOptionMenu(
            row, values=list(self.AI_CHOICES), variable=self.ai_var,
            command=lambda _: self._ai_hint(), width=210, height=34,
            corner_radius=t.RADIUS_CTL, font=_font(12),
            fg_color=t.SUBTLE, button_color=t.SUBTLE,
            button_hover_color=t.GHOST_HOVER, text_color=t.TEXT,
            dropdown_fg_color=t.CARD, dropdown_text_color=t.TEXT,
            dropdown_hover_color=t.GHOST_HOVER,
            dropdown_font=_font(12)).pack(side="left")

        self.ai_key = ctk.CTkEntry(
            row, height=34, corner_radius=t.RADIUS_CTL, font=_font(12),
            fg_color=t.SUBTLE, border_color=t.BORDER, text_color=t.TEXT,
            placeholder_text="API key", show="•")
        key = self._current("ai_api_key", "")
        if key:
            self.ai_key.insert(0, key)
        self.ai_key.pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.ai_note = _label(inner, "", size=11, color=t.TEXT_MUTED,
                              anchor="w", wraplength=560, justify="left")
        self.ai_note.pack(fill="x", pady=(6, 0))
        self._ai_hint()

    def _ai_hint(self) -> None:
        from .ai import PRESETS
        provider = self.AI_CHOICES.get(self.ai_var.get(), "")
        if not provider:
            text = ("Leave this off and no course content ever leaves your "
                    "computer.")
        else:
            note = PRESETS.get(provider, {}).get("note", "")
            text = f"{note}  Your key is stored only in this app's folder."
        self.ai_note.configure(text=text)

    def _current(self, attr: str, default: str) -> str:
        try:
            return str(getattr(load_config(self.config_path), attr))
        except Exception:
            return default

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="Choose where course files go")
        if chosen:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, chosen.replace("/", os.sep))

    def _start_fetch(self) -> None:
        if self.busy:
            return
        self._retry_action = self._start_fetch
        self.busy = True
        self._set_buttons(enabled=False)
        self.fetch_btn.configure(text="Loading...")
        base_url = self.url_entry.get().strip().rstrip("/") or DEFAULT_URL
        self._set_status("Contacting Moodle. If a browser window opens, log "
                         "in there and tick 'Keep me signed in'.")

        def work():
            try:
                cfg = Config(
                    base_url=base_url,
                    root_dir=self.config_path.parent / "MoodleFiles",
                    week_start=0, week_end=12, units=[], section_patterns=[],
                    unmatched_folder="_Other", skip_extensions=[],
                    config_dir=self.config_path.parent)
                with MoodleSession(cfg) as sess:
                    courses = fetch_courses(sess, base_url, "all")
                courses.sort(key=lambda c: -c.startdate)
                self.q.put(("courses", courses))
            except Exception as e:
                self.q.put(("error", e))

        self._run_bg(work)

    def _show_courses(self, courses: list[Course]) -> None:
        self.busy = False
        self._set_buttons(enabled=True)
        self.fetch_btn.configure(text="Reload course list")
        self.courses = courses
        self.course_vars = []
        for w in self.course_frame.winfo_children():
            w.destroy()

        ticked = preselect(courses, self._configured_codes())
        for i, c in enumerate(courses):
            var = ctk.BooleanVar(value=ticked[i])
            self.course_vars.append(var)
            row = ctk.CTkFrame(self.course_frame, fg_color="transparent")
            row.pack(fill="x")
            star = "★  " if c.starred else "     "
            ctk.CTkCheckBox(
                row, text=star + c.fullname[:74], variable=var,
                font=_font(13), text_color=t.TEXT if c.starred
                else t.TEXT_SECONDARY,
                checkbox_width=18, checkbox_height=18, corner_radius=4,
                border_width=1, border_color=t.BORDER_STRONG,
                fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
                checkmark_color=t.ON_ACCENT).pack(anchor="w", padx=10, pady=7)
            if i < len(courses) - 1:
                _rule(self.course_frame)

        self.finish_btn.configure(state="normal")
        self._set_status(f"{len(courses)} courses found - newest semester "
                         "first, ★ marks the ones you starred on Moodle.")

    def _configured_codes(self) -> set[str]:
        """Unit codes already in the settings file, if there are any."""
        try:
            cfg = load_config(self.config_path)
        except Exception:
            return set()
        return {u.code.upper() for u in cfg.units}

    def _finish(self) -> None:
        if self.busy:
            return
        base_url = self.url_entry.get().strip().rstrip("/") or DEFAULT_URL
        root_dir = self.dir_entry.get().strip()
        if not root_dir:
            self._set_status("Choose a folder first.", t.DANGER)
            return
        if self.star_var.get():
            selection, picked = "starred", []
        else:
            picked = [c for c, v in zip(self.courses, self.course_vars)
                      if v.get()]
            if not picked:
                self._set_status("Tick at least one course, or choose the "
                                 "star option.", t.DANGER)
                return
            selection = "manual"
        keep = {}
        try:
            old = load_config(self.config_path)
            keep = dict(weekly_notes=old.weekly_notes,
                        transcripts=old.transcripts)
        except Exception:
            pass
        write_config(self.config_path, base_url, root_dir, selection, picked,
                     ai_provider=self.AI_CHOICES.get(self.ai_var.get(), ""),
                     ai_api_key=self.ai_key.get(),
                     sync_interval_hours=self.INTERVALS.get(
                         self.interval_var.get(), 3),
                     **keep)
        self._build_dashboard()
        self._append_log("Settings saved - starting your first sync")
        self._start_sync()


def run_gui(config_path: Path) -> int:
    App(config_path).mainloop()
    return 0
