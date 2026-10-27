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
import control_status
from config_loader import CanonicalConfig, ConfigError, default_config_path, load_config

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
    config: CanonicalConfig | None = None

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.pending_at = None
        self.queue = FocusCommandQueue(self.data_dir / "focus_commands", quarantine_malformed=False)
        self.reminder_path = self.data_dir / reminders.STATE_NAME
        self.preferences_path = self.data_dir / desk_preferences.PREFERENCES_NAME

    def focus_status(self) -> FocusStatus:
        return read_focus_status(self.data_dir / "focus_state.json")

    def send_focus(self, command: str, *, now: float | None = None, **kwargs) -> str:
        if self.config is not None:
            config = self.current_config()
            hub = config.services['hub']
            if not hub.enabled or not hub.options.get('focus_sprint', {}).get('enabled', True):
                raise ValueError('Focus is disabled in the selected configuration.')
            snapshot = self.service_snapshot(config)
            if not snapshot.get('live') or not snapshot.get('services', {}).get('hub', {}).get('live'):
                raise ValueError('The host and Utility Hub must be running before a focus action can be queued.')
            if snapshot.get('config_matches') is not True:
                raise ValueError('The running host uses a different configuration. Check Services before sending a focus action.')
        status = self.focus_status()
        if not status_is_fresh(status, time.time() if now is None else now):
            raise ValueError("Focus controller is not reporting fresh status. Start the Utility Hub first.")
        if self.pending_id is not None:
            raise ValueError("The previous command is still waiting for the focus controller.")
        receipt = self.queue.enqueue(command, **kwargs)
        self.pending_id = receipt.id
        self.pending_at = time.time() if now is None else now
        return receipt.id

    def acknowledge(self, status: FocusStatus) -> bool:
        if self.pending_id is not None and self.pending_id in status.consumed_command_ids:
            self.pending_id = None
            self.pending_at = None
            return True
        return False

    def pending_state(self, status, now):
        if self.acknowledge(status):
            return 'confirmed'
        if self.pending_id is None:
            return 'idle'
        path = self.queue.directory / (self.pending_id + '.json')
        if (not path.exists() and self.pending_at is not None
                and status_is_fresh(status, now) and status.checkpoint > self.pending_at + 5):
            self.pending_id = None
            self.pending_at = None
            return 'unconfirmed'
        return 'slow' if self.pending_at is not None and now - self.pending_at >= 30 else 'waiting'

    def current_config(self):
        config = load_config(self.config.source_path)
        if (config.panel.url, config.runtime.data_dir) != (self.config.panel.url, self.data_dir):
            raise ConfigError('Configuration target changed. Reopen Desk Controls before sending actions.')
        return config

    @staticmethod
    def service_snapshot(config):
        from supervisor import status
        return status(config)

    def host_summary(self):
        if self.config is None:
            return control_status.summarize(configured=False)
        try:
            return control_status.summarize(self.service_snapshot(self.current_config()))
        except (ConfigError, OSError):
            return control_status.HostSummary('Configuration or status unavailable',
                'Check the selected configuration. Reopen controls if its panel or data folder changed.', ())

    def focus_configuration_reason(self):
        if self.config is None:
            return None
        try:
            config = self.current_config()
            hub = config.services['hub']
            if not hub.enabled or not hub.options.get('focus_sprint', {}).get('enabled', True):
                return 'Focus is disabled in the selected configuration.'
        except (ConfigError, OSError):
            return 'Configuration unavailable or target changed. Reopen Desk Controls.'
        return None


def controls_model(*, config_path=None, data_dir=None):
    if data_dir is not None:
        return ControlsModel(Path(data_dir))
    path = Path(config_path) if config_path is not None else default_config_path()
    if config_path is not None or path.exists():
        config = load_config(path)
        return ControlsModel(config.runtime.data_dir, config=config)
    return ControlsModel(default_data_dir())


def minute_value(value, label='Minutes'):
    try:
        if type(value) not in (str, int):
            raise ValueError()
        minutes = int(value)
        if not 1 <= minutes <= 1440:
            raise ValueError()
        return minutes
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a whole number from 1 to 1440.') from None


