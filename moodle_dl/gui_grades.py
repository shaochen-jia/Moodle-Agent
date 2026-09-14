"""The grades page: what you have, what it is worth, and what is left.

Everything on this page is editable, because everything on it can be wrong.
The weights come from a slide in a PDF, the marks come from a gradebook with
no weight column, and the quiz block is four quizzes when Moodle has made
three. So each number is a field, the arithmetic re-runs on every keystroke,
and nothing is written to disk until Save is pressed.

A field the user touches is marked as theirs and is never overwritten by a
later sync again. That is the whole contract of the page: the tool guesses
first, the user has the last word, and the last word sticks.
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

from . import grades, theme as t
from .config import Config

_NUM = "0123456789.-"


def _f(size: int = 13, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family=t.FONT, size=size,
                       weight="bold" if bold else "normal")


def _mono(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=t.MONO, size=size)


class GradesPage:
    """Built into the app's window; owns its own widgets and nothing else."""

    def __init__(self, app, cfg: Config, on_back, on_refresh):
        self.app = app
        self.cfg = cfg
        self.on_back = on_back
        self.on_refresh = on_refresh
        self.book = grades.Book(grades.default_path())
        self.codes = [u.code for u in cfg.units] or sorted(self.book.units)
        self.code = self.codes[0] if self.codes else ""
        self.dirty = False
        self.rows: list[tuple] = []

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        self.app._clear()
        page = ctk.CTkFrame(self.app, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=22, pady=20)
        card = ctk.CTkFrame(page, fg_color=t.CARD, corner_radius=t.RADIUS_CARD,
                            border_width=1, border_color=t.BORDER)
        card.pack(fill="both", expand=True)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 10))
        ctk.CTkLabel(head, text="Grades", font=_f(16, True),
                     text_color=t.TEXT).pack(side="left")
        self.saved_hint = ctk.CTkLabel(head, text="", font=_f(12),
                                       text_color=t.TEXT_MUTED)
        self.saved_hint.pack(side="left", padx=10)
        ctk.CTkButton(head, text="Back", command=self.on_back, width=64,
                      height=26, corner_radius=t.RADIUS_CTL, font=_f(12),
                      fg_color="transparent", hover_color=t.GHOST_HOVER,
                      text_color=t.TEXT_SECONDARY,
                      border_width=0).pack(side="right")
        ctk.CTkButton(head, text="Refresh from Moodle",
                      command=self._refresh, width=150, height=26,
                      corner_radius=t.RADIUS_CTL, font=_f(12),
                      fg_color="transparent", hover_color=t.GHOST_HOVER,
                      text_color=t.TEXT_SECONDARY,
                      border_width=1, border_color=t.BORDER).pack(side="right",
                                                                 padx=8)

        if not self.codes:
            ctk.CTkLabel(card, text="No units yet - run a sync first.",
                         font=_f(13), text_color=t.TEXT_SECONDARY).pack(pady=40)
            return

        if len(self.codes) > 1:
            self.tabs = ctk.CTkSegmentedButton(
                card, values=self.codes, command=self._switch, font=_f(12),
                selected_color=t.ACCENT, selected_hover_color=t.ACCENT_HOVER,
                unselected_color=t.SUBTLE, unselected_hover_color=t.GHOST_HOVER,
                text_color=t.ON_ACCENT, fg_color=t.SUBTLE)
            self.tabs.set(self.code)
            self.tabs.pack(fill="x", padx=18, pady=(0, 12))

        self.summary = ctk.CTkFrame(card, fg_color=t.SUBTLE,
                                    corner_radius=t.RADIUS_CTL)
        self.summary.pack(fill="x", padx=18, pady=(0, 10))

        self.list = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self.list.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.pack(fill="x", padx=18, pady=(2, 14))
        ctk.CTkButton(foot, text="+ Add an assessment", command=self._add,
                      width=150, height=30, corner_radius=t.RADIUS_CTL,
                      font=_f(12), fg_color="transparent",
                      hover_color=t.GHOST_HOVER, text_color=t.TEXT_SECONDARY,
                      border_width=1, border_color=t.BORDER).pack(side="left")
        self.save_btn = ctk.CTkButton(
            foot, text="Save", command=self._save, width=104, height=32,
            corner_radius=t.RADIUS_CTL, font=_f(13, True), fg_color=t.ACCENT,
            hover_color=t.ACCENT_HOVER, text_color=t.ON_ACCENT)
        self.save_btn.pack(side="right")
        self.discard_btn = ctk.CTkButton(
            foot, text="Discard", command=self._discard, width=80, height=32,
            corner_radius=t.RADIUS_CTL, font=_f(12), fg_color="transparent",
            hover_color=t.GHOST_HOVER, text_color=t.TEXT_SECONDARY,
            border_width=0)
        self.discard_btn.pack(side="right", padx=6)

        self._render()

    # --------------------------------------------------------------- render

    def _switch(self, code: str) -> None:
        if self.dirty and not self._confirm_leave():
            self.tabs.set(self.code)
            return
        self.code = code
        self.book = grades.Book(grades.default_path())
        self.dirty = False
        self._render()

    def _render(self) -> None:
        for w in self.list.winfo_children():
            w.destroy()
        self.rows = []
        unit = self.book.unit(self.code)

        header = ctk.CTkFrame(self.list, fg_color="transparent")
        header.pack(fill="x", padx=6, pady=(2, 4))
        for text, w, side in (("Assessment", 0, "left"), ("counts", 78, "right"),
                              ("out of", 74, "right"), ("score", 74, "right"),
                              ("weight", 74, "right")):
            ctk.CTkLabel(header, text=text, font=_f(11),
                         text_color=t.TEXT_MUTED,
                         width=w or 160, anchor="w" if side == "left" else "e"
                         ).pack(side=side, padx=(0, 4))

        for item in unit.items:
            self._item_row(item)
            for part in item.parts:
                self._part_row(item, part)

        # The rows are built empty and filled by the same code that runs on
        # every keystroke, so the first paint cannot drift from later ones.
        self._recalc()

    def _item_row(self, item: grades.Item) -> None:
        row = ctk.CTkFrame(self.list, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=1)

        keep = ctk.BooleanVar(value=item.include)
        ctk.CTkCheckBox(row, text="", variable=keep, width=22, checkbox_width=17,
                        checkbox_height=17, corner_radius=4,
                        fg_color=t.ACCENT, hover_color=t.ACCENT_HOVER,
                        border_color=t.BORDER_STRONG,
                        command=lambda: self._touch(item, "include", keep.get())
                        ).pack(side="left")

        name = ctk.CTkLabel(row, text=item.name, font=_f(12),
                            text_color=t.TEXT if item.include else t.TEXT_MUTED,
                            anchor="w", width=150)
        name.pack(side="left", fill="x", expand=True)

        counts = self._entry(row, item.count_best or "", 78,
                             lambda v: self._touch(item, "count_best", v),
                             placeholder=str(item.total_parts)
                             if item.grouped else "-",
                             enabled=item.grouped)
        out_of = self._entry(row, "" if item.grouped else _g(item.out_of), 74,
                             lambda v: self._touch(item, "out_of", v),
                             enabled=not item.grouped)
        score = self._entry(row, "" if item.grouped else _g(item.score), 74,
                            lambda v: self._touch(item, "score", v),
                            enabled=not item.grouped)
        weight = self._entry(row, _g(item.weight), 74,
                             lambda v: self._touch(item, "weight", v))

        contrib = ctk.CTkLabel(row, text="", font=_mono(11),
                               text_color=t.TEXT_SECONDARY, width=104,
                               anchor="e")
        contrib.pack(side="right", padx=(6, 0))

        src = ctk.CTkLabel(self.list, text=self._source_line(item), font=_f(10),
                           text_color=t.TEXT_MUTED, anchor="w")
        src.pack(fill="x", padx=(34, 6), pady=(0, 3))

        self.rows.append((item, None, contrib, name, src,
                          (counts, out_of, score, weight)))

    def _part_row(self, item: grades.Item, part: grades.Part) -> None:
        row = ctk.CTkFrame(self.list, fg_color="transparent")
        row.pack(fill="x", padx=(40, 6), pady=1)
        ctk.CTkLabel(row, text=part.name or "(not set yet)", font=_f(11),
                     text_color=t.TEXT_SECONDARY, anchor="w", width=140
                     ).pack(side="left", fill="x", expand=True)
        self._entry(row, _g(part.out_of), 74,
                    lambda v: self._touch_part(part, "out_of", v))
        self._entry(row, _g(part.score), 74,
                    lambda v: self._touch_part(part, "score", v))
        ctk.CTkLabel(row, text="", width=74).pack(side="right", padx=(6, 0))
        lab = ctk.CTkLabel(row, text="", font=_mono(11),
                           text_color=t.TEXT_MUTED, width=104, anchor="e")
        lab.pack(side="right", padx=(6, 0))
        self.rows.append((item, part, lab, None, None, ()))

    def _entry(self, parent, value, width, on_change, placeholder="",
               enabled=True) -> ctk.CTkEntry:
        var = tk.StringVar(value=str(value))
        e = ctk.CTkEntry(parent, textvariable=var, width=width, height=26,
                         font=_mono(11), justify="right",
                         corner_radius=t.RADIUS_CTL, border_width=1,
                         border_color=t.BORDER, fg_color=t.CARD,
                         text_color=t.TEXT, placeholder_text=placeholder)
        e.pack(side="right", padx=(6, 0))
        if not enabled:
            e.configure(state="disabled", fg_color=t.SUBTLE)
        else:
            var.trace_add("write", lambda *_: on_change(var.get()))
        return e

    # ---------------------------------------------------------------- edits

    def _touch(self, item: grades.Item, field: str, raw) -> None:
        """Apply one edit to the model in memory. Disk waits for Save."""
        if field == "include":
            item.include = bool(raw)
        elif field == "count_best":
            item.count_best = _int(raw)
        else:
            value = _float(raw)
            if field == "out_of" and (value is None or value <= 0):
                value = item.out_of  # a zero denominator is not an opinion
            setattr(item, field, value)
        item.lock(field)
        if field in ("score", "out_of"):
            item.score_from = "you"
        if field == "weight":
            item.weight_from = "you"
        self._changed()

    def _touch_part(self, part: grades.Part, field: str, raw) -> None:
        value = _float(raw)
        if field == "out_of" and (value is None or value <= 0):
            return
        setattr(part, field, value)
        part.edited = True
        self._changed()

    def _changed(self) -> None:
        self.dirty = True
        self.saved_hint.configure(text="unsaved changes", text_color=t.DANGER)
        self._recalc()

    def _recalc(self) -> None:
        unit = self.book.unit(self.code)
        for item, part, label, name, src, _ in self.rows:
            if part is not None:
                f = part.fraction
                label.configure(text="-" if f is None else f"{f * 100:.0f}%")
                continue
            got = item.percent()
            earned = item.earned()
            if item.weight is None:
                label.configure(text="weight?", text_color=t.DANGER)
            elif got is None:
                label.configure(text=f"- / {_g(item.weight)}%",
                                text_color=t.TEXT_MUTED)
            else:
                label.configure(text=f"{earned:.2f} / {_g(item.weight)}%",
                                text_color=t.TEXT_SECONDARY)
            if name is not None:
                name.configure(text_color=t.TEXT if item.include else t.TEXT_MUTED)
            if src is not None:
                src.configure(text=self._source_line(item))
        self._summary(unit)

    def _source_line(self, item: grades.Item) -> str:
        where = item.weight_from or "not found - type it in"
        if where not in ("you", "item name", "range"):
            where = Path(where).name if where else where
        bits = [f"weight from {where}"]
        if item.grouped:
            bits.append(f"{len(item.parts)} parts, best {item.counted_parts} count")
        if item.source == "plan":
            bits.append("not in Moodle's gradebook yet")
        return "     " + "  ·  ".join(bits)

    # -------------------------------------------------------------- summary

    def _summary(self, unit: grades.Unit) -> None:
        for w in self.summary.winfo_children():
            w.destroy()

        top = ctk.CTkFrame(self.summary, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 4))
        st = unit.standing()
        _stat(top, "so far", "-" if st is None else f"{st:.1f}%",
              t.TEXT if st is None else _band_colour(unit.band()))
        _stat(top, "marked", f"{unit.assessed():g}%")
        _stat(top, "left", f"{unit.remaining():g}%")
        _stat(top, "weights", f"{unit.weight_total():g}%",
              t.SUCCESS if unit.weights_complete() else t.DANGER)

        target = ctk.CTkFrame(self.summary, fg_color="transparent")
        target.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkLabel(target, text="I want", font=_f(12),
                     text_color=t.TEXT_SECONDARY).pack(side="left")
        choice = ctk.CTkSegmentedButton(
            target, values=["HD", "D", "C", "P", "-"], font=_f(11), width=190,
            command=lambda v: self._set_target(unit, v),
            selected_color=t.ACCENT, selected_hover_color=t.ACCENT_HOVER,
            unselected_color=t.CARD, unselected_hover_color=t.GHOST_HOVER,
            text_color=t.ON_ACCENT, fg_color=t.CARD)
        choice.set(unit.target or "-")
        choice.pack(side="left", padx=10)
        ctk.CTkLabel(target, text=self._verdict(unit), font=_f(12),
                     text_color=t.TEXT).pack(side="left", padx=4)

        problems = self._problems(unit)
        if problems:
            ctk.CTkLabel(self.summary, text=problems, font=_f(11),
                         text_color=t.DANGER, anchor="w", justify="left"
                         ).pack(fill="x", padx=14, pady=(0, 10))

    def _verdict(self, unit: grades.Unit) -> str:
        need = unit.needed()
        if need is None:
            if unit.target and unit.remaining() <= 0:
                return "everything is marked - this is your result"
            return ""
        if need <= 0:
            return f"already safe - {unit.target} is locked in"
        if need > 100:
            return f"out of reach: would need {need:.0f}% on what is left"
        return f"need {need:.1f}% average on the remaining {unit.remaining():g}%"

    def _problems(self, unit: grades.Unit) -> str:
        out = []
        total = unit.weight_total()
        if not unit.weights_complete():
            gap = 100 - total
            word = "missing" if gap > 0 else "over"
            out.append(f"Weights add up to {total:g}%, not 100% "
                       f"({abs(gap):g}% {word}) - the totals below are only as "
                       f"right as these numbers.")
        missing = unit.unweighted()
        if missing:
            names = ", ".join(i.name[:28] for i in missing[:3])
            more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
            out.append(f"No weight found for: {names}{more}. "
                       f"Type one in, or untick to leave it out.")
        return "\n".join(out)

    def _set_target(self, unit: grades.Unit, value: str) -> None:
        unit.target = "" if value == "-" else value
        self._changed()

    # --------------------------------------------------------------- actions

    def _add(self) -> None:
        unit = self.book.unit(self.code)
        n = sum(1 for i in unit.items if i.source == "you") + 1
        unit.items.append(grades.Item(
            id=f"you:{self.code}:{n}:{int(len(unit.items))}",
            name=f"New assessment {n}", source="you", weight_from="you"))
        self._changed()
        self._render()

    def _save(self) -> None:
        try:
            self.book.save()
        except OSError as e:
            self.saved_hint.configure(text=f"could not save: {e}",
                                      text_color=t.DANGER)
            return
        self.dirty = False
        self.saved_hint.configure(text="saved", text_color=t.SUCCESS)
        self.app.after(2500, lambda: self.saved_hint.winfo_exists()
                       and self.saved_hint.configure(text=""))

    def _discard(self) -> None:
        if self.dirty and not self._confirm_leave():
            return
        self.book = grades.Book(grades.default_path())
        self.dirty = False
        self.saved_hint.configure(text="")
        self._render()

    def _confirm_leave(self) -> bool:
        from tkinter import messagebox
        return messagebox.askyesno(
            "Unsaved changes",
            "You have edits that have not been saved. Throw them away?")

    def _refresh(self) -> None:
        if self.dirty and not self._confirm_leave():
            return
        self.saved_hint.configure(text="checking Moodle...",
                                  text_color=t.TEXT_SECONDARY)
        threading.Thread(target=self._refresh_bg, daemon=True).start()

    def _refresh_bg(self) -> None:
        from .main import refresh_grades
        try:
            refresh_grades(self.cfg)
            err = ""
        except Exception as e:  # noqa: BLE001 - shown, never raised into tk
            err = f"{type(e).__name__}: {e}"
        self.app.after(0, lambda: self._refreshed(err))

    def _refreshed(self, err: str) -> None:
        if err:
            self.saved_hint.configure(text=err[:70], text_color=t.DANGER)
            return
        self.book = grades.Book(grades.default_path())
        self.dirty = False
        self.saved_hint.configure(text="up to date", text_color=t.SUCCESS)
        self._render()


def _stat(parent, label: str, value: str, colour=None) -> None:
    box = ctk.CTkFrame(parent, fg_color="transparent")
    box.pack(side="left", padx=(0, 26))
    ctk.CTkLabel(box, text=value, font=_f(19, True),
                 text_color=colour or t.TEXT).pack(anchor="w")
    ctk.CTkLabel(box, text=label, font=_f(11),
                 text_color=t.TEXT_MUTED).pack(anchor="w")


def _band_colour(band: str):
    return t.SUCCESS if band in ("HD", "D") else t.TEXT


def _g(v) -> str:
    """A number as a person would write it: 20, not 20.0."""
    if v is None:
        return ""
    return f"{float(v):g}"


def _float(raw) -> float | None:
    raw = str(raw).strip()
    if not raw or raw in "-.":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(raw) -> int | None:
    v = _float(raw)
    return int(v) if v and v > 0 else None
