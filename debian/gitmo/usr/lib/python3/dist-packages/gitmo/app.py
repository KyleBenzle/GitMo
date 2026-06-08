from __future__ import annotations

import subprocess
import sys
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from gitmo.config import (
    APP_DIR,
    CONFIG_PATH,
    CREDENTIALS_PATH,
    LEGACY_CONFIG_PATH,
    LOG_PATH,
    AppConfig,
    CachedGitHubRepo,
    RepoConfig,
    load_config,
    save_config,
)
from gitmo.github_api import GitHubAPIError, GitHubClient, GitHubRepo
from gitmo.git_cli import (
    GitCommandError,
    clone_repo,
    init_repo,
    is_git_repo,
    set_local_identity,
    set_remote_url,
)
from gitmo.sync_engine import SyncEngine


BASE_DIR = Path(__file__).resolve().parent.parent
SYSTEM_ASSET_DIR = Path("/usr/share/gitmo")
ASSET_DIR = SYSTEM_ASSET_DIR if (SYSTEM_ASSET_DIR / "logo.png").exists() else BASE_DIR
LOGO_PATH = ASSET_DIR / "logo.png"
ICON_PATH = ASSET_DIR / "icon.png"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_PATH = AUTOSTART_DIR / "GitMo.desktop"
FONT_FAMILY = "TkDefaultFont"
BASE_BODY_SIZE = 12
BASE_TITLE_SIZE = 24
MIN_FONT_DELTA = -4
MAX_FONT_DELTA = 8
SYNC_SCHEDULE_OPTIONS = {
    "After 1 minute idle (Recommended)": "idle-1m",
    "Every 5 minutes while changing": "interval-5m",
    "Every 10 minutes while changing": "interval-10m",
    "Every 30 minutes while changing": "interval-30m",
    "Manual commits only (Run Sync Now)": "manual",
}
COMMIT_MESSAGE_OPTIONS = {
    "Changed-file summary (Recommended)": "summary",
    "GitMo autosave": "standard",
    "GitMo autosave with date and time": "datetime",
}

THEME = {
    "bg": "#f6f8fa",
    "card": "#ffffff",
    "panel": "#ffffff",
    "panel_alt": "#f6f8fa",
    "text": "#24292f",
    "muted": "#57606a",
    "line": "#d0d7de",
    "accent": "#0969da",
    "accent_hover": "#0757b6",
    "accent_text": "#ffffff",
    "button": "#f6f8fa",
    "button_hover": "#eef2f6",
    "button_text": "#24292f",
    "danger_button": "#ffebe9",
    "danger_hover": "#ffd8d3",
    "danger_text": "#cf222e",
    "success": "#1a7f37",
    "sync": "#0969da",
    "warning": "#9a6700",
    "danger": "#cf222e",
}


@dataclass
class RepoSelection:
    name: str
    local_path: Path
    exists_local: bool
    exists_remote: bool
    local_is_repo: bool
    source: str
    clone_url: str | None = None


class LocalValue:
    def __init__(self, value) -> None:
        self.value = value
        self.callbacks: list = []

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value
        for callback in tuple(self.callbacks):
            callback()

    def trace_add(self, _mode: str, callback):
        self.callbacks.append(callback)
        return callback


def tail_text_lines(path: Path, count: int, block_size: int = 8192) -> list[str]:
    if count <= 0 or not path.exists():
        return []
    with path.open("rb") as log_file:
        log_file.seek(0, 2)
        position = log_file.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count:
            read_size = min(block_size, position)
            position -= read_size
            log_file.seek(position)
            data = log_file.read(read_size) + data
    return data.decode("utf-8", errors="replace").splitlines()[-count:]


def repo_catalog_sort_key(selection: RepoSelection) -> tuple[int, str, str]:
    if selection.exists_remote and selection.exists_local:
        group = 0
    elif selection.exists_remote:
        group = 1
    elif selection.exists_local:
        group = 2
    else:
        group = 3
    return group, selection.name.casefold(), selection.name


def repo_targets_changed(
    selection: RepoSelection,
    *,
    wants_github: bool,
    wants_local: bool,
) -> bool:
    return (
        wants_github != selection.exists_remote
        or wants_local != selection.exists_local
    )


def sync_button_presentation(sync_running: bool) -> tuple[str, str, str]:
    if sync_running:
        return "■", "Stop Sync", THEME["warning"]
    return "▶", "Start Sync", THEME["success"]


def repo_settings_state(
    *,
    github: bool,
    local: bool,
    enabled: bool,
    sync_mode: str,
    sync_schedule: str,
    commit_message_mode: str,
    local_path: str,
) -> tuple[bool, bool, bool, str, str, str, str]:
    return (
        github,
        local,
        enabled,
        sync_mode,
        sync_schedule,
        commit_message_mode,
        local_path,
    )


def repo_description(repo_path: Path, repo_name: str) -> str:
    if repo_path.is_dir():
        readmes = sorted(
            (
                path
                for path in repo_path.iterdir()
                if path.is_file() and path.name.casefold().startswith("readme")
            ),
            key=lambda path: (path.name.casefold() != "readme.md", path.name.casefold()),
        )
        for readme in readmes:
            try:
                description = _first_readme_sentence(readme.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if description:
                return description[:350].rstrip()

    title = repo_name.replace("-", " ").replace("_", " ").strip() or repo_name
    return f"Project files for {title}."[:350]


def _first_readme_sentence(text: str) -> str:
    paragraph: list[str] = []
    in_code_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line:
            if paragraph:
                break
            continue
        if (
            line.startswith(("#", "!", "<", ">", "-", "*", "+"))
            or re.match(r"^\d+[.)]\s", line)
            or "|" in line
        ):
            continue
        paragraph.append(line)

    prose = " ".join(paragraph)
    prose = re.sub(r"!\[[^\]]*]\([^)]*\)", "", prose)
    prose = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", prose)
    prose = re.sub(r"[`*_~]+", "", prose)
    prose = re.sub(r"\s+", " ", prose).strip()
    if not prose:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", prose, maxsplit=1)[0]
    return sentence.strip()


class TrayMenuActivationTracker:
    def __init__(self, double_click_seconds: float = 0.6) -> None:
        self.double_click_seconds = double_click_seconds
        self.opened_at: float | None = None
        self.menu_action_selected = False

    def menu_opened(self, opened_at: float) -> None:
        self.opened_at = opened_at
        self.menu_action_selected = False

    def menu_action_started(self) -> None:
        self.menu_action_selected = True

    def menu_closed(self, closed_at: float) -> bool:
        opened_at = self.opened_at
        should_restore = (
            opened_at is not None
            and not self.menu_action_selected
            and 0 <= closed_at - opened_at <= self.double_click_seconds
        )
        self.opened_at = None
        self.menu_action_selected = False
        return should_restore


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent,
        text: str,
        command,
        *,
        variant: str = "secondary",
        width: int | None = None,
        font_size_delta: int = 0,
        canvas_bg: str | None = None,
    ) -> None:
        self.command = command
        self.variant = variant
        self.enabled = True
        body_size = BASE_BODY_SIZE + font_size_delta
        if variant == "primary":
            self.bg_color = THEME["accent"]
            self.hover_color = THEME["accent_hover"]
            self.text_color = THEME["accent_text"]
            self.border_color = THEME["accent"]
        elif variant == "danger":
            self.bg_color = THEME["danger_button"]
            self.hover_color = THEME["danger_hover"]
            self.text_color = THEME["danger_text"]
            self.border_color = THEME["danger_hover"]
        else:
            self.bg_color = THEME["button"]
            self.hover_color = THEME["button_hover"]
            self.text_color = THEME["button_text"]
        self.border_color = THEME["line"]
        self.disabled_bg_color = THEME["button"]
        self.disabled_text_color = THEME["muted"]
        self.disabled_border_color = THEME["line"]
        self.button_font = (FONT_FAMILY, body_size, "bold")
        self.icon_font = (FONT_FAMILY, round(body_size * 1.25), "bold")
        self.radius = max(14, body_size + 4)
        self.button_width = width or max(128, len(text) * (body_size - 2) + 42)
        self.button_height = body_size + 28
        super().__init__(
            parent,
            width=self.button_width,
            height=self.button_height,
            highlightthickness=0,
            bd=0,
            bg=canvas_bg or THEME["bg"],
            cursor="hand2",
        )
        self._text = text
        self._draw(self.bg_color)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Return>", self._on_click)
        self.bind("<Enter>", lambda _event: self._draw(self.hover_color if self.enabled else self.disabled_bg_color))
        self.bind("<Leave>", lambda _event: self._draw(self.bg_color if self.enabled else self.disabled_bg_color))
        self.configure(takefocus=True)

    def _draw(self, fill: str) -> None:
        self.delete("all")
        text_color = self.text_color if self.enabled else self.disabled_text_color
        border_color = self.border_color if self.enabled else self.disabled_border_color
        self.create_rounded_rect(
            1,
            1,
            self.button_width - 1,
            self.button_height - 1,
            self.radius,
            fill=fill,
            outline=border_color,
        )
        if self._has_leading_icon():
            icon, label = self._text.split(" ", 1)
            center = self.button_width // 2
            icon_x = center - max(18, len(label) * 3)
            self.create_text(
                icon_x,
                self.button_height // 2,
                text=icon,
                fill=text_color,
                font=self.icon_font,
            )
            self.create_text(
                icon_x + 18,
                self.button_height // 2,
                text=label,
                fill=text_color,
                font=self.button_font,
                anchor="w",
            )
        else:
            self.create_text(
                self.button_width // 2,
                self.button_height // 2,
                text=self._text,
                fill=text_color,
                font=self.button_font,
            )

    def _has_leading_icon(self) -> bool:
        return " " in self._text and not self._text[0].isascii()

    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs) -> int:
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        return self.create_polygon(points, smooth=True, splinesteps=18, **kwargs)

    def _on_click(self, _event=None) -> None:
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw(self.bg_color if enabled else self.disabled_bg_color)


class FolderPickerDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        *,
        title: str,
        initial_path: Path,
        must_exist: bool,
        font_size_delta: int = 0,
    ) -> None:
        super().__init__(parent)
        self.dialog_title = title
        self.title(title)
        self.configure(bg=THEME["bg"])
        self.geometry("760x520")
        self.minsize(620, 420)
        self.result: Path | None = None
        self.must_exist = must_exist
        self.body_size = BASE_BODY_SIZE + font_size_delta
        self.current_path = self._initial_directory(initial_path)
        self.path_var = tk.StringVar(value=str(self.current_path))
        self.status_var = tk.StringVar(value="")
        self._build_ui()
        self._load_directory(self.current_path)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _event: self._select_current())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.transient(parent)
        self.update_idletasks()
        self._center_over(parent)
        self.lift(parent)
        self.focus_force()
        self.grab_set()

    def _center_over(self, parent: tk.Tk) -> None:
        parent.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _initial_directory(self, path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.exists() and expanded.is_dir():
            return expanded
        parent = expanded.parent
        if parent.exists() and parent.is_dir():
            return parent
        return Path.home()

    def _build_ui(self) -> None:
        shell = tk.Frame(self, bg=THEME["bg"], padx=18, pady=18)
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=THEME["bg"])
        header.pack(fill="x", pady=(0, 12))
        tk.Label(
            header,
            text=self.dialog_title,
            bg=THEME["bg"],
            fg=THEME["text"],
            font=(FONT_FAMILY, self.body_size + 6, "bold"),
            anchor="w",
        ).pack(side="left")

        path_row = tk.Frame(shell, bg=THEME["bg"])
        path_row.pack(fill="x", pady=(0, 10))
        ttk.Entry(path_row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        self._dialog_button(path_row, "Go", self._go_to_entry, width=72).pack(side="left", padx=(8, 0))

        nav = tk.Frame(shell, bg=THEME["bg"])
        nav.pack(fill="x", pady=(0, 10))
        self._dialog_button(nav, "Home", lambda: self._load_directory(Path.home()), width=82).pack(side="left")
        self._dialog_button(nav, "Up", self._go_up, width=70).pack(side="left", padx=(8, 0))
        self._dialog_button(nav, "New Folder", self._create_folder, width=116).pack(side="left", padx=(8, 0))

        list_card = tk.Frame(
            shell,
            bg=THEME["card"],
            highlightbackground=THEME["line"],
            highlightcolor=THEME["line"],
            highlightthickness=1,
            bd=0,
        )
        list_card.pack(fill="both", expand=True)
        self.folder_list = tk.Listbox(
            list_card,
            bg=THEME["card"],
            fg=THEME["text"],
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT_FAMILY, self.body_size),
            selectbackground=THEME["accent"],
            selectforeground=THEME["accent_text"],
        )
        scrollbar = ttk.Scrollbar(list_card, orient="vertical", command=self.folder_list.yview)
        self.folder_list.configure(yscrollcommand=scrollbar.set)
        self.folder_list.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        scrollbar.pack(side="right", fill="y", pady=8)
        self.folder_list.bind("<<ListboxSelect>>", lambda _event: self._select_highlighted_folder())
        self.folder_list.bind("<Double-Button-1>", lambda _event: self._open_selected())

        footer = tk.Frame(shell, bg=THEME["bg"])
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=THEME["bg"],
            fg=THEME["danger"],
            anchor="w",
            font=(FONT_FAMILY, self.body_size),
        ).pack(side="left", fill="x", expand=True)
        self._dialog_button(footer, "Cancel", self._cancel, width=96).pack(side="right")
        self._dialog_button(footer, "Select", self._select_current, width=104, variant="primary").pack(side="right", padx=(0, 8))

    def _dialog_button(
        self,
        parent,
        text: str,
        command,
        *,
        width: int,
        variant: str = "secondary",
    ) -> RoundedButton:
        return RoundedButton(
            parent,
            text,
            command,
            variant=variant,
            width=width,
            font_size_delta=self.body_size - BASE_BODY_SIZE,
            canvas_bg=THEME["bg"],
        )

    def _load_directory(self, path: Path) -> None:
        expanded = path.expanduser()
        if not expanded.exists() or not expanded.is_dir():
            self.status_var.set(f"Folder does not exist: {expanded}")
            return
        try:
            folders = sorted(
                [child for child in expanded.iterdir() if child.is_dir()],
                key=lambda item: item.name.lower(),
            )
        except OSError as exc:
            self.status_var.set(str(exc))
            return
        self.current_path = expanded
        self.path_var.set(str(expanded))
        self.status_var.set("")
        self.folder_list.delete(0, "end")
        if folders:
            for folder in folders:
                self.folder_list.insert("end", folder.name)
        else:
            self.folder_list.insert("end", "(No folders here)")

    def _selected_child(self) -> Path | None:
        selected = self.folder_list.curselection()
        if not selected:
            return None
        name = self.folder_list.get(selected[0])
        if name == "(No folders here)":
            return None
        return self.current_path / name

    def _select_highlighted_folder(self) -> None:
        child = self._selected_child()
        if child:
            self.path_var.set(str(child))

    def _open_selected(self) -> None:
        child = self._selected_child()
        if child:
            self._load_directory(child)

    def _go_up(self) -> None:
        parent = self.current_path.parent
        if parent != self.current_path:
            self._load_directory(parent)

    def _go_to_entry(self) -> None:
        path = Path(self.path_var.get()).expanduser()
        if path.exists():
            self._load_directory(path)
            return
        if self.must_exist:
            self.status_var.set("Folder must exist.")
            return
        parent = path.parent
        if parent.exists() and parent.is_dir():
            self.current_path = path
            self.status_var.set("Folder will be created when selected.")
        else:
            self.status_var.set("Parent folder does not exist.")

    def _create_folder(self) -> None:
        name = simpledialog.askstring("New Folder", "Folder name:", parent=self)
        if not name:
            return
        path = self.current_path / name.strip()
        try:
            path.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            self.status_var.set(str(exc))
            return
        self._load_directory(path)

    def _select_current(self) -> None:
        path = Path(self.path_var.get()).expanduser()
        if not path.exists():
            if self.must_exist:
                self.status_var.set("Folder must exist.")
                return
            parent = path.parent
            if not parent.exists() or not parent.is_dir():
                self.status_var.set("Parent folder does not exist.")
                return
        elif not path.is_dir():
            self.status_var.set("Selected path is not a folder.")
            return
        self.result = path
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class GitMoApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GitMo")
        self.root.geometry("900x840")
        self.root.minsize(900, 620)
        self.root.configure(bg=THEME["bg"])
        self.config = load_config()
        self.github_client: GitHubClient | None = None
        self.remote_repos = self._cached_remote_repos()
        self.remote_repos_loaded = False
        self.remote_repos_loading = False
        self.remote_repos_error = ""
        self.refresh_repos_screen_after_load = False
        self.remote_repos_queue: queue.Queue[tuple[list[GitHubRepo], str, bool]] = queue.Queue()
        self.tray_command_queue: queue.Queue[str] = queue.Queue()
        self.tray_action_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.tray_thread_started = False
        self.tray_unavailable = False
        self.repo_catalog: list[RepoSelection] = []
        self.manual_folder_paths: dict[str, Path] = {}
        self.pending_added_folder_names: set[str] = set()
        self.log_lock = threading.Lock()
        self.sync_engine = SyncEngine(self.config, self._enqueue_log)
        self.logo_image: tk.PhotoImage | None = None
        self.header_logo_image: tk.PhotoImage | None = None
        self.icon_image: tk.PhotoImage | None = None
        self._load_images()
        self.shell = ttk.Frame(root)
        self.shell.pack(fill="both", expand=True)
        self.content = ttk.Frame(self.shell, padding=22)
        self.content_host = self.content
        self.content.pack(fill="both", expand=True)
        self.repo_rows: dict[str, dict[str, LocalValue]] = {}
        self.repo_manager_status_var: tk.StringVar | None = None
        self.repo_manager_status_label: tk.Widget | None = None
        self.page_frames: dict[str, ttk.Frame] = {}
        self.status_vars: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, tk.Label] = {}
        self.last_sync_vars: dict[str, tk.StringVar] = {}
        self.header_last_sync_var = tk.StringVar(value="Last Sync: Not yet")
        self.header_git_var = tk.StringVar(value="Git: OK")
        self.dashboard_watch_var = tk.StringVar(value="Watching")
        self.dashboard_summary_var = tk.StringVar(value="")
        self.dashboard_watch_label: tk.Label | None = None
        self.sync_toolbar_buttons: list[tk.Frame] = []
        self.last_checked_var = tk.StringVar(value="")
        self.status_refresh_after_id: str | None = None
        self.log_text: tk.Text | None = None
        self.dashboard_tree: ttk.Treeview | None = None
        self.fixed_action_bar: tk.Frame | None = None
        self.current_screen = ""
        self.sync_should_run = True
        self.is_paused = False
        self.autostart_var = tk.BooleanVar(value=self._autostart_enabled())
        self._build_style()
        self._build_menu()
        self.root.after(250, self._poll_tray_actions)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self) -> None:
        if self.config.github_token and self.config.gitmo_path:
            self._try_resume()
        else:
            self.show_login_screen()

    def _build_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        body_size = BASE_BODY_SIZE + self.config.font_size_delta
        title_size = BASE_TITLE_SIZE + self.config.font_size_delta
        body_font = (FONT_FAMILY, body_size)
        title_font = (FONT_FAMILY, title_size, "bold")
        subtitle_font = (FONT_FAMILY, body_size)
        header_font = (FONT_FAMILY, body_size, "bold")

        style.configure(".", background=THEME["bg"], foreground=THEME["text"], font=body_font)
        style.configure("TFrame", background=THEME["bg"])
        style.configure("Panel.TFrame", background=THEME["panel"])
        style.configure("TLabel", background=THEME["bg"], foreground=THEME["text"])
        style.configure("Card.TFrame", background=THEME["card"])
        style.configure("Card.TLabel", background=THEME["card"], foreground=THEME["text"])
        style.configure("CardMuted.TLabel", background=THEME["card"], foreground=THEME["muted"])
        style.configure("CardHeader.TLabel", background=THEME["card"], foreground=THEME["text"], font=header_font)
        style.configure(
            "Title.TLabel",
            background=THEME["bg"],
            foreground=THEME["text"],
            font=title_font,
        )
        style.configure(
            "Subtitle.TLabel",
            background=THEME["bg"],
            foreground=THEME["muted"],
            font=subtitle_font,
        )
        style.configure(
            "Header.TLabel",
            background=THEME["bg"],
            foreground=THEME["muted"],
            font=header_font,
        )
        style.configure("Error.TLabel", background=THEME["bg"], foreground=THEME["danger"])
        style.configure(
            "TEntry",
            fieldbackground=THEME["card"],
            foreground=THEME["text"],
            insertcolor=THEME["text"],
            bordercolor=THEME["line"],
            lightcolor=THEME["line"],
            darkcolor=THEME["line"],
        )
        style.configure(
            "TCombobox",
            fieldbackground=THEME["card"],
            background=THEME["card"],
            foreground=THEME["text"],
            arrowcolor=THEME["text"],
            bordercolor=THEME["line"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", THEME["card"])],
            foreground=[("readonly", THEME["text"])],
        )
        style.configure("TCheckbutton", background=THEME["bg"], foreground=THEME["text"])
        style.map("TCheckbutton", background=[("active", THEME["bg"])], foreground=[("active", THEME["text"])])
        style.configure("Card.TCheckbutton", background=THEME["card"], foreground=THEME["text"])
        style.map("Card.TCheckbutton", background=[("active", THEME["card"])], foreground=[("active", THEME["text"])])
        style.configure(
            "Treeview",
            background=THEME["card"],
            fieldbackground=THEME["card"],
            foreground=THEME["text"],
            bordercolor=THEME["line"],
            rowheight=body_size + 14,
        )
        style.configure(
            "Treeview.Heading",
            background=THEME["panel_alt"],
            foreground=THEME["muted"],
            font=header_font,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=THEME["button"],
            troughcolor=THEME["card"],
            arrowcolor=THEME["text"],
            bordercolor=THEME["line"],
        )

    def _load_images(self) -> None:
        if ICON_PATH.exists():
            self.icon_image = tk.PhotoImage(file=str(ICON_PATH)).subsample(8, 8)
            self.root.iconphoto(True, self.icon_image)
        if LOGO_PATH.exists():
            self.logo_image = tk.PhotoImage(file=str(LOGO_PATH)).subsample(5, 5)
            self.header_logo_image = tk.PhotoImage(file=str(LOGO_PATH)).subsample(10, 10)

    def _button(
        self,
        parent,
        text: str,
        command,
        *,
        variant: str = "secondary",
        width: int | None = None,
        canvas_bg: str | None = None,
    ) -> RoundedButton:
        button = RoundedButton(
            parent,
            text,
            command,
            variant=variant,
            width=width,
            font_size_delta=self.config.font_size_delta,
            canvas_bg=canvas_bg,
        )
        return button

    def _ribbon_button(
        self,
        parent: tk.Frame,
        icon: str,
        label: str,
        command,
        *,
        icon_color: str | None = None,
    ) -> tk.Frame:
        toolbar_bg = "#f0f2f4"
        item = tk.Frame(
            parent,
            bg=toolbar_bg,
            cursor="hand2",
            padx=10,
            pady=7,
        )
        icon_label = None
        if icon:
            icon_label = tk.Label(
                item,
                text=icon,
                bg=toolbar_bg,
                fg=icon_color or THEME["accent"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta + 3, "bold"),
                cursor="hand2",
            )
            icon_label.pack(side="left", padx=(0, 7))
        text_label = None
        if label:
            text_label = tk.Label(
                item,
                text=label,
                bg=toolbar_bg,
                fg=THEME["text"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
                cursor="hand2",
            )
            text_label.pack(side="left")

        def on_enter(_event=None) -> None:
            item.configure(bg=THEME["button_hover"])
            if icon_label:
                icon_label.configure(bg=THEME["button_hover"])
            if text_label:
                text_label.configure(bg=THEME["button_hover"])

        def on_leave(_event=None) -> None:
            item.configure(bg=toolbar_bg)
            if icon_label:
                icon_label.configure(bg=toolbar_bg)
            if text_label:
                text_label.configure(bg=toolbar_bg)

        def on_click(_event=None) -> None:
            if command:
                command()

        widgets = [item]
        if icon_label:
            widgets.append(icon_label)
        if text_label:
            widgets.append(text_label)
        for widget in widgets:
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            widget.bind("<Button-1>", on_click)
        item.icon_label = icon_label  # type: ignore[attr-defined]
        item.text_label = text_label  # type: ignore[attr-defined]
        return item

    def _ribbon_separator(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=THEME["line"], width=1).pack(
            side="left",
            fill="y",
            padx=5,
            pady=6,
        )

    def _card(self, parent, *, padding: int = 16) -> tk.Frame:
        outer = tk.Frame(
            parent,
            bg=THEME["card"],
            highlightbackground=THEME["line"],
            highlightcolor=THEME["line"],
            highlightthickness=1,
            bd=0,
        )
        inner = tk.Frame(outer, bg=THEME["card"], padx=padding, pady=padding)
        inner.pack(fill="both", expand=True)
        outer.inner = inner  # type: ignore[attr-defined]
        return outer

    def _card_label(self, parent, text: str, *, muted: bool = False, bold: bool = False, **pack_options) -> tk.Label:
        body_size = BASE_BODY_SIZE + self.config.font_size_delta
        font = (FONT_FAMILY, body_size, "bold") if bold else (FONT_FAMILY, body_size)
        label = tk.Label(
            parent,
            text=text,
            bg=THEME["card"],
            fg=THEME["muted"] if muted else THEME["text"],
            font=font,
            anchor="w",
        )
        label.pack(**pack_options)
        return label

    def _scrollable_area(self, parent) -> tk.Frame:
        shell = tk.Frame(parent, bg=THEME["card"])
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, bg=THEME["card"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=THEME["card"])
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<Enter>", lambda _event: self._bind_nested_mousewheel(canvas))
        inner.bind("<Enter>", lambda _event: self._bind_nested_mousewheel(canvas))
        canvas.bind("<Leave>", lambda _event: self._bind_outer_mousewheel())
        shell.bind("<Leave>", lambda _event: self._bind_outer_mousewheel())
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return inner

    def _page_header(self, title: str, subtitle: str, *, show_logo: bool = True) -> None:
        header = ttk.Frame(self.content)
        header.pack(fill="x", pady=(0, 22))
        if show_logo and self.header_logo_image:
            ttk.Label(header, image=self.header_logo_image).pack(side="left", padx=(0, 14))
        text_frame = ttk.Frame(header)
        text_frame.pack(side="left", fill="x", expand=True)
        ttk.Label(text_frame, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(text_frame, text=subtitle, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Run Sync Now", accelerator="Ctrl+R", command=self._manual_sync)
        file_menu.add_command(label="View Logs", command=self._view_logs)
        file_menu.add_command(label="Manage Repos", command=self._open_repo_manager)
        file_menu.add_command(label="Add Local Folder", command=self._add_outside_folder_selection)
        file_menu.add_command(label="Choose GitMo Folder", command=self.show_gitmo_screen)
        file_menu.add_command(label="View GitHub", command=self._open_github_profile)
        file_menu.add_separator()
        file_menu.add_command(label="Run in Background", command=self._run_in_background)
        file_menu.add_checkbutton(
            label="Start with boot",
            variable=self.autostart_var,
            command=self._toggle_autostart,
        )
        file_menu.add_separator()
        file_menu.add_command(label="Delete all settings and restart", command=self._delete_all_settings_and_restart)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Ctrl+Q", command=self._on_close)
        menu_bar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menu_bar, tearoff=False)
        edit_menu.add_command(
            label="Increase Text Size ++",
            accelerator="Ctrl++",
            command=self._increase_text_size,
        )
        edit_menu.add_command(
            label="Decrease Text Size --",
            accelerator="Ctrl+-",
            command=self._decrease_text_size,
        )
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy Log Path", accelerator="Ctrl+L", command=self._copy_log_path)
        edit_menu.add_command(label="Clear Log File", command=self._clear_log_file)
        menu_bar.add_cascade(label="Edit", menu=edit_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="About GitMo", command=self._show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menu_bar)
        self.root.bind_all("<Control-r>", lambda _event: self._manual_sync())
        self.root.bind_all("<Control-q>", lambda _event: self._on_close())
        self.root.bind_all("<Control-l>", lambda _event: self._copy_log_path())
        self.root.bind_all("<Control-plus>", lambda _event: self._increase_text_size())
        self.root.bind_all("<Control-equal>", lambda _event: self._increase_text_size())
        self.root.bind_all("<Control-minus>", lambda _event: self._decrease_text_size())

    def _clear(self) -> None:
        if self.status_refresh_after_id is not None:
            self.root.after_cancel(self.status_refresh_after_id)
            self.status_refresh_after_id = None
        if self.fixed_action_bar is not None:
            self.fixed_action_bar.destroy()
            self.fixed_action_bar = None
        for child in self.content.winfo_children():
            child.destroy()
        self.log_text = None

    def _prepare_page(self, name: str, *, reuse: bool) -> bool:
        if self.status_refresh_after_id is not None:
            self.root.after_cancel(self.status_refresh_after_id)
            self.status_refresh_after_id = None
        if self.fixed_action_bar is not None:
            self.fixed_action_bar.place_forget()
        for frame in self.page_frames.values():
            frame.pack_forget()

        existing = self.page_frames.get(name)
        if reuse and existing is not None and existing.winfo_exists():
            self.content = existing
            existing.pack(fill="both", expand=True)
            if name == "repos" and self.fixed_action_bar is not None:
                self.fixed_action_bar.place(relx=0, rely=1, relwidth=1, anchor="sw")
                self.fixed_action_bar.lift()
            return False

        if existing is not None and existing.winfo_exists():
            existing.destroy()
        frame = ttk.Frame(self.content_host)
        frame.pack(fill="both", expand=True)
        self.page_frames[name] = frame
        self.content = frame
        return True

    def _discard_page(self, name: str) -> None:
        frame = self.page_frames.pop(name, None)
        if frame is not None and frame.winfo_exists():
            frame.destroy()
        if name == "dashboard":
            self.dashboard_tree = None
            self.log_text = None
            self.dashboard_watch_label = None

    def _fixed_bottom_bar(self) -> tk.Frame:
        if self.fixed_action_bar is not None:
            self.fixed_action_bar.destroy()
        self.fixed_action_bar = tk.Frame(
            self.root,
            bg=THEME["card"],
            highlightbackground=THEME["line"],
            highlightcolor=THEME["line"],
            highlightthickness=1,
            bd=0,
            padx=16,
            pady=10,
        )
        self.fixed_action_bar.place(relx=0, rely=1, relwidth=1, anchor="sw")
        self.fixed_action_bar.lift()
        return self.fixed_action_bar

    def _update_content_scroll_region(self, _event=None) -> None:
        return

    def _resize_content_window(self, event) -> None:
        return

    def _on_mousewheel(self, event) -> None:
        return

    def _bind_outer_mousewheel(self) -> None:
        return

    def _bind_nested_mousewheel(self, canvas: tk.Canvas) -> None:
        def scroll_nested(event) -> str:
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            elif event.delta:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        canvas.bind_all("<MouseWheel>", scroll_nested)
        canvas.bind_all("<Button-4>", scroll_nested)
        canvas.bind_all("<Button-5>", scroll_nested)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.tray_thread_started:
            self.tray_command_queue.put("hide")

    def _has_enabled_repos(self) -> bool:
        return any(repo.enabled for repo in self.config.repos.values())

    def _run_in_background(self) -> None:
        if self._has_enabled_repos():
            self._ensure_sync_engine_running()
        if self.tray_unavailable:
            self.root.iconify()
        else:
            self.root.withdraw()
        if self._ensure_tray_icon():
            self.tray_command_queue.put("show")
        self._notify_background_running()

    def _ensure_tray_icon(self) -> bool:
        # Tray support varies across Linux desktops. This old GTK fallback is
        # optional; GitMo must keep working if the desktop ignores or rejects it.
        if self.tray_unavailable:
            return False
        if self.tray_thread_started:
            return True
        self.tray_thread_started = True

        def tray_thread() -> None:
            try:
                import gi  # type: ignore

                gi.require_version("Gtk", "3.0")
                from gi.repository import GLib, Gtk  # type: ignore

                indicator = None
                indicator_status = None
                status_icon = None

                for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
                    try:
                        gi.require_version(namespace, "0.1")
                        indicator_module = __import__(
                            "gi.repository",
                            fromlist=[namespace],
                        )
                        app_indicator = getattr(indicator_module, namespace)
                        indicator = app_indicator.Indicator.new(
                            "gitmo",
                            self._indicator_icon_name(),
                            app_indicator.IndicatorCategory.APPLICATION_STATUS,
                        )
                        if ICON_PATH.exists() and hasattr(indicator, "set_icon_theme_path"):
                            indicator.set_icon_theme_path(str(ICON_PATH.parent))
                        if ICON_PATH.exists() and hasattr(indicator, "set_icon_full"):
                            indicator.set_icon_full(self._indicator_icon_name(), "GitMo")
                        indicator_status = app_indicator.IndicatorStatus
                        indicator.set_status(indicator_status.PASSIVE)
                        break
                    except (ValueError, ImportError, AttributeError):
                        indicator = None
                        indicator_status = None

                if indicator is None:
                    if ICON_PATH.exists():
                        status_icon = Gtk.StatusIcon.new_from_file(str(ICON_PATH))
                    else:
                        status_icon = Gtk.StatusIcon.new_from_icon_name("folder-sync")
                    status_icon.set_tooltip_text("GitMo is running in the background")
                    status_icon.set_visible(False)

                menu = Gtk.Menu()
                show_item = Gtk.MenuItem(label="Show GitMo")
                quit_item = Gtk.MenuItem(label="Quit GitMo")
                menu.append(show_item)
                menu.append(quit_item)
                menu.show_all()
                menu_activation = TrayMenuActivationTracker()

                def show_app(_item=None) -> None:
                    menu_activation.menu_action_started()
                    self.tray_action_queue.put(("show", ""))

                def quit_app(_item=None) -> None:
                    menu_activation.menu_action_started()
                    self.tray_action_queue.put(("quit", ""))

                def popup_menu(_icon, button, activate_time) -> None:
                    menu.popup(None, None, None, None, button, activate_time)

                def menu_opened(_menu) -> None:
                    menu_activation.menu_opened(time.monotonic())

                def menu_closed(_menu) -> None:
                    closed_at = time.monotonic()

                    def finish_menu_close() -> bool:
                        if menu_activation.menu_closed(closed_at):
                            self.tray_action_queue.put(("show", ""))
                        return False

                    GLib.idle_add(finish_menu_close)

                def process_commands() -> bool:
                    while True:
                        try:
                            command = self.tray_command_queue.get_nowait()
                        except queue.Empty:
                            break
                        if command == "show":
                            if indicator is not None and indicator_status is not None:
                                indicator.set_status(indicator_status.ACTIVE)
                            elif status_icon is not None:
                                status_icon.set_visible(True)
                        elif command == "hide":
                            if indicator is not None and indicator_status is not None:
                                indicator.set_status(indicator_status.PASSIVE)
                            elif status_icon is not None:
                                status_icon.set_visible(False)
                        elif command == "quit":
                            Gtk.main_quit()
                            return False
                    return True

                if indicator is not None:
                    indicator.set_menu(menu)
                    if hasattr(indicator, "set_secondary_activate_target"):
                        indicator.set_secondary_activate_target(show_item)
                    self.tray_action_queue.put(("ready", "AppIndicator"))
                elif status_icon is not None:
                    status_icon.connect("activate", show_app)
                    status_icon.connect("popup-menu", popup_menu)
                    self.tray_action_queue.put(("ready", "GtkStatusIcon"))
                menu.connect("map", menu_opened)
                menu.connect("deactivate", menu_closed)
                show_item.connect("activate", show_app)
                quit_item.connect("activate", quit_app)
                GLib.timeout_add(200, process_commands)
                Gtk.main()
            except Exception as exc:  # pragma: no cover - depends on desktop tray support
                self.tray_action_queue.put(("error", str(exc)))

        threading.Thread(target=tray_thread, daemon=True).start()
        return True

    def _poll_tray_actions(self) -> None:
        while True:
            try:
                action, detail = self.tray_action_queue.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                self._show_window()
            elif action == "quit":
                self.sync_engine.stop()
                self.tray_command_queue.put("quit")
                self.root.destroy()
                return
            elif action == "error":
                self.tray_unavailable = True
                self.tray_thread_started = False
                self._enqueue_log("tray", f"Tray icon unavailable: {detail}")
                if self.root.state() == "withdrawn":
                    self.root.deiconify()
                    self.root.iconify()
            elif action == "ready":
                self._enqueue_log("tray", f"Tray icon ready: {detail}")
        if self.root.winfo_exists():
            self.root.after(250, self._poll_tray_actions)

    def _notify_background_running(self) -> None:
        if not self._has_enabled_repos():
            message = "GitMo is running in the background. No repos are enabled for sync."
        elif self.sync_should_run:
            message = "GitMo is still running in the background and watching enabled repos."
        else:
            message = "GitMo is running in the background. Sync is paused."
        notify_send = shutil.which("notify-send")
        if not notify_send:
            self._enqueue_log("app", message)
            return
        try:
            subprocess.run(
                [
                    notify_send,
                    "--app-name=GitMo",
                    "GitMo is running",
                    message,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            self._enqueue_log("app", message)

    def _autostart_enabled(self) -> bool:
        return AUTOSTART_PATH.exists()

    def _toggle_autostart(self) -> None:
        try:
            if self.autostart_var.get():
                self._enable_autostart()
                self._enqueue_log("app", "Enabled start with boot.")
            else:
                self._disable_autostart()
                self._enqueue_log("app", "Disabled start with boot.")
        except OSError as exc:
            self.autostart_var.set(self._autostart_enabled())
            messagebox.showerror("GitMo", f"Could not update start with boot setting:\n{exc}")

    def _enable_autostart(self) -> None:
        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSTART_PATH.write_text(self._autostart_desktop_entry(), encoding="utf-8")

    def _disable_autostart(self) -> None:
        if AUTOSTART_PATH.exists():
            AUTOSTART_PATH.unlink()

    def _delete_all_settings_and_restart(self) -> None:
        if not self._confirm_destructive_action(
            "Delete all settings",
            "This deletes the saved GitHub token, GitMo folder, selected repos, UI settings, and start-with-boot setting.\n\n"
            "Local folders and GitHub repositories are not deleted.",
        ):
            return

        self.sync_should_run = False
        self.is_paused = True
        self.sync_engine.stop()
        if self.tray_thread_started:
            self.tray_command_queue.put("hide")

        for path in (CONFIG_PATH, CREDENTIALS_PATH, LEGACY_CONFIG_PATH):
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                messagebox.showerror("GitMo", f"Could not delete settings file:\n{path}\n\n{exc}")
                return

        try:
            self._disable_autostart()
        except OSError as exc:
            messagebox.showerror("GitMo", f"Could not disable start with boot:\n{exc}")
            return

        self.config = AppConfig()
        self.github_client = None
        self.remote_repos = {}
        self.remote_repos_loaded = False
        self.remote_repos_loading = False
        self.remote_repos_error = ""
        self.refresh_repos_screen_after_load = False
        self.repo_catalog = []
        self.manual_folder_paths = {}
        self.pending_added_folder_names.clear()
        self.repo_rows = {}
        self.status_vars = {}
        self.status_labels = {}
        self.last_sync_vars = {}
        self.autostart_var.set(False)
        self.sync_engine = SyncEngine(self.config, self._enqueue_log)
        self._enqueue_log("app", "Deleted all settings and restarted setup.")
        self.show_login_screen()

    def _autostart_desktop_entry(self) -> str:
        return "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                "Name=GitMo",
                "Comment=Sync GitMo folders with GitHub",
                f"Exec={self._desktop_exec_value(self._app_command())} --background",
                f"Icon={self._desktop_exec_value(self._icon_value())}",
                "Terminal=false",
                "Categories=Development;RevisionControl;",
                "StartupNotify=false",
                "X-GNOME-Autostart-enabled=true",
                "",
            ]
        )

    def _app_command(self) -> str:
        source_launcher = BASE_DIR / "run-gitmo.sh"
        if source_launcher.exists():
            return str(source_launcher)
        installed_command = shutil.which("gitmo")
        if installed_command:
            return installed_command
        return "gitmo"

    def _icon_value(self) -> str:
        if ASSET_DIR == SYSTEM_ASSET_DIR:
            return "gitmo"
        return str(ICON_PATH)

    def _indicator_icon_name(self) -> str:
        if ASSET_DIR == SYSTEM_ASSET_DIR:
            return "gitmo"
        return ICON_PATH.stem if ICON_PATH.exists() else "folder-sync"

    def _desktop_exec_value(self, value: str | Path) -> str:
        text = str(value)
        if not any(char.isspace() for char in text):
            return text
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _increase_text_size(self) -> None:
        self._change_text_size(1)

    def _decrease_text_size(self) -> None:
        self._change_text_size(-1)

    def _change_text_size(self, step: int) -> None:
        next_delta = max(
            MIN_FONT_DELTA,
            min(MAX_FONT_DELTA, self.config.font_size_delta + step),
        )
        if next_delta == self.config.font_size_delta:
            return
        self.config.font_size_delta = next_delta
        save_config(self.config)
        self._build_style()
        self._refresh_current_screen()

    def _refresh_current_screen(self) -> None:
        self._discard_page(self.current_screen)
        if self.current_screen == "login":
            self.show_login_screen()
        elif self.current_screen == "gitmo":
            self.show_gitmo_screen()
        elif self.current_screen == "repos":
            self.show_repo_selection_screen()
        elif self.current_screen == "dashboard":
            self.show_dashboard()

    def _open_repo_manager(self) -> None:
        if not self.config.github_token:
            self.show_login_screen()
            return
        if not self.config.gitmo_path:
            self.show_gitmo_screen()
            return
        self.show_repo_selection_screen()

    def _ensure_github_client(self) -> GitHubClient:
        if not self.github_client:
            self.github_client = GitHubClient(self.config.github_token)
        return self.github_client

    def _cached_remote_repos(self) -> dict[str, GitHubRepo]:
        if (
            not self.config.github_login
            or self.config.cached_github_login != self.config.github_login
        ):
            return {}
        return {
            name: GitHubRepo(
                name=repo.name,
                clone_url=repo.clone_url,
                private=repo.private,
                default_branch=repo.default_branch,
            )
            for name, repo in self.config.cached_github_repos.items()
        }

    def _save_remote_repo_cache(self) -> None:
        self.config.cached_github_login = self.config.github_login
        self.config.cached_github_repos = {
            name: CachedGitHubRepo(
                name=repo.name,
                clone_url=repo.clone_url,
                private=repo.private,
                default_branch=repo.default_branch,
            )
            for name, repo in self.remote_repos.items()
        }
        save_config(self.config)

    @staticmethod
    def _remote_repo_signature(repos: dict[str, GitHubRepo]) -> tuple:
        return tuple(
            sorted(
                (
                    name,
                    repo.clone_url,
                    repo.private,
                    repo.default_branch,
                )
                for name, repo in repos.items()
            )
        )

    def _start_remote_repo_load(self, *, refresh_repos_screen: bool = False) -> None:
        # GitHub API calls can be slow. Load repos in the background so startup
        # and Manage Repos can paint immediately from local/cached state.
        if refresh_repos_screen:
            self.refresh_repos_screen_after_load = True
        if self.remote_repos_loaded or not self.config.github_token:
            return
        if self.remote_repos_loading:
            return
        self.remote_repos_loading = True
        self.remote_repos_error = ""
        client = self._ensure_github_client()

        def load() -> None:
            try:
                repos = client.list_repos()
            except GitHubAPIError as exc:
                self.remote_repos_queue.put(([], str(exc), refresh_repos_screen))
                return
            self.remote_repos_queue.put((repos, "", refresh_repos_screen))

        threading.Thread(target=load, daemon=True).start()
        self.root.after(100, self._poll_remote_repo_load)

    def _poll_remote_repo_load(self) -> None:
        try:
            remote_repos, error, refresh_repos_screen = self.remote_repos_queue.get_nowait()
        except queue.Empty:
            if self.remote_repos_loading and self.root.winfo_exists():
                self.root.after(100, self._poll_remote_repo_load)
            return
        self._finish_remote_repo_load(remote_repos, error, refresh_repos_screen)

    def _finish_remote_repo_load(
        self,
        remote_repos: list[GitHubRepo],
        error: str,
        refresh_repos_screen: bool,
    ) -> None:
        self.remote_repos_loading = False
        self.remote_repos_error = error
        should_refresh_repos_screen = refresh_repos_screen or self.refresh_repos_screen_after_load
        self.refresh_repos_screen_after_load = False
        if error:
            self._enqueue_log("github", error)
            if self.current_screen == "repos" and self.repo_manager_status_var is not None:
                self.repo_manager_status_var.set(
                    "Could not refresh GitHub repos. Showing the saved list."
                )
        else:
            updated_repos = {repo.name: repo for repo in remote_repos}
            catalog_changed = self._remote_repo_signature(
                self.remote_repos
            ) != self._remote_repo_signature(updated_repos)
            self.remote_repos = updated_repos
            self.remote_repos_loaded = True
            self._save_remote_repo_cache()
            self._enqueue_log("github", f"Loaded {len(remote_repos)} GitHub repo(s).")
            if self.current_screen == "repos" and self.repo_manager_status_var is not None:
                self.repo_manager_status_var.set("")
            if (
                self.current_screen == "repos"
                and self.repo_manager_status_label is not None
                and self.repo_manager_status_label.winfo_exists()
            ):
                self.repo_manager_status_label.destroy()
                self.repo_manager_status_label = None
        if (
            should_refresh_repos_screen
            and self.current_screen == "repos"
            and not error
            and catalog_changed
        ):
            self._discard_page("repos")
            self.show_repo_selection_screen()

    def _choose_folder(self, *, title: str, initial_path: Path, must_exist: bool) -> Path | None:
        dialog = FolderPickerDialog(
            self.root,
            title=title,
            initial_path=initial_path,
            must_exist=must_exist,
            font_size_delta=self.config.font_size_delta,
        )
        self.root.wait_window(dialog)
        return dialog.result

    def _build_ribbon_toolbar(self) -> None:
        toolbar = tk.Frame(
            self.content,
            bg="#f0f2f4",
            highlightbackground=THEME["line"],
            highlightcolor=THEME["line"],
            highlightthickness=1,
            bd=0,
            padx=5,
            pady=2,
        )
        toolbar.pack(fill="x", pady=(0, 14))
        sync_icon, sync_label, sync_color = sync_button_presentation(self.sync_should_run)
        nav_label = "Dashboard" if self.current_screen == "repos" else "Manage Repos"
        nav_command = self.show_dashboard if self.current_screen == "repos" else self.show_repo_selection_screen
        nav_icon = "▦" if self.current_screen == "repos" else "☷"
        self._ribbon_button(
            toolbar,
            nav_icon,
            nav_label,
            nav_command,
        ).pack(side="left")
        self._ribbon_separator(toolbar)
        sync_button = self._ribbon_button(
            toolbar,
            sync_icon,
            sync_label,
            self._toggle_sync_running,
            icon_color=sync_color,
        )
        sync_button.pack(side="left")
        self.sync_toolbar_buttons.append(sync_button)
        self._ribbon_separator(toolbar)
        self._ribbon_button(
            toolbar,
            "↓",
            "Run in Background",
            self._run_in_background,
        ).pack(side="left")

    def show_login_screen(self) -> None:
        self.current_screen = "login"
        self._show_window()
        self._prepare_page("login", reuse=False)
        if self.logo_image:
            ttk.Label(self.content, image=self.logo_image).pack(anchor="w", pady=(0, 14))
        self._page_header(
            "GitMo",
            "Automatic GitHub sync for local project folders.",
            show_logo=False,
        )
        ttk.Label(
            self.content,
            text="Enter a GitHub personal access token with repo access.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        token_var = tk.StringVar(value=self.config.github_token)
        ttk.Entry(self.content, textvariable=token_var, show="*", width=72).pack(anchor="w", fill="x")

        status_var = tk.StringVar(value="")
        ttk.Label(self.content, textvariable=status_var, style="Error.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

        def submit() -> None:
            token = token_var.get().strip()
            if not token:
                status_var.set("Token is required.")
                return
            client = GitHubClient(token)
            try:
                user = client.get_authenticated_user()
            except GitHubAPIError as exc:
                status_var.set(str(exc))
                return
            self.github_client = client
            self.config.github_token = token
            self.config.github_login = user.get("login", "")
            self.config.github_email = user.get("email") or (
                f"{self.config.github_login}@users.noreply.github.com"
                if self.config.github_login
                else "gitmo@users.noreply.github.com"
            )
            if self.config.cached_github_login != self.config.github_login:
                self.config.cached_github_login = ""
                self.config.cached_github_repos = {}
            save_config(self.config)
            self._enqueue_log("app", f"Authenticated as {self.config.github_login or 'unknown'}.")
            self.remote_repos_loaded = False
            self.remote_repos_loading = False
            self.remote_repos_error = ""
            self.refresh_repos_screen_after_load = False
            self.remote_repos = self._cached_remote_repos()
            self._start_remote_repo_load()
            self.show_gitmo_screen()

        self._button(self.content, "Continue", submit, variant="primary").pack(anchor="w", pady=(18, 0))

    def show_gitmo_screen(self) -> None:
        self.current_screen = "gitmo"
        self._show_window()
        self._prepare_page("gitmo", reuse=False)
        self._page_header(
            "Choose GitMo Folder",
            "Track direct children of the GitMo folder or folders anywhere on disk.",
        )

        path_var = tk.StringVar(value=self.config.gitmo_path or str(Path.home() / "GitMo"))
        row = ttk.Frame(self.content)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=path_var, width=72).pack(side="left", fill="x", expand=True)

        def browse() -> None:
            selected = self._choose_folder(
                title="Select GitMo folder",
                initial_path=Path(path_var.get() or str(Path.home())),
                must_exist=False,
            )
            if selected:
                path_var.set(str(selected))

        self._button(row, "Browse", browse, width=96).pack(side="left", padx=(10, 0))

        def submit() -> None:
            selected_path = Path(path_var.get()).expanduser()
            selected_path.mkdir(parents=True, exist_ok=True)
            self.config.gitmo_path = str(selected_path)
            save_config(self.config)
            self.show_repo_selection_screen()

        self._button(self.content, "Load Repos", submit, variant="primary").pack(anchor="w", pady=(18, 0))

    def show_repo_selection_screen(self) -> None:
        self.current_screen = "repos"
        self._show_window()
        if not self._prepare_page("repos", reuse=True):
            return
        self._build_ribbon_toolbar()

        search_var = tk.StringVar()
        header_card = self._card(self.content, padding=14)
        header_card.pack(fill="x", pady=(0, 8))
        header = header_card.inner  # type: ignore[attr-defined]

        search = tk.Frame(header, bg=THEME["card"])
        search.pack(side="right", anchor="se", padx=(18, 0))
        tk.Label(
            search,
            text="Search",
            bg=THEME["card"],
            fg=THEME["text"],
            font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta, "bold"),
            anchor="e",
        ).pack(fill="x", anchor="e", pady=(0, 3))
        search_row = tk.Frame(search, bg=THEME["card"])
        search_row.pack(anchor="e")
        search_entry = ttk.Entry(search_row, textvariable=search_var, width=34)
        search_entry.pack(side="left")
        tk.Button(
            search_row,
            text="🔍",
            command=search_entry.focus_set,
            bg=THEME["button"],
            fg=THEME["muted"],
            activebackground=THEME["button_hover"],
            activeforeground=THEME["text"],
            relief="flat",
            bd=0,
            padx=8,
            pady=2,
            cursor="hand2",
            font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
        ).pack(side="left", padx=(6, 0))

        if self.header_logo_image:
            tk.Label(header, image=self.header_logo_image, bg=THEME["card"]).pack(side="left", anchor="n", padx=(0, 12))
        header_text = tk.Frame(header, bg=THEME["card"])
        header_text.pack(side="left", anchor="n", fill="x", expand=True)
        tk.Label(
            header_text,
            text="Manage Repositories",
            bg=THEME["card"],
            fg=THEME["text"],
            font=(FONT_FAMILY, round((BASE_BODY_SIZE + self.config.font_size_delta) * 1.5), "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header_text,
            text="Choose repos and folders to sync.",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
            anchor="w",
        ).pack(fill="x", pady=(1, 0))

        status_var = tk.StringVar(value="")
        status_label = ttk.Label(self.content, textvariable=status_var, style="Subtitle.TLabel")
        status_label.pack(anchor="w", pady=(0, 4))
        self.repo_manager_status_var = status_var
        self.repo_manager_status_label = status_label

        self._ensure_github_client()
        if not self.remote_repos_loaded:
            if self.remote_repos_error:
                status_var.set("Could not load GitHub repos. Showing local and cached repos.")
            else:
                status_var.set(
                    "Showing saved repositories while GitHub refreshes in the background..."
                    if self.remote_repos
                    else "Loading GitHub repos in the background..."
                )
                self._start_remote_repo_load(refresh_repos_screen=True)
        self.repo_catalog = self._build_repo_catalog(list(self.remote_repos.values()))

        if not status_var.get():
            status_label.destroy()
            self.repo_manager_status_label = None
        self.repo_rows = {}
        saved_modes = [
            repo.sync_mode
            for repo in self.config.repos.values()
            if repo.enabled and repo.sync_mode in {"one-way", "two-way"}
        ]
        default_sync_mode = saved_modes[0] if saved_modes else "two-way"

        for selection in self.repo_catalog:
            saved_config = self.config.repos.get(selection.name)
            default_enabled = saved_config.enabled if saved_config else False
            if selection.name in self.pending_added_folder_names:
                default_enabled = True
            if selection.exists_local and selection.exists_remote and not selection.local_is_repo:
                default_enabled = False

            self.repo_rows[selection.name] = {
                "selected": LocalValue(False),
                "enabled": LocalValue(default_enabled),
                "github": LocalValue(selection.exists_remote or default_enabled),
                "local": LocalValue(selection.exists_local or default_enabled),
                "sync_mode": LocalValue(saved_config.sync_mode if saved_config else default_sync_mode),
                "sync_schedule": LocalValue(
                    saved_config.sync_schedule if saved_config else "idle-1m"
                ),
                "commit_message_mode": LocalValue(
                    saved_config.commit_message_mode if saved_config else "summary"
                ),
                "local_path": LocalValue(str(selection.local_path)),
            }

        def current_repo_settings(repo_name: str) -> tuple[bool, bool, bool, str, str, str, str]:
            row = self.repo_rows[repo_name]
            return repo_settings_state(
                github=bool(row["github"].get()),
                local=bool(row["local"].get()),
                enabled=bool(row["enabled"].get()),
                sync_mode=str(row["sync_mode"].get()),
                sync_schedule=str(row["sync_schedule"].get()),
                commit_message_mode=str(row["commit_message_mode"].get()),
                local_path=str(row["local_path"].get()),
            )

        initial_repo_settings = {
            selection.name: current_repo_settings(selection.name)
            for selection in self.repo_catalog
        }
        for selection in self.repo_catalog:
            if selection.name not in self.pending_added_folder_names:
                continue
            current = initial_repo_settings[selection.name]
            initial_repo_settings[selection.name] = repo_settings_state(
                github=selection.exists_remote,
                local=selection.exists_local,
                enabled=False,
                sync_mode=current[3],
                sync_schedule=current[4],
                commit_message_mode=current[5],
                local_path=current[6],
            )

        apply_button: RoundedButton | None = None

        def has_pending_repo_settings() -> bool:
            return any(
                current_repo_settings(repo_name) != initial_state
                for repo_name, initial_state in initial_repo_settings.items()
                if repo_name in self.repo_rows
            )

        def update_apply_button(*_args) -> None:
            if apply_button is not None:
                apply_button.set_enabled(has_pending_repo_settings())

        table_card = self._card(self.content, padding=0)
        table_card.pack(fill="both", expand=True)
        table_shell = table_card.inner  # type: ignore[attr-defined]
        table_header = tk.Frame(table_shell, bg=THEME["card"], padx=16, pady=12)
        table_header.pack(fill="x")
        self._card_label(table_header, "Repositories", bold=True, side="left")
        ttk.Checkbutton(
            table_header,
            text="Select All (Nuke)",
            command=lambda: set_all_visible(),
            variable=tk.BooleanVar(value=False),
            style="Card.TCheckbutton",
        ).pack(side="right")
        table_body = tk.Frame(table_shell, bg=THEME["card"])
        table_body.pack(fill="both", expand=True)

        def toggle_target(row: dict[str, LocalValue], key: str) -> None:
            row[key].set(not bool(row[key].get()))
            row["enabled"].set(bool(row["github"].get()) and bool(row["local"].get()))

        def is_outside_local_folder(selection: RepoSelection, row_vars: dict[str, LocalValue]) -> bool:
            local_path = Path(str(row_vars["local_path"].get())).expanduser()
            return not self._is_inside_gitmo_folder(local_path)

        def remove_outside_folder(selection: RepoSelection, dialog: tk.Toplevel) -> None:
            row_vars = self.repo_rows[selection.name]
            local_path = Path(str(row_vars["local_path"].get())).expanduser()
            if not is_outside_local_folder(selection, row_vars):
                return
            if not messagebox.askyesno(
                "GitMo",
                f"Remove '{selection.name}' from GitMo's repo list?\n\n"
                f"The local folder will not be deleted:\n{local_path}",
                parent=dialog,
            ):
                return

            self.config.repos.pop(selection.name, None)
            self.manual_folder_paths.pop(selection.name, None)
            save_config(self.config)
            self.repo_catalog = [
                item
                for item in self.repo_catalog
                if item.name != selection.name or item.exists_remote
            ]
            if not selection.exists_remote:
                self.repo_rows.pop(selection.name, None)
            elif selection.name in self.repo_rows:
                fallback_path = Path(self.config.gitmo_path).expanduser() / selection.name
                self.repo_catalog = [
                    RepoSelection(
                        name=item.name,
                        local_path=fallback_path,
                        exists_local=False,
                        exists_remote=True,
                        local_is_repo=False,
                        source="remote",
                        clone_url=item.clone_url,
                    )
                    if item.name == selection.name
                    else item
                    for item in self.repo_catalog
                ]
                self.repo_rows[selection.name]["selected"].set(False)
                self.repo_rows[selection.name]["local"].set(False)
                self.repo_rows[selection.name]["local_path"].set(str(fallback_path))
            dialog.destroy()
            populate_tree()
            update_delete_buttons()

        def open_repo_options(selection: RepoSelection) -> None:
            row_vars = self.repo_rows[selection.name]
            outside_folder = is_outside_local_folder(selection, row_vars)
            syncs_both = bool(row_vars["github"].get()) and bool(row_vars["local"].get())
            dialog = tk.Toplevel(self.root)
            dialog.title(f"{selection.name} Options")
            dialog.configure(bg=THEME["bg"])
            if syncs_both:
                dialog.geometry("640x610" if outside_folder else "640x550")
            else:
                dialog.geometry("600x360" if outside_folder else "600x300")
            dialog.minsize(520, 280)
            dialog.transient(self.root)
            dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

            body = tk.Frame(dialog, bg=THEME["bg"], padx=18, pady=18)
            body.pack(fill="both", expand=True)
            tk.Label(
                body,
                text=selection.name,
                bg=THEME["bg"],
                fg=THEME["text"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta + 4, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 10))

            tk.Label(
                body,
                text="Sync direction",
                bg=THEME["bg"],
                fg=THEME["muted"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta, "bold"),
                anchor="w",
            ).pack(fill="x")
            sync_mode_var = tk.StringVar(value=str(row_vars["sync_mode"].get()))
            tk.Radiobutton(
                body,
                text="One way: local folder uploads to GitHub",
                variable=sync_mode_var,
                value="one-way",
                bg=THEME["bg"],
                fg=THEME["text"],
                activebackground=THEME["bg"],
                activeforeground=THEME["text"],
                selectcolor=THEME["card"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
                anchor="w",
                wraplength=520,
            ).pack(fill="x", anchor="w", pady=(8, 0))
            tk.Radiobutton(
                body,
                text="Two way: local and GitHub both update each other",
                variable=sync_mode_var,
                value="two-way",
                bg=THEME["bg"],
                fg=THEME["text"],
                activebackground=THEME["bg"],
                activeforeground=THEME["text"],
                selectcolor=THEME["card"],
                font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
                anchor="w",
                wraplength=520,
            ).pack(fill="x", anchor="w", pady=(4, 0))

            schedule_var = tk.StringVar(
                value=next(
                    (
                        label
                        for label, value in SYNC_SCHEDULE_OPTIONS.items()
                        if value == str(row_vars["sync_schedule"].get())
                    ),
                    "After 1 minute idle (Recommended)",
                )
            )
            commit_message_var = tk.StringVar(
                value=next(
                    (
                        label
                        for label, value in COMMIT_MESSAGE_OPTIONS.items()
                        if value == str(row_vars["commit_message_mode"].get())
                    ),
                    "Changed-file summary (Recommended)",
                )
            )
            if syncs_both:
                ttk.Separator(body).pack(fill="x", pady=14)
                tk.Label(
                    body,
                    text="Local commit and push schedule",
                    bg=THEME["bg"],
                    fg=THEME["muted"],
                    font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta, "bold"),
                    anchor="w",
                ).pack(fill="x")
                ttk.Combobox(
                    body,
                    textvariable=schedule_var,
                    values=list(SYNC_SCHEDULE_OPTIONS),
                    state="readonly",
                    width=48,
                ).pack(fill="x", pady=(6, 4))
                tk.Label(
                    body,
                    text="Timed schedules commit only when Git detects local changes. Run Sync Now always commits immediately.",
                    bg=THEME["bg"],
                    fg=THEME["muted"],
                    font=(FONT_FAMILY, max(8, BASE_BODY_SIZE + self.config.font_size_delta - 2)),
                    anchor="w",
                    justify="left",
                    wraplength=570,
                ).pack(fill="x")

                tk.Label(
                    body,
                    text="Commit message",
                    bg=THEME["bg"],
                    fg=THEME["muted"],
                    font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta, "bold"),
                    anchor="w",
                ).pack(fill="x", pady=(14, 0))
                ttk.Combobox(
                    body,
                    textvariable=commit_message_var,
                    values=list(COMMIT_MESSAGE_OPTIONS),
                    state="readonly",
                    width=48,
                ).pack(fill="x", pady=(6, 0))

            if outside_folder:
                ttk.Separator(body).pack(fill="x", pady=14)
                self._button(
                    body,
                    "Remove from Repo List",
                    lambda: remove_outside_folder(selection, dialog),
                    variant="danger",
                    width=210,
                    canvas_bg=THEME["bg"],
                ).pack(anchor="w")

            actions = tk.Frame(body, bg=THEME["bg"])
            actions.pack(fill="x", pady=(18, 0))

            def save_options() -> None:
                row_vars["sync_mode"].set(sync_mode_var.get())
                if syncs_both:
                    row_vars["sync_schedule"].set(
                        SYNC_SCHEDULE_OPTIONS[schedule_var.get()]
                    )
                    row_vars["commit_message_mode"].set(
                        COMMIT_MESSAGE_OPTIONS[commit_message_var.get()]
                    )
                update_apply_button()
                dialog.destroy()

            self._button(actions, "Cancel", dialog.destroy, width=104, canvas_bg=THEME["bg"]).pack(side="right")
            self._button(
                actions,
                "Save",
                save_options,
                variant="primary",
                width=104,
                canvas_bg=THEME["bg"],
            ).pack(side="right", padx=(0, 8))

            dialog.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"{dialog.winfo_width()}x{dialog.winfo_height()}+{x}+{y}")
            dialog.lift(self.root)
            dialog.focus_force()
            dialog.grab_set()

        def visible_items() -> list[RepoSelection]:
            query = search_var.get().strip().lower()
            return [
                item
                for item in self.repo_catalog
                if not query or query in item.name.lower() or query in str(item.local_path).lower()
            ]

        def set_all_visible() -> None:
            visible = visible_items()
            checked = not all(bool(self.repo_rows[selection.name]["selected"].get()) for selection in visible)
            for selection in visible_items():
                self.repo_rows[selection.name]["selected"].set(checked)
                refresh_tree_row(selection.name)
            update_delete_buttons()

        columns = ("delete", "name", "github", "local", "location")
        tree = ttk.Treeview(
            table_body,
            columns=columns,
            show="headings",
            selectmode="none",
            height=min(18, max(6, len(self.repo_catalog))),
        )
        tree.heading("delete", text="Delete")
        tree.heading("name", text="Name")
        tree.heading("github", text="GitHub")
        tree.heading("local", text="Local")
        tree.heading("location", text="Folder Location")
        tree.column("delete", width=70, minwidth=60, anchor="center", stretch=False)
        tree.column("name", width=260, minwidth=160, anchor="w")
        tree.column("github", width=110, minwidth=90, anchor="center", stretch=False)
        tree.column("local", width=100, minwidth=80, anchor="center", stretch=False)
        tree.column("location", width=300, minwidth=200, anchor="w")
        tree_scroll = ttk.Scrollbar(table_body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        selections_by_name = {selection.name: selection for selection in self.repo_catalog}

        def tree_values(repo_name: str) -> tuple[str, str, str, str, str]:
            row = self.repo_rows[repo_name]
            local_path = Path(str(row["local_path"].get())).expanduser()
            local_enabled = bool(row["local"].get())
            return (
                "☑" if bool(row["selected"].get()) else "☐",
                repo_name,
                "✅" if bool(row["github"].get()) else "❌",
                "✅" if local_enabled else "❌",
                self._shorten_folder_parent(local_path) if local_enabled else "",
            )

        def refresh_tree_row(repo_name: str) -> None:
            if tree.exists(repo_name):
                tree.item(repo_name, values=tree_values(repo_name))

        def populate_tree() -> None:
            tree.delete(*tree.get_children())
            for selection in visible_items():
                tree.insert(
                    "",
                    "end",
                    iid=selection.name,
                    values=tree_values(selection.name),
                )

        def on_tree_click(event) -> str | None:
            repo_name = tree.identify_row(event.y)
            column = tree.identify_column(event.x)
            if not repo_name or repo_name not in self.repo_rows:
                return None
            row = self.repo_rows[repo_name]
            if column == "#1":
                row["selected"].set(not bool(row["selected"].get()))
            elif column == "#2":
                open_repo_options(selections_by_name[repo_name])
                return "break"
            elif column == "#3":
                toggle_target(row, "github")
            elif column == "#4":
                toggle_target(row, "local")
            else:
                return None
            refresh_tree_row(repo_name)
            return "break"

        def on_tree_double_click(event) -> str | None:
            repo_name = tree.identify_row(event.y)
            if (
                not repo_name
                or tree.identify_column(event.x) != "#5"
                or repo_name not in self.repo_rows
            ):
                return None
            row = self.repo_rows[repo_name]
            local_path = Path(str(row["local_path"].get())).expanduser()
            if bool(row["local"].get()) and local_path.exists():
                self._open_local_path(local_path)
                return "break"
            return None

        tree.bind("<Button-1>", on_tree_click)
        tree.bind("<Double-1>", on_tree_double_click)
        search_after_id: str | None = None

        def apply_filter() -> None:
            populate_tree()

        def schedule_filter(*_args) -> None:
            nonlocal search_after_id
            if search_after_id is not None:
                self.root.after_cancel(search_after_id)

            def run_scheduled_filter() -> None:
                nonlocal search_after_id
                search_after_id = None
                apply_filter()

            search_after_id = self.root.after(180, run_scheduled_filter)

        search_var.trace_add("write", schedule_filter)
        populate_tree()

        tk.Frame(self.content, bg=THEME["bg"], height=48).pack(fill="x")
        bottom = self._fixed_bottom_bar()
        delete_github_button = self._button(
            bottom,
            "Delete GitHub Repo",
            self._delete_selected_github_repos,
            variant="danger",
            width=170,
            canvas_bg=THEME["card"],
        )
        delete_github_button.pack(side="left")
        delete_local_button = self._button(
            bottom,
            "Delete Local Folder",
            self._delete_selected_local_folders,
            variant="danger",
            width=176,
            canvas_bg=THEME["card"],
        )
        delete_local_button.pack(side="left", padx=(8, 0))
        self._button(
            bottom,
            "Add Local Folder",
            self._add_outside_folder_selection,
            width=166,
            canvas_bg=THEME["card"],
        ).pack(side="left", padx=(8, 0))
        apply_button = self._button(
            bottom,
            "Apply Selection",
            self._apply_repo_selection,
            variant="primary",
            width=150,
            canvas_bg=THEME["card"],
        )
        apply_button.pack(side="right")
        self._button(bottom, "Cancel", self.show_dashboard, width=104, canvas_bg=THEME["card"]).pack(side="right", padx=(0, 8))

        def update_delete_buttons(*_args) -> None:
            selected = self._selected_repo_selections()
            delete_github_button.set_enabled(any(selection.exists_remote for selection in selected))
            delete_local_button.set_enabled(any(selection.exists_local for selection in selected))

        for row in self.repo_rows.values():
            row["selected"].trace_add("write", update_delete_buttons)
            for key in (
                "github",
                "local",
                "enabled",
                "sync_mode",
                "sync_schedule",
                "commit_message_mode",
                "local_path",
            ):
                row[key].trace_add("write", update_apply_button)
        update_delete_buttons()
        update_apply_button()

    def show_dashboard(self) -> None:
        self.current_screen = "dashboard"
        self._show_window()
        if not self._prepare_page("dashboard", reuse=True):
            self.is_paused = not self.sync_should_run
            self._refresh_dashboard_summary()
            self._refresh_dashboard_table()
            self._refresh_log_view()
            if self.sync_should_run:
                self._ensure_sync_engine_running()
            self._schedule_status_refresh(1000)
            return
        self.status_vars = {}
        self.status_labels = {}
        self.last_sync_vars = {}
        self.header_last_sync_var = tk.StringVar(value="Last Sync: Not yet")
        self.header_git_var = tk.StringVar(value="Git: OK")
        self.is_paused = not self.sync_should_run

        self._build_ribbon_toolbar()

        enabled_repos = [
            (name, repo_config)
            for name, repo_config in sorted(self.config.repos.items())
            if repo_config.enabled
        ]
        repo_count = len(enabled_repos)

        header_card = self._card(self.content, padding=14)
        header_card.pack(fill="x", pady=(0, 14))
        header = header_card.inner  # type: ignore[attr-defined]

        status_header = tk.Frame(header, bg=THEME["card"])
        status_header.pack(side="right", anchor="ne", padx=(18, 0))
        self.dashboard_watch_label = tk.Label(
            status_header,
            textvariable=self.dashboard_watch_var,
            bg=THEME["card"],
            fg=THEME["success"] if self.sync_should_run else THEME["danger"],
            font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta + 1, "bold"),
            justify="right",
            anchor="e",
        )
        self.dashboard_watch_label.pack(fill="x", anchor="e")
        tk.Label(
            status_header,
            textvariable=self.dashboard_summary_var,
            bg=THEME["card"],
            fg=THEME["muted"],
            justify="right",
            anchor="e",
            font=(FONT_FAMILY, max(8, BASE_BODY_SIZE + self.config.font_size_delta - 1)),
        ).pack(fill="x", anchor="e", pady=(2, 0))

        if self.header_logo_image:
            tk.Label(header, image=self.header_logo_image, bg=THEME["card"]).pack(side="left", anchor="n", padx=(0, 12))
        branding = tk.Frame(header, bg=THEME["card"])
        branding.pack(side="left", anchor="nw", fill="x", expand=True)
        tk.Label(
            branding,
            text="GitMo",
            bg=THEME["card"],
            fg=THEME["text"],
            font=(FONT_FAMILY, round((BASE_BODY_SIZE + self.config.font_size_delta) * 1.5), "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            branding,
            text="Auto sync for GitHub folders",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=(FONT_FAMILY, BASE_BODY_SIZE + self.config.font_size_delta),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))
        self._refresh_dashboard_summary()

        repos_card = self._card(self.content)
        repos_card.pack(fill="both", expand=True, pady=(0, 12))
        repos = repos_card.inner  # type: ignore[attr-defined]
        repos_header = tk.Frame(repos, bg=THEME["card"])
        repos_header.pack(fill="x", pady=(0, 10))
        self._card_label(repos_header, "Repositories", bold=True, side="left")
        self._card_label(
            repos_header,
            f"{repo_count} repo{'s' if repo_count != 1 else ''}",
            muted=True,
            side="right",
        )
        table = tk.Frame(repos, bg=THEME["card"])
        table.pack(fill="both", expand=True)
        columns = ("repo", "mode", "local", "status", "last_sync")
        self.dashboard_tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            selectmode="none",
            height=max(3, min(12, repo_count or 3)),
        )
        headings = {
            "repo": "Repo",
            "mode": "Mode",
            "local": "Local Folder",
            "status": "Status",
            "last_sync": "Last Sync",
        }
        widths = {
            "repo": 175,
            "mode": 100,
            "local": 280,
            "status": 240,
            "last_sync": 110,
        }
        for column in columns:
            self.dashboard_tree.heading(column, text=headings[column])
            self.dashboard_tree.column(
                column,
                width=widths[column],
                minwidth=80,
                anchor="w",
            )
        dashboard_scroll = ttk.Scrollbar(
            table,
            orient="vertical",
            command=self.dashboard_tree.yview,
        )
        self.dashboard_tree.configure(yscrollcommand=dashboard_scroll.set)
        self.dashboard_tree.pack(side="left", fill="both", expand=True)
        dashboard_scroll.pack(side="right", fill="y")

        for repo_name, repo_config in enabled_repos:
            repo_path = self.sync_engine.repo_path_for(repo_name, repo_config)
            status_var = tk.StringVar(value="🟢 Watching")
            last_sync_var = tk.StringVar(value="Not yet")
            self.dashboard_tree.insert(
                "",
                "end",
                iid=repo_name,
                values=(
                    repo_name,
                    repo_config.sync_mode,
                    self._shorten_path(repo_path),
                    status_var.get(),
                    last_sync_var.get(),
                ),
            )
            self.status_vars[repo_name] = status_var
            self.last_sync_vars[repo_name] = last_sync_var

        if not enabled_repos:
            self.dashboard_tree.insert(
                "",
                "end",
                iid="__empty__",
                values=("No repositories selected yet.", "", "", "", ""),
            )

        log_card = self._card(self.content)
        log_card.pack(fill="x", pady=(0, 12))
        log = log_card.inner  # type: ignore[attr-defined]
        log_header = tk.Frame(log, bg=THEME["card"])
        log_header.pack(fill="x", pady=(0, 8))
        self._card_label(log_header, "Activity Log", bold=True, side="left")
        self._button(log_header, "Clear Log", self._clear_log_and_refresh, width=104, canvas_bg=THEME["card"]).pack(side="right")
        log_body = tk.Frame(log, bg=THEME["card"])
        log_body.pack(fill="x")
        self.log_text = tk.Text(
            log_body,
            height=5,
            bg=THEME["panel_alt"],
            fg=THEME["text"],
            relief="flat",
            wrap="word",
            padx=10,
            pady=8,
        )
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self._refresh_log_view()

        footer = tk.Frame(self.content, bg=THEME["bg"])
        footer.pack(fill="x")
        tk.Label(
            footer,
            textvariable=self.last_checked_var,
            bg=THEME["bg"],
            fg=THEME["muted"],
            anchor="e",
        ).pack(side="right")

        if self.sync_should_run:
            self._ensure_sync_engine_running()
        self._schedule_status_refresh(1000)

    def _start_sync_engine_if_current(self) -> None:
        if self.current_screen == "dashboard" and self.sync_should_run:
            self._ensure_sync_engine_running()

    def _ensure_sync_engine_running(self) -> None:
        if not self.sync_should_run or not self._has_enabled_repos():
            return
        self.is_paused = False
        self.sync_engine.start()

    def _open_gitmo_folder(self) -> None:
        if not self.config.gitmo_path:
            self.show_gitmo_screen()
            return
        self._open_local_path(Path(self.config.gitmo_path).expanduser())

    def _open_local_path(self, path: Path) -> None:
        try:
            subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("GitMo", f"Could not open folder:\n{exc}")

    def _open_github_profile(self) -> None:
        if not self.config.github_login:
            messagebox.showinfo("GitMo", "No GitHub profile is available until you sign in.")
            return
        webbrowser.open(f"https://github.com/{self.config.github_login}")

    def _toggle_sync_running(self) -> None:
        if self.sync_should_run:
            self._stop_sync()
        else:
            self._start_sync()

    def _start_sync(self) -> None:
        self.sync_should_run = True
        self.is_paused = False
        self._ensure_sync_engine_running()
        self._refresh_sync_controls()
        if self.current_screen == "dashboard":
            self._schedule_status_refresh(250)

    def _stop_sync(self) -> None:
        self.sync_should_run = False
        self.is_paused = True
        self.sync_engine.stop()
        for repo_name, status_var in self.status_vars.items():
            status_var.set("🟠 Paused")
            self._refresh_dashboard_tree_row(repo_name)
        self._refresh_sync_controls()

    def _refresh_sync_controls(self) -> None:
        icon, label, color = sync_button_presentation(self.sync_should_run)
        live_buttons: list[tk.Frame] = []
        for button in self.sync_toolbar_buttons:
            if not button.winfo_exists():
                continue
            icon_label = getattr(button, "icon_label", None)
            text_label = getattr(button, "text_label", None)
            if icon_label is not None:
                icon_label.configure(text=icon, fg=color)
            if text_label is not None:
                text_label.configure(text=label)
            live_buttons.append(button)
        self.sync_toolbar_buttons = live_buttons
        self.dashboard_watch_var.set("Watching" if self.sync_should_run else "Paused")
        if self.dashboard_watch_label is not None and self.dashboard_watch_label.winfo_exists():
            self.dashboard_watch_label.configure(
                fg=THEME["success"] if self.sync_should_run else THEME["danger"]
            )
        self._refresh_dashboard_summary()

    def _clear_log_and_refresh(self) -> None:
        self._clear_log_file()
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        if not self.log_text:
            return
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.touch(exist_ok=True)
        lines = tail_text_lines(LOG_PATH, 80)
        display_lines = [self._format_log_line(line) for line in lines]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(display_lines) if display_lines else "No activity yet.")
        self.log_text.configure(state="disabled")

    def _format_log_line(self, line: str) -> str:
        parts = line.split(" ", 2)
        if len(parts) != 3:
            return line
        date, time_text, message = parts
        if len(date) == 10 and len(time_text) == 8:
            return f"{date}   {time_text}   {message}"
        return line

    def _refresh_dashboard_tree_row(self, repo_name: str) -> None:
        if self.dashboard_tree is None or not self.dashboard_tree.exists(repo_name):
            return
        repo_config = self.config.repos.get(repo_name)
        if repo_config is None:
            return
        repo_path = self.sync_engine.repo_path_for(repo_name, repo_config)
        self.dashboard_tree.item(
            repo_name,
            values=(
                repo_name,
                repo_config.sync_mode,
                self._shorten_path(repo_path),
                self.status_vars.get(repo_name, tk.StringVar(value="🟢 Watching")).get(),
                self.last_sync_vars.get(repo_name, tk.StringVar(value="Not yet")).get(),
            ),
        )

    def _refresh_dashboard_summary(self) -> None:
        enabled_repos = [
            repo_config
            for repo_config in self.config.repos.values()
            if repo_config.enabled
        ]
        modes = {repo.sync_mode for repo in enabled_repos}
        if not modes:
            mode_text = "No mode"
        elif len(modes) == 1:
            mode_text = "Two-way" if "two-way" in modes else "One-way"
        else:
            mode_text = "Mixed mode"
        folder = (
            self._shorten_path(Path(self.config.gitmo_path).expanduser())
            if self.config.gitmo_path
            else "Not selected"
        )
        self.dashboard_watch_var.set("Watching" if self.sync_should_run else "Paused")
        self.dashboard_summary_var.set(
            f"{len(enabled_repos)} repos | {mode_text} | {folder}\n"
            f"{self.header_last_sync_var.get()} | {self.header_git_var.get()}"
        )

    def _refresh_dashboard_table(self) -> None:
        if self.dashboard_tree is None:
            return
        enabled_names = {
            name
            for name, repo_config in self.config.repos.items()
            if repo_config.enabled
        }
        existing = set(self.dashboard_tree.get_children())
        if "__empty__" in existing and enabled_names:
            self.dashboard_tree.delete("__empty__")
            existing.remove("__empty__")
        for repo_name in existing - enabled_names:
            if self.dashboard_tree.exists(repo_name):
                self.dashboard_tree.delete(repo_name)
        for repo_name in sorted(enabled_names):
            self.status_vars.setdefault(repo_name, tk.StringVar(value="🟢 Watching"))
            self.last_sync_vars.setdefault(repo_name, tk.StringVar(value="Not yet"))
            if not self.dashboard_tree.exists(repo_name):
                self.dashboard_tree.insert("", "end", iid=repo_name)
            self._refresh_dashboard_tree_row(repo_name)
        if not enabled_names and not self.dashboard_tree.exists("__empty__"):
            self.dashboard_tree.insert(
                "",
                "end",
                iid="__empty__",
                values=("No repositories selected yet.", "", "", "", ""),
            )

    def _status_text_and_color(self, state: str, detail: str) -> tuple[str, str]:
        if state == "idle":
            color = THEME["success"]
            indicator = "🟢"
            label = "Watching"
        elif state in {"synced", "pulled"}:
            color = THEME["success"]
            indicator = "🟢"
            label = state.title()
        elif state == "pending":
            color = THEME["sync"]
            indicator = "🔵"
            label = "Syncing"
        elif state in {"warning", "conflict"}:
            color = THEME["warning"]
            indicator = "🟠"
            label = "Conflict"
        elif state == "error":
            color = THEME["danger"]
            indicator = "🔴"
            label = "Error"
        else:
            color = THEME["sync"]
            indicator = "🔵"
            label = state.title() if state else "Syncing"
        return (
            f"{indicator} {label}: {detail}" if detail else f"{indicator} {label}",
            color,
        )

    def _try_resume(self) -> None:
        self.github_client = GitHubClient(self.config.github_token)
        self.show_dashboard()
        self._start_remote_repo_load()

    def _build_repo_catalog(self, remote_repos: list[GitHubRepo]) -> list[RepoSelection]:
        remote_map = {repo.name: repo for repo in remote_repos}
        gitmo_path = Path(self.config.gitmo_path).expanduser()
        local_map: dict[str, Path] = {}

        if gitmo_path.exists():
            for child in gitmo_path.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    local_map.setdefault(child.name, child)

        for repo_name, repo_config in self.config.repos.items():
            local_path = self.sync_engine.repo_path_for(repo_name, repo_config)
            # Keep configured names visible while GitHub repos are still loading.
            # Many GitHub-only rows have no local folder, so filtering only by
            # local existence makes them seem to disappear until the API returns.
            local_map[repo_name] = local_path

        for repo_name, local_path in self.manual_folder_paths.items():
            local_map[repo_name] = local_path

        names = set(remote_map) | set(local_map)
        catalog: list[RepoSelection] = []
        for name in names:
            local_path = local_map.get(name, gitmo_path / name)
            remote_repo = remote_map.get(name)
            exists_local = local_path.exists()
            exists_remote = remote_repo is not None
            saved_config = self.config.repos.get(name)
            if (
                self.remote_repos_loaded
                and saved_config
                and not saved_config.enabled
                and not exists_local
                and not exists_remote
            ):
                continue
            catalog.append(
                RepoSelection(
                    name=name,
                    local_path=local_path,
                    exists_local=exists_local,
                    exists_remote=exists_remote,
                    local_is_repo=self._is_git_repo_fast(local_path) if exists_local else False,
                    source="tracked"
                    if name in self.config.repos
                    else ("gitmo" if self._is_inside_gitmo_folder(local_path) else "remote"),
                    clone_url=remote_repo.clone_url if remote_repo else None,
                )
            )
        return sorted(catalog, key=repo_catalog_sort_key)

    def _is_git_repo_fast(self, path: Path) -> bool:
        return (path / ".git").exists()

    def _is_inside_gitmo_folder(self, path: Path) -> bool:
        if not self.config.gitmo_path:
            return False
        gitmo_path = Path(self.config.gitmo_path).expanduser()
        try:
            return path.expanduser().resolve().is_relative_to(gitmo_path.resolve())
        except (OSError, ValueError):
            try:
                return path.expanduser().resolve().parent == gitmo_path.resolve()
            except OSError:
                return path.expanduser().parent == gitmo_path

    def _add_outside_folder_selection(self) -> None:
        selected = self._choose_folder(
            title="Select outside folder to sync",
            initial_path=Path.home(),
            must_exist=True,
        )
        if not selected:
            return

        local_path = selected.expanduser()
        repo_name = local_path.name

        existing = next((item for item in self.repo_catalog if item.name == repo_name), None)
        if existing and existing.local_path != local_path and (existing.exists_local or existing.name in self.config.repos):
            messagebox.showerror(
                "GitMo",
                f"A repo named '{repo_name}' is already tracked at {existing.local_path}.\n"
                "GitMo currently requires one unique local folder per GitHub repo name.",
            )
            return

        self.manual_folder_paths[repo_name] = local_path
        self.pending_added_folder_names.add(repo_name)
        existing_config = self.config.repos.get(repo_name)
        self.config.repos[repo_name] = RepoConfig(
            name=repo_name,
            local_path=str(local_path),
            sync_mode=existing_config.sync_mode if existing_config else "two-way",
            enabled=existing_config.enabled if existing_config else False,
            sync_schedule=existing_config.sync_schedule if existing_config else "idle-1m",
            commit_message_mode=(
                existing_config.commit_message_mode if existing_config else "summary"
            ),
        )
        save_config(self.config)
        remote_repo = self.remote_repos.get(repo_name)
        selection = RepoSelection(
            name=repo_name,
            local_path=local_path,
            exists_local=True,
            exists_remote=remote_repo is not None,
            local_is_repo=self._is_git_repo_fast(local_path),
            source="manual",
            clone_url=remote_repo.clone_url if remote_repo else None,
        )

        if existing:
            index = self.repo_catalog.index(existing)
            self.repo_catalog[index] = selection
        else:
            self.repo_catalog.append(selection)
            self.repo_catalog.sort(key=lambda item: item.name.lower())

        self._discard_page("repos")
        self.show_repo_selection_screen()

    def _selected_repo_names(self) -> list[str]:
        return [
            repo_name
            for repo_name, row in self.repo_rows.items()
            if bool(row["selected"].get())
        ]

    def _selected_repo_selections(self) -> list[RepoSelection]:
        selected_names = set(self._selected_repo_names())
        return [selection for selection in self.repo_catalog if selection.name in selected_names]

    def _delete_selected_local_folders(self) -> None:
        selections = [selection for selection in self._selected_repo_selections() if selection.exists_local]
        if not selections:
            messagebox.showinfo("GitMo", "Check one or more local folders first.")
            return

        names = ", ".join(selection.name for selection in selections)
        if not self._confirm_destructive_action(
            "Delete local folder(s)",
            f"This permanently deletes the local folder(s):\n\n{names}\n\n"
            "GitHub repositories are not deleted by this action.",
        ):
            return

        deleted = 0
        for selection in selections:
            path = selection.local_path.expanduser()
            if not self._safe_to_delete_local_folder(path):
                messagebox.showerror("GitMo", f"GitMo will not delete this folder:\n{path}")
                continue
            shutil.rmtree(path)
            deleted += 1
            if selection.name in self.repo_rows:
                self.repo_rows[selection.name]["selected"].set(False)
                self.repo_rows[selection.name]["local"].set(False)
                self.repo_rows[selection.name]["enabled"].set(False)

        self._save_repo_rows_to_config()
        self._enqueue_log("app", f"Deleted {deleted} local folder(s).")
        self._discard_page("repos")
        self.show_repo_selection_screen()

    def _delete_selected_github_repos(self) -> None:
        assert self.github_client is not None
        selections = [selection for selection in self._selected_repo_selections() if selection.exists_remote]
        if not selections:
            messagebox.showinfo("GitMo", "Check one or more GitHub repos first.")
            return
        if not self.config.github_login:
            messagebox.showerror("GitMo", "GitHub username is missing. Sign in again before deleting repos.")
            return

        names = ", ".join(f"{self.config.github_login}/{selection.name}" for selection in selections)
        if not self._confirm_destructive_action(
            "Delete GitHub repo(s)",
            f"This permanently deletes the GitHub repo(s):\n\n{names}\n\n"
            "Local folders are not deleted by this action.",
        ):
            return

        deleted = 0
        try:
            for selection in selections:
                self.github_client.delete_repo(self.config.github_login, selection.name)
                deleted += 1
                self.remote_repos.pop(selection.name, None)
                if selection.name in self.repo_rows:
                    self.repo_rows[selection.name]["selected"].set(False)
                    self.repo_rows[selection.name]["github"].set(False)
                    self.repo_rows[selection.name]["enabled"].set(False)
        except GitHubAPIError as exc:
            messagebox.showerror("GitMo", str(exc))
            return

        self._save_remote_repo_cache()
        self._save_repo_rows_to_config()
        self._enqueue_log("app", f"Deleted {deleted} GitHub repo(s).")
        self._discard_page("repos")
        self.show_repo_selection_screen()

    def _confirm_destructive_action(self, title: str, detail: str) -> bool:
        confirmed = messagebox.askyesno(
            "GitMo",
            f"{detail}\n\nThis cannot be undone. Continue?",
        )
        if not confirmed:
            return False
        typed = simpledialog.askstring(
            title,
            "Type DELETE to confirm.",
            parent=self.root,
        )
        return typed == "DELETE"

    def _safe_to_delete_local_folder(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            home = Path.home().resolve()
            gitmo_path = Path(self.config.gitmo_path).expanduser().resolve() if self.config.gitmo_path else None
        except OSError:
            return False

        blocked_paths = {home, Path("/").resolve()}
        if gitmo_path:
            blocked_paths.add(gitmo_path)
        return resolved.exists() and resolved.is_dir() and resolved not in blocked_paths

    def _save_repo_rows_to_config(self) -> None:
        updated_repos: dict[str, RepoConfig] = {}
        for selection in self.repo_catalog:
            row = self.repo_rows.get(selection.name)
            if not row:
                continue
            if self._row_has_no_targets(row):
                continue
            repo_path = Path(str(row["local_path"].get())).expanduser()
            updated_repos[selection.name] = RepoConfig(
                name=selection.name,
                local_path=str(repo_path),
                sync_mode=str(row["sync_mode"].get()),
                enabled=self._row_sync_enabled(row),
                sync_schedule=str(row["sync_schedule"].get()),
                commit_message_mode=str(row["commit_message_mode"].get()),
            )
        self.config.repos = updated_repos
        save_config(self.config)

    def _row_sync_enabled(self, row: dict[str, LocalValue]) -> bool:
        return bool(row["enabled"].get())

    def _row_has_no_targets(self, row: dict[str, LocalValue]) -> bool:
        return not bool(row["github"].get()) and not bool(row["local"].get())

    def _confirm_apply_destructive_actions(
        self,
        delete_remote: list[RepoSelection],
        delete_local: list[RepoSelection],
        delete_everywhere: list[RepoSelection],
    ) -> bool:
        if not delete_remote and not delete_local and not delete_everywhere:
            return True

        lines = [
            "Apply Selection will permanently delete some selected items.",
            "",
        ]
        if delete_everywhere:
            names = ", ".join(selection.name for selection in delete_everywhere)
            lines.extend(
                [
                    f"Delete everywhere: {names}",
                    "These repos/folders have both GitHub and Local Folder set to red X.",
                    "They will be deleted everywhere GitMo can find them.",
                    "",
                ]
            )
        remote_only = [selection for selection in delete_remote if selection not in delete_everywhere]
        local_only = [selection for selection in delete_local if selection not in delete_everywhere]
        if remote_only:
            lines.append("Delete from GitHub: " + ", ".join(selection.name for selection in remote_only))
        if local_only:
            lines.append("Delete local folder: " + ", ".join(selection.name for selection in local_only))
        lines.extend(["", "Continue?"])
        return messagebox.askyesno("GitMo", "\n".join(lines), parent=self.root)

    def _delete_remote_for_apply(self, selection: RepoSelection) -> None:
        if not selection.exists_remote:
            return
        if not self.config.github_login:
            raise GitHubAPIError("GitHub username is missing. Sign in again before deleting repos.")
        assert self.github_client is not None
        self.github_client.delete_repo(self.config.github_login, selection.name)
        self.remote_repos.pop(selection.name, None)
        self._save_remote_repo_cache()
        self._enqueue_log(selection.name, "Deleted GitHub repo during Apply Selection.")

    def _delete_local_for_apply(self, selection: RepoSelection) -> None:
        if not selection.exists_local:
            return
        path = selection.local_path.expanduser()
        if not self._safe_to_delete_local_folder(path):
            raise GitCommandError(f"GitMo will not delete this folder: {path}")
        shutil.rmtree(path)
        self._enqueue_log(selection.name, "Deleted local folder during Apply Selection.")

    def _apply_repo_selection(self) -> None:
        assert self.github_client is not None
        updated_repos: dict[str, RepoConfig] = {}
        selected_selections = [
            selection
            for selection in self.repo_catalog
            if bool(self.repo_rows[selection.name]["selected"].get())
        ]
        delete_remote = [
            selection
            for selection in selected_selections
            if selection.exists_remote and not bool(self.repo_rows[selection.name]["github"].get())
        ]
        delete_local = [
            selection
            for selection in selected_selections
            if selection.exists_local and not bool(self.repo_rows[selection.name]["local"].get())
        ]
        delete_everywhere = [
            selection
            for selection in selected_selections
            if (
                not bool(self.repo_rows[selection.name]["github"].get())
                and not bool(self.repo_rows[selection.name]["local"].get())
                and (selection.exists_remote or selection.exists_local)
            )
        ]
        if not self._confirm_apply_destructive_actions(delete_remote, delete_local, delete_everywhere):
            return

        try:
            for selection in self.repo_catalog:
                row = self.repo_rows[selection.name]
                selected = bool(row["selected"].get())
                wants_github = bool(row["github"].get())
                wants_local = bool(row["local"].get())
                enabled = bool(row["enabled"].get())
                sync_mode = str(row["sync_mode"].get())
                repo_path = Path(str(row["local_path"].get())).expanduser()
                targets_changed = repo_targets_changed(
                    selection,
                    wants_github=wants_github,
                    wants_local=wants_local,
                )

                if not self._row_has_no_targets(row):
                    updated_repos[selection.name] = RepoConfig(
                        name=selection.name,
                        local_path=str(repo_path),
                        sync_mode=sync_mode,
                        enabled=enabled,
                        sync_schedule=str(row["sync_schedule"].get()),
                        commit_message_mode=str(row["commit_message_mode"].get()),
                    )
                if not targets_changed:
                    continue

                if selected and selection.exists_remote and not wants_github:
                    self._delete_remote_for_apply(selection)
                if selected and selection.exists_local and not wants_local:
                    self._delete_local_for_apply(selection)
                if not wants_github and not wants_local:
                    continue

                remote_repo = self.remote_repos.get(selection.name)
                if wants_github and not wants_local:
                    if not selection.exists_remote:
                        remote_repo = self.github_client.create_repo(
                            selection.name,
                            description=repo_description(repo_path, selection.name),
                        )
                        self.remote_repos[selection.name] = remote_repo
                        self._save_remote_repo_cache()
                        self._enqueue_log(selection.name, "Created GitHub repo.")
                    continue

                if wants_local and not wants_github:
                    if not selection.exists_local:
                        repo_path.mkdir(parents=True, exist_ok=True)
                        self._enqueue_log(selection.name, f"Created local folder {repo_path}.")
                    continue

                if selection.exists_remote and not selection.exists_local:
                    clone_repo(
                        selection.clone_url or "",
                        repo_path,
                        token=self.config.github_token,
                    )
                    self._set_repo_identity(repo_path)
                    self._enqueue_log(selection.name, f"Cloned into {repo_path}.")
                    continue

                if selection.exists_local and not selection.exists_remote:
                    remote_repo = self.github_client.create_repo(
                        selection.name,
                        description=repo_description(repo_path, selection.name),
                    )
                    self.remote_repos[selection.name] = remote_repo
                    self._save_remote_repo_cache()
                    self._connect_local_repo_to_remote(repo_path, remote_repo)
                    self._enqueue_log(
                        selection.name,
                        "Created GitHub repo and uploaded local folder contents.",
                    )
                    continue

                if selection.exists_local and selection.exists_remote:
                    if selection.local_is_repo:
                        set_remote_url(repo_path, selection.clone_url or "")
                        self._set_repo_identity(repo_path)
                    else:
                        self._connect_non_repo_folder_to_existing_remote(repo_path, remote_repo)

            self.config.repos = updated_repos
            self.manual_folder_paths = {
                repo_name: Path(repo.local_path).expanduser()
                for repo_name, repo in self.config.repos.items()
                if repo.local_path and not self._is_inside_gitmo_folder(Path(repo.local_path))
            }
            self.pending_added_folder_names.clear()
            save_config(self.config)
        except (GitHubAPIError, GitCommandError) as exc:
            messagebox.showerror("GitMo", str(exc))
            return
        self._discard_page("dashboard")
        self._discard_page("repos")
        self.show_dashboard()

    def _connect_non_repo_folder_to_existing_remote(self, repo_path: Path, remote_repo: GitHubRepo | None) -> None:
        if remote_repo is None:
            raise GitCommandError("Missing GitHub repo details for selected folder.")

        if self._folder_is_empty(repo_path):
            clone_repo(remote_repo.clone_url, repo_path, token=self.config.github_token)
            self._set_repo_identity(repo_path)
            self._enqueue_log(remote_repo.name, f"Cloned GitHub repo into empty folder {repo_path}.")
            return

        overwrite_remote = messagebox.askyesno(
            "GitMo",
            f"GitHub repo '{remote_repo.name}' already exists, but '{repo_path}' is just a local folder.\n\n"
            "Yes: overwrite GitHub with the local folder contents.\n"
            "No: keep the current GitHub repo and leave this folder unmanaged.",
        )
        if not overwrite_remote:
            raise GitCommandError(
                f"Skipped linking {repo_path} because the existing GitHub repo was not allowed to be overwritten."
            )

        if not is_git_repo(repo_path):
            init_repo(repo_path)
        self._ensure_readme(repo_path, remote_repo.name)
        self._set_repo_identity(repo_path)
        prep_engine = SyncEngine(self.config, self._enqueue_log)
        prep_engine.prepare_new_remote_repo(
            repo_path,
            remote_repo.clone_url,
            force=True,
        )
        self._enqueue_log(remote_repo.name, f"Force-pushed local folder contents from {repo_path}.")

    def _connect_local_repo_to_remote(self, repo_path: Path, remote_repo: GitHubRepo) -> None:
        if not repo_path.exists():
            repo_path.mkdir(parents=True, exist_ok=True)
        if not is_git_repo(repo_path):
            init_repo(repo_path)
        self._ensure_readme(repo_path, remote_repo.name)
        self._set_repo_identity(repo_path)
        prep_engine = SyncEngine(self.config, self._enqueue_log)
        prep_engine.prepare_new_remote_repo(repo_path, remote_repo.clone_url)

    def _ensure_readme(self, repo_path: Path, repo_name: str) -> None:
        if any(path.is_file() and path.name.lower().startswith("readme") for path in repo_path.iterdir()):
            return
        readme_path = repo_path / "README.md"
        title = repo_name.replace("-", " ").replace("_", " ").strip() or repo_name
        readme_path.write_text(f"# {title}\n", encoding="utf-8")

    def _set_repo_identity(self, repo_path: Path) -> None:
        set_local_identity(
            repo_path,
            self.config.github_login or "GitMo",
            self.config.github_email or "gitmo@users.noreply.github.com",
        )

    def _shorten_path(self, path: Path) -> str:
        text = str(path)
        home = str(Path.home())
        if text.startswith(home):
            text = text.replace(home, "~", 1)
        return text

    def _shorten_folder_parent(self, path: Path) -> str:
        """Show where a folder lives without repeating the repo/folder name."""
        parent = path.expanduser().parent
        home = Path.home()
        try:
            relative = parent.relative_to(home)
        except ValueError:
            parts = parent.parts
            if len(parts) <= 3:
                return str(parent)
            return f"{Path(*parts[:3])}/..."

        if not relative.parts:
            return "~/"
        if len(relative.parts) == 1:
            return f"~/home/{relative.parts[0]}/..."
        shown_parts = relative.parts[:2]
        return f"~/{'/'.join(shown_parts)}/..."

    def _folder_is_empty(self, path: Path) -> bool:
        return path.exists() and path.is_dir() and not any(path.iterdir())

    def _manual_sync(self) -> None:
        try:
            self.is_paused = False
            self.sync_engine.run_once(force_autosave=True)
            self._refresh_statuses()
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("GitMo", str(exc))

    def _view_logs(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.touch(exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])
        except OSError as exc:
            messagebox.showerror("GitMo", f"Could not open log file:\n{exc}\n\n{LOG_PATH}")

    def _copy_log_path(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(str(LOG_PATH))

    def _clear_log_file(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with self.log_lock:
            LOG_PATH.write_text("", encoding="utf-8")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About GitMo",
            "\n".join(
                [
                    "GitMo",
                    "",
                    "GitMo keeps selected local folders connected to GitHub repos.",
                    "It is meant for simple personal backup/sync workflows, not complex team merge workflows.",
                    "",
                    "Basic flow:",
                    "1. Sign in with a GitHub token.",
                    "2. Choose a GitMo folder.",
                    "3. Open Manage Repos.",
                    "4. Pick where each project should exist: GitHub, Local Folder, or both.",
                    "5. Click Apply Selection.",
                    "",
                    "Manage Repos:",
                    "- GitHub check means keep/create/sync that repo on GitHub.",
                    "- Local Folder check means keep/create/sync that folder on this computer.",
                    "- Both checked means GitMo links the local folder and GitHub repo.",
                    "- GitHub only means the repo can stay remote only.",
                    "- Local only means the folder can stay local only.",
                    "- Both unchecked on a selected row means delete/remove everywhere after confirmation.",
                    "",
                    "When names match:",
                    "- GitMo matches repos and folders by name.",
                    "- If GitHub exists but local is missing, GitMo clones it locally.",
                    "- If local exists but GitHub is missing, GitMo creates the GitHub repo and pushes it.",
                    "- If both exist and the local folder is already a git repo, GitMo connects it to GitHub.",
                    "- If both exist but the local folder is plain/non-git, GitMo asks before overwriting GitHub.",
                    "",
                    "Sync modes:",
                    "- One-way: local changes are committed and pushed. Remote changes are warned about but not pulled.",
                    "- Two-way: GitMo fetches GitHub changes and pulls only safe fast-forward updates.",
                    "",
                    "Conflicts:",
                    "- GitMo checks git commit history, not just file names.",
                    "- If local and GitHub both changed, GitMo marks a conflict instead of guessing which side wins.",
                    "- Conflicts need manual git resolution.",
                    "",
                    "Autosave:",
                    "- GitMo watches enabled repos.",
                    "- After files are quiet for about 60 seconds, it commits with 'GitMo autosave' and pushes.",
                    "",
                    "App data:",
                    f"- Config: {APP_DIR / 'config.json'}",
                    f"- Log: {LOG_PATH}",
                ]
            ),
        )

    def _refresh_statuses(self) -> None:
        self.status_refresh_after_id = None
        if self.current_screen != "dashboard":
            return
        if self.is_paused:
            self.last_checked_var.set(f"Last checked {datetime.now().strftime('%H:%M:%S')}")
            self._schedule_status_refresh(2000)
            return
        for repo_name, status_var in self.status_vars.items():
            status = self.sync_engine.status_for(repo_name)
            text, color = self._status_text_and_color(status.state, status.detail)
            status_var.set(text)
            last_sync_var = self.last_sync_vars.get(repo_name)
            if last_sync_var and status.last_sync_at:
                last_sync_var.set(datetime.fromtimestamp(status.last_sync_at).strftime("%H:%M:%S"))
            self._refresh_dashboard_tree_row(repo_name)
        latest_sync_at = max(
            (status.last_sync_at for status in self.sync_engine.repo_statuses.values()),
            default=0.0,
        )
        if latest_sync_at:
            self.header_last_sync_var.set(
                f"Last Sync: {datetime.fromtimestamp(latest_sync_at).strftime('%H:%M:%S')}"
            )
        self.last_checked_var.set(f"Last checked {datetime.now().strftime('%H:%M:%S')}")
        if any(status.state == "error" for status in self.sync_engine.repo_statuses.values()):
            self.header_git_var.set("Git: Needs attention")
        else:
            self.header_git_var.set("Git: OK")
        self._refresh_dashboard_summary()
        self._refresh_log_view()
        self._schedule_status_refresh(2000)

    def _schedule_status_refresh(self, delay_ms: int) -> None:
        if self.status_refresh_after_id is not None:
            self.root.after_cancel(self.status_refresh_after_id)
        if self.root.winfo_exists() and self.current_screen == "dashboard":
            self.status_refresh_after_id = self.root.after(
                delay_ms,
                self._refresh_statuses,
            )

    def _enqueue_log(self, repo_name: str, message: str) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_lock:
            with LOG_PATH.open("a", encoding="utf-8") as log_file:
                log_file.write(f"{timestamp} {repo_name}: {message}\n")

    def _on_close(self) -> None:
        if self._has_enabled_repos():
            keep_running = messagebox.askyesno(
                "GitMo",
                "Keep GitMo running in the background?\n\nYes: keep syncing in the background.\nNo: stop syncing and close GitMo.",
            )
            if keep_running:
                self._run_in_background()
                return
        self.sync_engine.stop()
        if self.tray_thread_started:
            self.tray_command_queue.put("quit")
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    start_background = "--background" in args
    root = tk.Tk()
    app = GitMoApp(root)
    app.run()
    if start_background and app._has_enabled_repos():
        root.after(250, app._run_in_background)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