class DeskControls:
    def __init__(self, root, model: ControlsModel, *, demo: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.model, self.demo = root, model, demo
        self.tk, self.ttk = tk, ttk
        self.demo_started = time.time()
        self.preferences = desk_preferences.load(model.preferences_path)
        root.title("DriftMatrix · Desk Controls" + (" · DEMO" if demo else ""))
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
        style.configure("TNotebook", background="#10151e", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Segoe UI", 10),
                        background="#202c3a", foreground="#aab9cf", borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#10151e"), ("active", "#2d3d50")],
                  foreground=[("selected", "#a3dfc7"), ("active", "#ecf1f8")])
        # Action feedback stays in view when either tab is scrolled.
        self.message = tk.StringVar(value="Demo uses temporary local files." if demo else
            "Controls use the existing Utility Hub. Opening this window changes nothing on the panel.")
        feedback = ttk.Frame(root, padding=(18, 10))
        feedback.pack(side="bottom", fill="x")
        ttk.Label(feedback, textvariable=self.message, wraplength=610).pack(anchor="w")
        self.tabs = ttk.Notebook(root)
        self.tabs.enable_traversal()
        self.tabs.pack(fill="both", expand=True)
        desk_tab, services_tab = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(desk_tab, text="Focus & reminders")
        self.tabs.add(services_tab, text="Services")
        self.viewport = tk.Canvas(desk_tab, background="#10151e", highlightthickness=0, width=680, height=760)
        scrollbar = ttk.Scrollbar(desk_tab, orient="vertical", command=self.viewport.yview)
        scrollbar.pack(side="right", fill="y")
        self.viewport.pack(side="left", fill="both", expand=True)
        self.viewport.configure(yscrollcommand=scrollbar.set)
        body = self.body = ttk.Frame(self.viewport, padding=24)
        content = self.viewport.create_window((0, 0), window=body, anchor="nw", width=680)
        body.bind("<Configure>", lambda event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(content, width=event.width))
        root.bind("<MouseWheel>", self.scroll_window)
        ttk.Label(body, text="D R I F T M A T R I X   /   D E S K", style="Eyebrow.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(body, text="Focus & reminders", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Start a focus session or schedule a panel reminder.", style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
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
        self.focus_actions = []
        presets = ttk.Frame(focus)
        presets.pack(fill="x")
        for label, preset in (("15 min  ·  Sprint", "sprint"), ("25 min  ·  Classic", "classic"), ("50 min  ·  Deep", "deep")):
            self.focus_button(presets, label, "start", preset=preset)
        actions = ttk.Frame(focus)
        actions.pack(fill="x", pady=(8, 0))
        for label, action, args in (("Pause / resume", "toggle", {}), ("+5 min", "add", {"minutes": 5}), ("Finish", "finish", {}), ("Stop", "stop", {})):
            self.focus_button(actions, label, action, **args)

        custom = ttk.Frame(focus)
        custom.pack(fill="x", pady=(12, 0))
        ttk.Label(custom, text="Custom focus · minutes", style="Muted.TLabel").pack(side="left")
        self.focus_minutes = tk.StringVar(value=str(self.preferences.focus_minutes))
        custom_entry = ttk.Spinbox(custom, from_=1, to=1440, textvariable=self.focus_minutes, width=7)
        custom_entry.pack(side="left", padx=10)
        custom_entry.bind("<Return>", lambda event: self.start_custom_focus())
        custom_button = ttk.Button(custom, text="Start custom", command=self.start_custom_focus)
        custom_button.pack(side="right")
        self.focus_buttons.append(custom_button)
        self.focus_actions.append((custom_button, 'start'))

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
        ttk.Label(buttons, text="Snooze for", style="Muted.TLabel").pack(side="left")
        self.snooze_minutes = tk.StringVar(value=str(self.preferences.snooze_minutes))
        ttk.Spinbox(buttons, from_=1, to=1440, textvariable=self.snooze_minutes, width=5).pack(side="left", padx=8)
        ttk.Label(buttons, text="min", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.snooze_button = ttk.Button(buttons, text="Snooze", command=self.snooze)
        self.snooze_button.pack(side="left")
        self.dismiss_button = ttk.Button(buttons, text="Dismiss", style="Quiet.TButton", command=self.dismiss)
        self.dismiss_button.pack(side="left", padx=8)
        self.reminder_status = tk.StringVar(value="No reminder scheduled")
        ttk.Label(reminder, textvariable=self.reminder_status, wraplength=590).pack(anchor="w", pady=(6, 0))
        ttk.Label(reminder, text="Panel reminders must be enabled in your Utility Hub.", wraplength=590, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        self.canvas = tk.Canvas(body, width=576, height=144, background="#030507", highlightthickness=1, highlightbackground="#27303b")
        self.canvas.pack(pady=(18, 7))
        ttk.Label(body, text="32 × 8  /  LOCAL PREVIEW", style="Muted.TLabel").pack()
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        self.pin = tk.BooleanVar(value=self.preferences.keep_on_top)
        ttk.Checkbutton(footer, text="Keep on top", variable=self.pin, command=self.set_topmost).pack(side="left")
        ttk.Button(footer, text="Preview label", style="Quiet.TButton", command=self.preview_label).pack(side="right")
        self.build_services_tab(services_tab)
        root.bind('<Destroy>', self.cancel_refresh, add='+')
        self.refresh()

    def cancel_refresh(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        identifier = getattr(self, 'refresh_after_id', None)
        if identifier is not None:
            try:
                self.root.after_cancel(identifier)
            except self.tk.TclError:
                pass
            self.refresh_after_id = None

    def build_services_tab(self, parent):
        tk, ttk = self.tk, self.ttk
        self.service_viewport = tk.Canvas(parent, background='#10151e', highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient='vertical', command=self.service_viewport.yview)
        scroll.pack(side='right', fill='y')
        self.service_viewport.pack(fill='both', expand=True)
        self.service_viewport.configure(yscrollcommand=scroll.set)
        frame = ttk.Frame(self.service_viewport, padding=24)
        content = self.service_viewport.create_window((0, 0), window=frame, anchor='nw', width=680)
        frame.bind('<Configure>', lambda event: self.service_viewport.configure(
            scrollregion=self.service_viewport.bbox('all')))
        self.service_viewport.bind('<Configure>', lambda event: self.service_viewport.itemconfigure(
            content, width=event.width))
        ttk.Label(frame, text='Host services', style='Title.TLabel').pack(anchor='w')
        self.host_headline = tk.StringVar(value='Reading local status…')
        self.host_detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.host_headline, font=('Segoe UI Semibold', 13)).pack(anchor='w', pady=(20, 4))
        ttk.Label(frame, textvariable=self.host_detail, wraplength=590, style='Muted.TLabel').pack(anchor='w')
        self.service_rows = {}
        for name, label in control_status.SERVICE_LABELS.items():
            row = ttk.Frame(frame, padding=(0, 14))
            row.pack(fill='x')
            value, detail = tk.StringVar(), tk.StringVar()
            ttk.Label(row, text=label, font=('Segoe UI Semibold', 11)).pack(anchor='w')
            ttk.Label(row, textvariable=value).pack(anchor='w', pady=(3, 0))
            ttk.Label(row, textvariable=detail, wraplength=590, style='Muted.TLabel').pack(anchor='w')
            self.service_rows[label] = (value, detail)
        self.service_updated = tk.StringVar()
        ttk.Label(frame, textvariable=self.service_updated, style='Muted.TLabel').pack(anchor='w', pady=12)
        ttk.Button(frame, text='Refresh status', command=self.refresh_services).pack(anchor='w')
        ttk.Label(frame, text='This view reads local process status. Opening it does not start, stop or restart services.',
                  wraplength=590, style='Muted.TLabel').pack(anchor='w', pady=(14, 0))

    def refresh_services(self):
        if self.demo:
            snapshot = {'live': True, 'healthy': True, 'config_matches': True, 'services': {
                name: {'state': 'DISABLED' if name == 'drift_link' else 'RUNNING',
                       'live': name != 'drift_link', 'restart_count': 0}
                for name in control_status.SERVICE_LABELS}}
            summary = control_status.summarize(snapshot)
        else:
            summary = self.model.host_summary()
        self.host_headline.set(('DEMO · ' if self.demo else '') + summary.headline)
        self.host_detail.set(summary.detail)
        rows = {label: (state, note) for label, state, note in summary.rows}
        for label, (value, detail) in self.service_rows.items():
            shown, note = rows.get(label, ('Not reported', ''))
            value.set(shown); detail.set(note)
        self.service_updated.set(('Sample data · ' if self.demo else 'Checked locally · ') +
                                 datetime.now().strftime('%H:%M:%S'))

    def focus_button(self, parent, label, action, **kwargs):
        style = "Accent.TButton" if action == "toggle" else "Quiet.TButton" if action in {"stop", "finish"} else "TButton"
        button = self.ttk.Button(parent, text=label, style=style, command=lambda: self.focus_action(action, **kwargs))
        button.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self.focus_buttons.append(button)
        self.focus_actions.append((button, action))
        if action == 'toggle':
            self.toggle_button = button

    def scroll_window(self, event):
        if event.widget.winfo_class() != "TSpinbox" and event.delta:
            view = self.service_viewport if self.tabs.index(self.tabs.select()) == 1 else self.viewport
            view.yview_scroll(-1 if event.delta > 0 else 1, "units")
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
            return False
        return True

    def start_custom_focus(self):
        try:
            minutes = minute_value(self.focus_minutes.get(), 'Focus minutes')
            accepted = self.focus_action('start', minutes=minutes)
            if self.demo or accepted:
                self.preferences = replace(self.preferences, focus_minutes=minutes)
                self.save_preferences()
        except ValueError as error:
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
            minutes = minute_value(self.snooze_minutes.get(), 'Snooze minutes')
            reminders.schedule(self.model.reminder_path, current.label, minutes)
            self.preferences = replace(self.preferences, snooze_minutes=minutes)
            self.message.set(f"Reminder moved to {minutes} minutes from now.")
            self.save_preferences()
        except ValueError as error:
            self.message.set(str(error))
        except OSError:
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
        self.cancel_refresh()
        now = time.time()
        status = FocusStatus(StatusKind.IDLE, available=False)
        try:
            status = (FocusStatus(StatusKind.WORK, 25 * 60 - int(now - self.demo_started) % 1500, checkpoint=now)
                      if self.demo else self.model.focus_status())
            # The controller can publish while this window is reading. Sample
            # freshness afterwards so a valid new checkpoint is not in the future.
            now = time.time()
            fresh = status_is_fresh(status, now)
            self.timer.set(timer_text(status))
            self.phase.set(f"{status.kind.value.title()} · {status.cycle_count} completed focus sessions")
            pending = self.model.pending_state(status, now)
            if pending == 'confirmed':
                self.message.set("Focus action confirmed in the saved timer state.")
            elif pending == 'slow':
                self.message.set("Still waiting for the Focus controller. Check Services; this action has not been resent.")
            elif pending == 'unconfirmed':
                self.message.set("The action is no longer queued, but its receipt is unavailable. Check the timer before sending another action.")
            self.health.set("DEMO · isolated local controls" if self.demo else (
                "Timer service · running" if fresh else
                "Timer service · not reporting fresh status"))
        except (FocusControlError, OSError):
            fresh = False
            self.timer.set("--:--")
            self.phase.set("Saved focus status unavailable")
            self.health.set("Focus state could not be read · focus controls are disabled")
        reason = None if self.demo else self.model.focus_configuration_reason()
        if reason:
            fresh = False
            self.health.set(reason)
        active = fresh and status.kind in {StatusKind.WORK, StatusKind.REST, StatusKind.PAUSED}
        for button, action in self.focus_actions:
            allowed = fresh and self.model.pending_id is None and (action == 'start' or active)
            button.state(["!disabled"] if allowed else ["disabled"])
        self.toggle_button.configure(text='Resume' if fresh and status.kind is StatusKind.PAUSED else 'Pause')
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
        if now >= getattr(self, 'next_service_refresh', 0):
            self.refresh_services()
            self.next_service_refresh = now + 5
        self.refresh_after_id = self.root.after(1000, self.refresh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--demo', action='store_true', help='use isolated temporary state')
    target.add_argument('--config', type=Path, help='canonical driftmatrix.json')
    target.add_argument('--data-dir', type=Path, help='explicit legacy state folder')
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix='driftmatrix-controls-') as temporary:
            model = ControlsModel(Path(temporary)) if args.demo else controls_model(
                config_path=args.config, data_dir=args.data_dir)
            if args.demo:
                reminders.schedule(model.reminder_path, 'TEA', 5)
            import tkinter as tk
            root = tk.Tk()
            DeskControls(root, model, demo=args.demo)
            root.mainloop()
        return 0
    except (ConfigError, OSError) as error:
        print('Desk Controls could not open: ' + (str(error) if isinstance(error, ConfigError) else type(error).__name__))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
