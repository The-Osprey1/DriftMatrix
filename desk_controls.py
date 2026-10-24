"""Local desktop controls. Opening this window never starts or restarts a sender.

Use --demo for an isolated, temporary queue and sample timer. The window talks
only to local files; the existing Utility Hub owns every panel request.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime
import math
from pathlib import Path
import tempfile
import time

import desk_reminder as reminders
from focus_control import FocusCommandQueue, FocusControlError, FocusStatus, StatusKind, default_data_dir, read_focus_status
import visual_system as visual
import hub_status
import desk_preferences

FRESH_SECONDS = 90
REMINDER_PRESETS = (("Tea", "TEA", 5), ("Water", "WATER", 30), ("Break", "BREAK", 60))


def reminder_caption(reminder: reminders.Reminder, now: float) -> str:
    phase = reminder.phase(now)
    if phase == "countdown":
        minutes, seconds = divmod(math.ceil(reminder.due_at - now), 60)
        deadline = datetime.fromtimestamp(reminder.due_at)
        pattern = "%I:%M %p" if deadline.date() == datetime.fromtimestamp(now).date() else "%a %I:%M %p"
        due = deadline.strftime(pattern).lstrip("0")
        return f"{reminder.label} · {minutes}:{seconds:02d} left · due {due}"
    return {"due": f"{reminder.label} · due now · snooze or dismiss",
            "expired": f"{reminder.label} · expired",
            "clock_changed": "System clock moved backwards · reminder hidden"}[phase]


def status_is_fresh(status: FocusStatus, now: float) -> bool:
    return status.available and status.checkpoint is not None and 0 <= now - status.checkpoint <= FRESH_SECONDS


def timer_text(status: FocusStatus) -> str:
    if status.remaining_seconds is None:
        return "--:--"
    minutes, seconds = divmod(status.remaining_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


@dataclass
class ControlsModel:
    data_dir: Path
    pending_id: str | None = None

    def __post_init__(self):
        self.queue = FocusCommandQueue(self.data_dir / "focus_commands", quarantine_malformed=False)
        self.reminder_path = self.data_dir / reminders.STATE_NAME
        self.preferences_path = self.data_dir / desk_preferences.PREFERENCES_NAME

    def focus_status(self) -> FocusStatus:
        return read_focus_status(self.data_dir / "focus_state.json")

    def send_focus(self, command: str, *, now: float | None = None, **kwargs) -> str:
        status = self.focus_status()
        if not status_is_fresh(status, time.time() if now is None else now):
            raise ValueError("Focus controller is not reporting fresh status. Start the Utility Hub first.")
        if self.pending_id is not None:
            raise ValueError("The previous command is still waiting for the focus controller.")
        receipt = self.queue.enqueue(command, **kwargs)
        self.pending_id = receipt.id
        return receipt.id

    def acknowledge(self, status: FocusStatus) -> bool:
        if self.pending_id is not None and self.pending_id in status.consumed_command_ids:
            self.pending_id = None
            return True
        return False


class DeskControls:
    def __init__(self, root, model: ControlsModel, *, demo: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.model, self.demo = root, model, demo
        self.tk, self.ttk = tk, ttk
        self.demo_started = time.time()
        self.preferences = desk_preferences.load(model.preferences_path)
        root.title("AWTRIX · Desk controls" + (" · DEMO" if demo else ""))
        root.minsize(680, 560)
        root.geometry(f"720x{max(560, min(900, root.winfo_screenheight() - 100))}")
        root.attributes("-topmost", self.preferences.keep_on_top)
        root.configure(background="#10151e")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#10151e")
        style.configure("TLabel", background="#10151e", foreground="#ecf1f8", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground="#aab9cf")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 24))
        style.configure("Eyebrow.TLabel", foreground="#9ce4c8", font=("Segoe UI Semibold", 9))
        style.configure("Timer.TLabel", font=("Segoe UI Light", 48), foreground="#fff7e8")
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 9),
                        background="#202c3a", foreground="#d9e5ef", borderwidth=0,
                        focuscolor="#99d8c3", lightcolor="#202c3a", darkcolor="#202c3a")
        style.map("TButton", background=[("disabled", "#19212c"), ("pressed", "#34475a"), ("active", "#2d3d50")],
                  foreground=[("disabled", "#8795a6")])
        style.configure("Accent.TButton", background="#a3dfc7", foreground="#10291f")
        style.map("Accent.TButton", background=[("disabled", "#253b34"), ("pressed", "#7dc8aa"), ("active", "#baf1dc")],
                  foreground=[("disabled", "#879d93"), ("!disabled", "#10291f")])
        style.configure("Quiet.TButton", background="#10151e", foreground="#aab9cf")
        style.configure("TEntry", fieldbackground="#0b1018", foreground="#edf4fa", insertcolor="#a3dfc7",
                        bordercolor="#344254", lightcolor="#344254", darkcolor="#344254", padding=9)
        style.configure("TSpinbox", fieldbackground="#0b1018", foreground="#edf4fa", insertcolor="#a3dfc7",
                        background="#202c3a", arrowcolor="#a3dfc7", bordercolor="#344254", padding=9)
        style.configure("TCheckbutton", background="#10151e", foreground="#aab9cf", font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", "#10151e")], foreground=[("active", "#ecf1f8")])
        style.configure("TLabelframe", background="#10151e", bordercolor="#2a3645", relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background="#10151e", foreground="#e5edf5", font=("Segoe UI Semibold", 11))
        # Keep every control reachable on smaller screens and at high DPI.
        self.viewport = tk.Canvas(root, background="#10151e", highlightthickness=0, width=680, height=760)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=self.viewport.yview)
        scrollbar.pack(side="right", fill="y")
        self.viewport.pack(side="left", fill="both", expand=True)
        self.viewport.configure(yscrollcommand=scrollbar.set)
        body = self.body = ttk.Frame(self.viewport, padding=24)
        content = self.viewport.create_window((0, 0), window=body, anchor="nw", width=680)
        body.bind("<Configure>", lambda event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(content, width=event.width))
        root.bind("<MouseWheel>", self.scroll_window)
        ttk.Label(body, text="A W T R I X   /   D E S K", style="Eyebrow.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(body, text="A calmer kind of productive.", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="A little focus. A timely reminder.", style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
        self.health = tk.StringVar(value="Reading local status…")
        ttk.Label(body, textvariable=self.health, wraplength=600, style="Muted.TLabel").pack(anchor="w")
        self.panel_health = tk.StringVar(value="Panel · waiting for status")
        self.reminder_health = tk.StringVar(value="Reminders · waiting for status")
        connections = ttk.Frame(body)
        connections.pack(fill="x", pady=(7, 0))
        ttk.Label(connections, textvariable=self.panel_health, style="Muted.TLabel").pack(side="left")
        ttk.Label(connections, textvariable=self.reminder_health, style="Muted.TLabel").pack(side="right")

        focus = ttk.LabelFrame(body, text="  Focus session  ", padding=18)
        focus.pack(fill="x", pady=14)
        self.timer = tk.StringVar(value="--:--")
        self.phase = tk.StringVar(value="No saved timer")
        ttk.Label(focus, textvariable=self.timer, style="Timer.TLabel").pack()
        ttk.Label(focus, textvariable=self.phase, style="Muted.TLabel").pack(pady=(0, 14))
        self.focus_buttons = []
        presets = ttk.Frame(focus)
        presets.pack(fill="x")
        for label, preset in (("15 min  ·  Sprint", "sprint"), ("25 min  ·  Classic", "classic"), ("50 min  ·  Deep", "deep")):
            self.focus_button(presets, label, "start", preset=preset)
        actions = ttk.Frame(focus)
        actions.pack(fill="x", pady=(8, 0))
        for label, action, args in (("Pause / resume", "toggle", {}), ("+5 min", "add", {"minutes": 5}), ("Finish", "finish", {}), ("Stop", "stop", {})):
            self.focus_button(actions, label, action, **args)

        reminder = ttk.LabelFrame(body, text="  A small reminder  ", padding=18)
        reminder.pack(fill="x")
        shortcuts = ttk.Frame(reminder)
        shortcuts.pack(fill="x", pady=(0, 8))
        for title, label, minutes in REMINDER_PRESETS:
            ttk.Button(shortcuts, text=f"{title} · {minutes}m", style="Quiet.TButton",
                       command=lambda label=label, minutes=minutes: self.select_preset(label, minutes)).pack(side="left", fill="x", expand=True)
        inputs = ttk.Frame(reminder)
        inputs.pack(fill="x")
        ttk.Label(inputs, text="REMIND ME", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(inputs, text="IN MINUTES", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=12)
        self.label = tk.StringVar(value=self.preferences.label)
        self.minutes = tk.StringVar(value=str(self.preferences.minutes))
        label_entry = ttk.Entry(inputs, textvariable=self.label, width=20)
        label_entry.grid(row=1, column=0, pady=5)
        minutes_entry = ttk.Spinbox(inputs, from_=1, to=1440, textvariable=self.minutes, width=9)
        minutes_entry.grid(row=1, column=1, padx=12)
        for entry in (label_entry, minutes_entry):
            entry.bind("<Return>", lambda event: self.set_reminder())
        self.set_button = ttk.Button(inputs, text="Set reminder", style="Accent.TButton", command=self.set_reminder)
        self.set_button.grid(row=1, column=2)
        buttons = ttk.Frame(reminder)
        buttons.pack(fill="x", pady=5)
        self.snooze_button = ttk.Button(buttons, text="Snooze 5 min", command=self.snooze)
        self.snooze_button.pack(side="left")
        self.dismiss_button = ttk.Button(buttons, text="Dismiss", style="Quiet.TButton", command=self.dismiss)
        self.dismiss_button.pack(side="left", padx=8)
        self.reminder_status = tk.StringVar(value="No reminder scheduled")
        ttk.Label(reminder, textvariable=self.reminder_status, wraplength=590).pack(anchor="w", pady=(6, 0))
        ttk.Label(reminder, text="Panel reminders must be enabled in your Utility Hub.", wraplength=590, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        self.canvas = tk.Canvas(body, width=576, height=144, background="#030507", highlightthickness=1, highlightbackground="#27303b")
        self.canvas.pack(pady=(18, 7))
        ttk.Label(body, text="32 × 8  /  LOCAL PREVIEW", style="Muted.TLabel").pack()
        self.message = tk.StringVar(value="Demo uses temporary local files." if demo else "Controls use the existing Utility Hub. Opening this window changes nothing on the panel.")
        ttk.Label(body, textvariable=self.message, wraplength=610).pack(anchor="w", pady=(14, 0))
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        self.pin = tk.BooleanVar(value=self.preferences.keep_on_top)
        ttk.Checkbutton(footer, text="Keep on top", variable=self.pin, command=self.set_topmost).pack(side="left")
        ttk.Button(footer, text="Preview label", style="Quiet.TButton", command=self.preview_label).pack(side="right")
        self.refresh()

    def focus_button(self, parent, label, action, **kwargs):
        style = "Accent.TButton" if action == "toggle" else "Quiet.TButton" if action in {"stop", "finish"} else "TButton"
        button = self.ttk.Button(parent, text=label, style=style, command=lambda: self.focus_action(action, **kwargs))
        button.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self.focus_buttons.append(button)

    def scroll_window(self, event):
        if event.widget.winfo_class() != "TSpinbox" and event.delta:
            self.viewport.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def focus_action(self, action, **kwargs):
        if self.demo:
            self.message.set("Demo: focus actions are not sent. Reminder controls use temporary files.")
            return
        try:
            self.model.send_focus(action, **kwargs)
            self.message.set("Queued. Waiting for the focus controller to consume this command…")
        except (ValueError, OSError, FocusControlError) as error:
            self.message.set(str(error))

    def select_preset(self, label, minutes):
        self.label.set(label)
        self.minutes.set(str(minutes))
        self.preview_label()
        self.message.set(f"{label.title()} preset selected. Set reminder when you’re ready.")

    def save_preferences(self):
        try:
            desk_preferences.save(self.model.preferences_path, self.preferences)
        except OSError:
            self.message.set("Your action worked, but preferences could not be saved for next time.")

    def set_topmost(self):
        self.root.attributes("-topmost", self.pin.get())
        self.preferences = replace(self.preferences, keep_on_top=self.pin.get())
        self.save_preferences()

    def set_reminder(self):
        try:
            minutes = int(self.minutes.get())
            saved = reminders.schedule(self.model.reminder_path, self.label.get(), minutes)
            self.message.set("Reminder saved locally. It will expire 10 minutes after its deadline.")
            self.preferences = replace(self.preferences, label=saved.label, minutes=minutes)
            self.save_preferences()
            self.sync_reminder_actions(saved)
        except ValueError as error:
            self.message.set(str(error) if "invalid literal" not in str(error) else "Minutes must be a whole number from 1 to 1440.")
        except OSError:
            self.message.set("Could not save the reminder. Check access to the local AWTRIX folder.")

    def snooze(self):
        try:
            current = reminders.read_reminder(self.model.reminder_path)
            if current is None:
                self.message.set("Set a reminder before snoozing it.")
                return
            reminders.schedule(self.model.reminder_path, current.label, 5)
            self.message.set("Reminder moved to five minutes from now.")
        except (ValueError, OSError):
            self.message.set("Could not snooze the saved reminder. Replace it with a new one.")

    def dismiss(self):
        try:
            self.model.reminder_path.unlink(missing_ok=True)
            self.sync_reminder_actions(None)
            self.message.set("Reminder dismissed locally; its panel page will clear on the next Hub refresh.")
        except OSError:
            self.message.set("Could not dismiss the reminder.")

    def paint(self, frame):
        self.canvas.delete("all")
        for y, row in enumerate(frame):
            for x, color in enumerate(row):
                fill = "#%02x%02x%02x" % color if color != visual.BLACK else "#0b0f15"
                self.canvas.create_oval(x * 18 + 3, y * 18 + 3, x * 18 + 15, y * 18 + 15, fill=fill, outline="")

    def preview_label(self):
        try:
            now = time.time()
            label = reminders.validate_label(self.label.get())
            self.paint(reminders.render(reminders.Reminder("preview", label, now, now + 300), now, show_label=True))
            self.preview_until = now + 5
        except ValueError as error:
            self.message.set(str(error))

    def sync_reminder_actions(self, reminder):
        self.set_button.configure(text="Replace reminder" if reminder else "Set reminder")
        self.snooze_button.state(["!disabled"] if reminder else ["disabled"])
        self.dismiss_button.state(["!disabled"] if self.model.reminder_path.exists() else ["disabled"])

    def refresh(self):
        now = time.time()
        try:
            status = (FocusStatus(StatusKind.WORK, 25 * 60 - int(now - self.demo_started) % 1500, checkpoint=now)
                      if self.demo else self.model.focus_status())
            fresh = status_is_fresh(status, now)
            self.timer.set(timer_text(status))
            self.phase.set(f"{status.kind.value.title()} · {status.cycle_count} completed focus sessions")
            if self.model.acknowledge(status):
                self.message.set("Focus controller consumed the command. The saved timer status has updated.")
            self.health.set("DEMO · isolated local controls" if self.demo else (
                "Timer service · running" if fresh else
                "Timer service · not reporting fresh status"))
        except (FocusControlError, OSError):
            fresh = False
            self.timer.set("--:--")
            self.phase.set("Saved focus status unavailable")
            self.health.set("Focus state could not be read · focus controls are disabled")
        for button in self.focus_buttons:
            button.state(["!disabled"] if (fresh and self.model.pending_id is None) else ["disabled"])
        current_reminder_id = None
        try:
            reminder = reminders.read_reminder(self.model.reminder_path)
            phase = reminder.phase(now) if reminder else "absent"
            if reminder and phase in {"countdown", "due"}:
                current_reminder_id = reminder.id
            self.sync_reminder_actions(reminder)
            text = reminder_caption(reminder, now) if reminder else "No reminder scheduled"
            self.reminder_status.set(text)
            if now >= getattr(self, "preview_until", 0):
                frame = reminders.render(reminder, now, show_label=int((now - reminder.created_at) // 10) % 2 == 1) if reminder else visual.new_frame()
                self.paint(frame)
        except (ValueError, OSError):
            self.reminder_status.set("Saved reminder unavailable · replace or dismiss it")
            self.sync_reminder_actions(None)
            self.paint(visual.new_frame())
        if self.demo:
            self.panel_health.set("Panel · demo preview")
            self.reminder_health.set("Reminders · temporary local files")
        else:
            panel_text, reminder_text = hub_status.indicator_text(
                hub_status.read_status(self.model.data_dir / hub_status.STATUS_NAME), now, current_reminder_id)
            self.panel_health.set(panel_text)
            self.reminder_health.set(reminder_text)
        self.root.after(1000, self.refresh)


def main(argv=None):
    import tkinter as tk
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="use temporary local reminder state; send no focus commands")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="awtrix-controls-") as temporary:
        directory = Path(temporary) if args.demo else default_data_dir()
        if args.demo:
            reminders.schedule(directory / reminders.STATE_NAME, "TEA", 5)
        root = tk.Tk()
        DeskControls(root, ControlsModel(directory), demo=args.demo)
        root.mainloop()


if __name__ == "__main__":
    main()
