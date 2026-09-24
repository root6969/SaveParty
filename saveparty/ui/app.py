"""SaveParty's graphical interface (CustomTkinter), gaming-styled.

Threading model: every engine operation runs in a worker thread; the engine
reports through UIEvents, which posts messages to a queue the Tk main loop
drains via `after()`. Blocking questions (confirm / conflict) park the worker
on a threading.Event until the user clicks a button in a modal dialog.
"""

from __future__ import annotations

import queue
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import __version__
from ..backup import list_backups
from ..detector import (
    cloud_folder_candidates,
    compatibility_check,
    detect_games,
    save_candidates,
)
from ..engine import SyncEngine
from ..game import list_running_processes
from ..profiles import Profile, ProfileStore
from ..util import SavePartyError, human_size, slugify


# ----------------------------------------------------------------------
# style
# ----------------------------------------------------------------------

BG = "#101014"
PANEL = "#17171d"
PANEL_2 = "#1e1e26"
EDGE = "#2a2a35"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#6847e8"
TEXT_DIM = "#8b8b99"
GOOD = "#2fe08d"
WARN = "#ffb454"
BAD = "#ff5c7a"

STATE_COLORS = {
    "idle": GOOD,
    "syncing": "#4aa8ff",
    "waiting": WARN,
    "playing": "#b07cff",
    "behind": WARN,
    "ahead": WARN,
    "conflict": BAD,
    "error": BAD,
}

LOG_COLORS = {
    "info": "#c9c9d4",
    "dim": TEXT_DIM,
    "success": GOOD,
    "warn": WARN,
    "error": BAD,
}


def _rgb(hexstr: str) -> tuple:
    hexstr = hexstr.lstrip("#")
    return tuple(int(hexstr[i : i + 2], 16) for i in (0, 2, 4))


def font(size: int, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal")


class PulseLabel(ctk.CTkLabel):
    """A label that cycles through pre-rendered image frames.

    Used for the glowing logo and the author credit. Cheap to run: the frames
    are tiny images rendered once and swapped on a timer.
    """

    def __init__(self, master, frames, period_ms: int = 100):
        super().__init__(master, image=frames[0], text="")
        self._frames = frames
        self._index = 0
        self._period = period_ms
        self.after(period_ms, self._tick)

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        self._index = (self._index + 1) % len(self._frames)
        self.configure(image=self._frames[self._index])
        self.after(self._period, self._tick)


def ghost_button(master, **kw) -> ctk.CTkButton:
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("hover_color", PANEL_2)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", EDGE)
    kw.setdefault("corner_radius", 10)
    return ctk.CTkButton(master, **kw)


class UIEvents:
    """Engine events implementation bridging a worker thread to the UI."""

    def __init__(self, app: "App", profile_id: str):
        self.app = app
        self.profile_id = profile_id

    def _post(self, kind: str, **payload) -> None:
        self.app.bus.put((kind, self.profile_id, payload))

    def log(self, text: str, style: str = "info") -> None:
        self._post("log", text=text, style=style)

    def status(self, state: str, text: str) -> None:
        self._post("status", state=state, text=text)

    def _ask(self, kind: str, **payload):
        done = threading.Event()
        box: dict = {}
        self._post(kind, done=done, box=box, **payload)
        done.wait()
        return box.get("result")

    def confirm(self, text: str, default: bool = True) -> bool:
        result = self._ask("confirm", text=text, default=default)
        return bool(result) if result is not None else default

    def resolve_conflict(self, info: dict) -> str:
        return self._ask("conflict", info=info) or "defer"


# ----------------------------------------------------------------------
# app shell
# ----------------------------------------------------------------------


class App(ctk.CTk):
    """The main window: a sidebar with one entry per synced game, and a main
    area showing either a game's page or the add-game wizard.

    All slow work happens on worker threads; the UI thread only draws and
    answers dialogs, so the window never freezes during a sync or session.
    """

    def __init__(self):
        super().__init__(fg_color=BG)
        ctk.set_appearance_mode("dark")
        self.title(f"SaveParty {__version__} - co-op save sync")
        self.geometry("1120x700")
        self.minsize(980, 620)
        # CustomTkinter applies its own geometry and icon shortly after start,
        # so maximizing and our icon must be scheduled to land AFTER that.
        self._smoke = False
        # Open maximized. CustomTkinter (and the slower frozen-exe startup) reset
        # the geometry shortly after launch, so a fixed set of early calls can all
        # land before that reset and get undone. Instead we RE-ASSERT "zoomed" on a
        # short repeating tick until it sticks, then stop so the user can restore.
        self._want_maximized = True
        self.after(60, self._ensure_maximized)
        try:
            from PIL import ImageTk

            from ..profiles import data_home
            from .art import app_icon, save_ico

            self._app_icon = ImageTk.PhotoImage(app_icon())
            self._ico_path = data_home() / "saveparty.ico"
            data_home().mkdir(parents=True, exist_ok=True)
            save_ico(self._ico_path)
            self.after(450, self._apply_icon)
        except Exception:
            pass

        self.store = ProfileStore().load()
        self._maybe_start_syncthing()
        self.bus: "queue.Queue" = queue.Queue()
        self.busy: set[str] = set()
        self.stop_events: dict[str, threading.Event] = {}
        self.pages: dict[str, GamePage] = {}
        self.sidebar_dots: dict[str, ctk.CTkLabel] = {}
        self.current_page = None

        self._build_sidebar()
        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main.pack(side="left", fill="both", expand=True)

        if self.store.profiles:
            self.show_home()
        else:
            self.show_add_game()

        self._start_artwork_prefetch()
        self.after(600, self._check_for_update)

    def _check_for_update(self) -> None:
        """On startup, look in the shared folder(s) for a newer SaveParty build
        and, if one is there, offer to install it. Background read; the dialog
        opens back on the main thread."""
        if getattr(self, "_smoke", False):
            return

        def work():
            try:
                from .. import update

                found = update.available_update(self.store.profiles)
            except Exception:
                found = None
            if found:
                info, cloud = found
                self.after(0, lambda: self._prompt_update(info, cloud))

        threading.Thread(target=work, daemon=True).start()

    def _prompt_update(self, info, cloud_dir: str) -> None:
        from .. import update

        lines = [f"A new version of SaveParty is available: {info.version} "
                 f"(you have {__version__})."]
        if info.published_by:
            lines.append(f"Published by {info.published_by}.")
        if info.notes:
            lines += ["", info.notes]
        if info.mandatory:
            lines += ["", "This update is required to keep playing together."]
        lines += ["", "Download and install it now?"]
        if not ConfirmDialog.ask_now(self, "\n".join(lines), default=True):
            if info.mandatory:
                messagebox.showwarning(
                    "SaveParty",
                    "This version is required to keep playing together.\n"
                    "SaveParty will now close - reopen it when you're ready to update.",
                )
                self.destroy()
            return
        if not update.running_frozen():
            messagebox.showinfo(
                "SaveParty",
                "You're running SaveParty from source, so it can't replace itself.\n"
                "Rebuild/reinstall to update.",
            )
            if info.mandatory:
                self.destroy()
            return
        if update.apply_update(info, cloud_dir):
            self.destroy()  # exit so the swap script can replace the exe and relaunch
        else:
            messagebox.showwarning(
                "SaveParty",
                "The new version isn't fully downloaded to this PC yet "
                "(Syncthing is still syncing it). Try again in a minute.",
            )
            if info.mandatory:
                self.destroy()

    def _start_artwork_prefetch(self) -> None:
        """Download official Steam art in the background; refresh once it's in."""
        try:
            from .artwork import prefetch, steam_appid
        except Exception:
            return
        appids = [steam_appid(p.game_id) for p in self.store.profiles]
        if not any(appids):
            return

        def work():
            prefetch(appids)
            self.after(0, self._on_artwork_ready)

        threading.Thread(target=work, daemon=True).start()

    def _on_artwork_ready(self) -> None:
        self._rebuild_profile_buttons()
        page = self.current_page
        if isinstance(page, HomePage):
            self.show_home()
        elif isinstance(page, GamePage):
            self.show_game(page.profile.id)

        self.after(100, self._drain_bus)
        self.after(1000, self._periodic_refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _ensure_maximized(self, tries: int = 0) -> None:
        """Keep re-applying the maximized state until it holds (robust against
        CustomTkinter / frozen-exe geometry resets that land after startup).
        Re-asserts for ~2.5s, then stops so the user can freely un-maximize."""
        if self._smoke or not getattr(self, "_want_maximized", False):
            return
        try:
            if self.state() == "withdrawn":
                pass  # not mapped yet; just retry on the next tick
            elif self.state() != "zoomed":
                self.state("zoomed")
                if tries == 0:
                    self.lift()  # bring it to the front on first show
        except Exception:
            try:
                self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight() - 48}+0+0")
            except Exception:
                pass
        if tries < 12:
            self.after(200, lambda: self._ensure_maximized(tries + 1))

    def _apply_icon(self) -> None:
        """Set the window/taskbar icon after CustomTkinter's own icon lands."""
        try:
            self.iconbitmap(str(self._ico_path))
            self.iconphoto(True, self._app_icon)
        except Exception:
            pass

    def _maybe_start_syncthing(self) -> None:
        """If enabled, make sure Syncthing is running (start it via its own autostart
        shortcut if it isn't) so the shared folder actually syncs. Background; never
        blocks, and never starts a second copy if one's already up."""
        if not getattr(self.store, "auto_start_syncthing", True):
            return

        def work():
            try:
                from .. import syncthing

                syncthing.ensure_running()
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    # -- sidebar --------------------------------------------------------

    def _build_sidebar(self) -> None:
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=PANEL)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        try:
            from .art import logo_frames

            PulseLabel(self.sidebar, logo_frames(), period_ms=100).pack(pady=(20, 0))
        except Exception:
            logo = ctk.CTkFrame(self.sidebar, fg_color="transparent")
            logo.pack(pady=(22, 2))
            ctk.CTkLabel(logo, text="Save", font=font(26, True), text_color="white").pack(side="left")
            ctk.CTkLabel(logo, text="Party", font=font(26, True), text_color=ACCENT).pack(side="left")
        ctk.CTkLabel(
            self.sidebar, text="CO-OP SAVE SYNC", font=font(10, True), text_color=TEXT_DIM
        ).pack(pady=(0, 10))

        ctk.CTkButton(
            self.sidebar, text="🏠    Home", height=38, corner_radius=10, anchor="w",
            fg_color="transparent", hover_color=EDGE, border_width=1, border_color=EDGE,
            font=font(13, True), command=self.show_home,
        ).pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(
            self.sidebar, text="YOUR GAMES", font=font(10, True), text_color=TEXT_DIM
        ).pack(anchor="w", padx=18)
        self.profile_list = ctk.CTkScrollableFrame(self.sidebar, fg_color="transparent")
        self.profile_list.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        ctk.CTkButton(
            self.sidebar,
            text="+   Add game",
            height=40,
            corner_radius=10,
            font=font(14, True),
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.show_add_game,
        ).pack(fill="x", padx=14, pady=10)

        name_row = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        name_row.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(name_row, text="Player", text_color=TEXT_DIM).pack(side="left")
        self.player_entry = ctk.CTkEntry(name_row, fg_color=PANEL_2, border_color=EDGE)
        self.player_entry.insert(0, self.store.player_name)
        self.player_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.player_entry.bind("<FocusOut>", lambda _e: self._save_player_name())
        try:
            from .art import credit_frames

            PulseLabel(self.sidebar, credit_frames(), period_ms=150).pack(pady=(0, 0))
        except Exception:
            ctk.CTkLabel(
                self.sidebar, text="Created by Rusu", text_color="#6ea8ff", font=font(14, True)
            ).pack(pady=(0, 0))
        ctk.CTkLabel(
            self.sidebar, text=f"v{__version__}", text_color=TEXT_DIM, font=font(10)
        ).pack(pady=(0, 10))
        self._rebuild_profile_buttons()

    def _save_player_name(self) -> None:
        name = self.player_entry.get().strip()
        if name and name != self.store.player_name:
            self.store.player_name = name
            self.store.save()

    def _rebuild_profile_buttons(self) -> None:
        for child in self.profile_list.winfo_children():
            child.destroy()
        self.sidebar_dots.clear()
        try:
            from .art import chip
        except Exception:
            chip = None
        from . import artwork
        for profile in self.store.profiles:
            row = ctk.CTkFrame(self.profile_list, fg_color=PANEL_2, corner_radius=10)
            row.pack(fill="x", pady=3)
            dot = ctk.CTkLabel(row, text="●", width=18, text_color=TEXT_DIM, font=font(13))
            dot.pack(side="left", padx=(8, 0))
            self.sidebar_dots[profile.id] = dot
            image = artwork.icon(artwork.steam_appid(profile.game_id), 26)
            if image is None and chip:
                image = chip(profile.game_id or profile.title, profile.title[:1], 26)
            ctk.CTkButton(
                row,
                text=f"  {profile.title}",
                image=image,
                compound="left",
                anchor="w",
                fg_color="transparent",
                hover_color=EDGE,
                font=font(13),
                command=lambda pid=profile.id: self.show_game(pid),
            ).pack(side="left", fill="x", expand=True, pady=2, padx=(2, 6))

    def set_sidebar_dot(self, profile_id: str, state: str) -> None:
        dot = self.sidebar_dots.get(profile_id)
        if dot:
            dot.configure(text_color=STATE_COLORS.get(state, TEXT_DIM))

    # -- page switching -------------------------------------------------

    def _clear_main(self) -> None:
        for child in self.main.winfo_children():
            child.destroy()
        self.current_page = None

    def show_home(self) -> None:
        self._clear_main()
        page = HomePage(self.main, self)
        page.pack(fill="both", expand=True, padx=16, pady=16)
        self.current_page = page

    def show_game(self, profile_id: str) -> None:
        profile = self.store.get(profile_id)
        if profile is None:
            return
        self._clear_main()
        page = GamePage(self.main, self, profile)
        page.pack(fill="both", expand=True, padx=16, pady=16)
        self.pages[profile_id] = page
        self.current_page = page
        page.refresh_async()

    def show_add_game(self) -> None:
        self._clear_main()
        page = AddGamePage(self.main, self)
        page.pack(fill="both", expand=True, padx=16, pady=16)
        self.current_page = page

    # -- worker plumbing ------------------------------------------------

    def run_task(self, profile: Profile, fn, *args, on_done=None) -> None:
        """Run one engine operation for a profile on a background thread.

        Only one operation per profile at a time; buttons are disabled while
        it runs and errors land in the game's activity log instead of crashing.
        """
        if profile.id in self.busy:
            return
        self.busy.add(profile.id)
        page = self.pages.get(profile.id)
        if page:
            page.set_busy(True)

        def worker():
            try:
                fn(*args)
            except SavePartyError as exc:
                self.bus.put(("log", profile.id, {"text": str(exc), "style": "error"}))
                self.bus.put(("status", profile.id, {"state": "error", "text": "Error - see log"}))
            except Exception as exc:  # never kill the worker silently
                self.bus.put(("log", profile.id, {"text": f"Unexpected error: {exc}", "style": "error"}))
                self.bus.put(("status", profile.id, {"state": "error", "text": "Error - see log"}))
            finally:
                self.bus.put(("task_done", profile.id, {"on_done": on_done}))

        threading.Thread(target=worker, daemon=True).start()

    def engine_for(self, profile: Profile) -> SyncEngine:
        return SyncEngine(profile, self.store.player_name, UIEvents(self, profile.id))

    # -- event bus ------------------------------------------------------

    def _drain_bus(self) -> None:
        """Pump messages from worker threads into the UI (runs every 100 ms).

        Log lines and status changes update the page; confirm/conflict
        requests open a modal dialog whose answer wakes the waiting worker.
        """
        try:
            while True:
                kind, profile_id, payload = self.bus.get_nowait()
                page = self.pages.get(profile_id)
                if kind == "log" and page:
                    page.append_log(payload["text"], payload["style"])
                elif kind == "status":
                    if page:
                        page.set_status(payload["state"], payload["text"])
                    self.set_sidebar_dot(profile_id, payload["state"])
                elif kind == "confirm":
                    ConfirmDialog(self, payload)
                elif kind == "conflict":
                    ConflictDialog(self, payload)
                elif kind == "task_done":
                    self.busy.discard(profile_id)
                    self.stop_events.pop(profile_id, None)
                    if page:
                        page.set_busy(False)
                        page.refresh_async()
                    if payload.get("on_done"):
                        payload["on_done"]()
        except queue.Empty:
            pass
        self.after(100, self._drain_bus)

    def _periodic_refresh(self) -> None:
        page = self.current_page
        if isinstance(page, GamePage) and page.profile.id not in self.busy:
            page.refresh_async(quiet=True)
        self.after(20000, self._periodic_refresh)

    def _on_close(self) -> None:
        if self.busy:
            if not ConfirmDialog.ask_now(
                self,
                "A sync or session is still running - closing now may leave the cloud "
                "lock behind. Close anyway?",
                default=False,
            ):
                return
        self.destroy()


# ----------------------------------------------------------------------
# game page
# ----------------------------------------------------------------------


class HomePage(ctk.CTkFrame):
    """SaveSync-style Games Library: a grid of the user's game cards."""

    def __init__(self, master, app: "App"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        ctk.CTkLabel(self, text="Games Library", font=font(30, True)).pack(anchor="w")
        n = len(app.store.profiles)
        ctk.CTkLabel(
            self,
            text=f"{n} game{'s' if n != 1 else ''} synced through your hub",
            text_color=TEXT_DIM, font=font(12),
        ).pack(anchor="w", pady=(0, 14))

        grid = ctk.CTkScrollableFrame(self, fg_color="transparent")
        grid.pack(fill="both", expand=True)
        per_row = 3
        entries = list(app.store.profiles) + [None]  # trailing None = the "Add game" card
        row = None
        for i, profile in enumerate(entries):
            if i % per_row == 0:
                row = ctk.CTkFrame(grid, fg_color="transparent")
                row.pack(fill="x", pady=7, anchor="w")
            self._card(row, profile)

    def _card(self, parent, profile) -> None:
        card = ctk.CTkFrame(
            parent, fg_color=PANEL, corner_radius=16, border_width=1, border_color=EDGE,
            width=300, height=196,
        )
        card.pack(side="left", padx=8)
        card.pack_propagate(False)

        if profile is None:
            ctk.CTkButton(
                card, text="+\n\nAdd game", font=font(16, True), fg_color="transparent",
                hover_color=EDGE, text_color=TEXT_DIM, command=self.app.show_add_game,
            ).pack(fill="both", expand=True, padx=2, pady=2)
            return

        from . import artwork

        img = artwork.banner(artwork.steam_appid(profile.game_id), 292, 122)
        if img is None:
            try:
                from .art import banner

                img = banner(profile.game_id or profile.title, profile.title[:1], width=292, height=122)
            except Exception:
                img = None
        if img is not None:
            ctk.CTkLabel(card, text="", image=img).pack(fill="x", padx=4, pady=(4, 0))

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(6, 0))
        ctk.CTkLabel(bottom, text=profile.title, font=font(15, True), anchor="w").pack(side="left")
        dot = ctk.CTkLabel(bottom, text="●", font=font(13), text_color=TEXT_DIM)
        dot.pack(side="right")
        self.app.sidebar_dots.setdefault(profile.id, dot)  # let status updates tint it too
        ctk.CTkButton(
            card, text="Open  ›", height=28, corner_radius=8, font=font(12, True),
            fg_color=PANEL_2, hover_color=EDGE, command=lambda p=profile: self.app.show_game(p.id),
        ).pack(fill="x", padx=12, pady=(6, 10))
        # Whole-card click also opens it.
        for w in (card,):
            w.bind("<Button-1>", lambda _e, p=profile: self.app.show_game(p.id))


class GamePage(ctk.CTkFrame):
    """One synced game: banner, live status pill, the big Play button, the
    tool rows (sync/pull/push/backups/…), and the activity log."""

    def __init__(self, master, app: App, profile: Profile):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.profile = profile

        # ---- HERO: full art + Steam-style semi-transparent bar. The title,
        # status, badges and stats are baked INTO the image (over a blurred,
        # darkened band you can still see the art through); only the buttons
        # are overlaid widgets (solid, so no transparent-frame boxes).
        from . import artwork

        appid = artwork.steam_appid(profile.game_id)
        self._appid = appid
        self._banner_w = 0
        self._hero_title = profile.title
        self._hero_badges = [("Supported", _rgb(GOOD))]
        if profile.game_id == "palworld":
            self._hero_badges.append(("Host Migration", _rgb(ACCENT)))
        self._hero_status = ("Loading…", _rgb(TEXT_DIM))
        self._hero_stats = [("…", "LOCAL SAVE"), ("…", "ON THE HUB"), ("…", "BACKUPS"), ("…", "SYNC")]

        hero = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16, height=260)
        hero.pack(fill="x")
        hero.pack_propagate(False)
        self.hero = hero
        self.banner_label = ctk.CTkLabel(hero, text="")
        self.banner_label.place(x=0, y=0, relwidth=1, relheight=1)
        self._render_hero(1180)
        hero.bind("<Configure>", self._on_hero_resize)

        # ---- ACTION BAR (Steam-style: PLAY + ▾ dropdown + right icons, overlaid)
        action = ctk.CTkFrame(hero, fg_color="transparent")
        action.place(relx=0, x=22, rely=1.0, y=-14, anchor="sw")
        self.play_btn = ctk.CTkButton(
            action, text="▶   PLAY SESSION", height=46, width=188, corner_radius=11,
            font=font(15, True), fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.on_play,
        )
        self.play_btn.pack(side="left")
        self.more_btn = ctk.CTkButton(
            action, text="▾", width=36, height=46, corner_radius=11, font=font(16, True),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._open_actions_menu,
        )
        self.more_btn.pack(side="left", padx=(3, 0))
        self.end_btn = ctk.CTkButton(
            action, text="■   End", height=46, corner_radius=11, font=font(14, True),
            fg_color=BAD, hover_color="#d64a66", command=self.on_end_session,
        )

        # Right-aligned compact buttons, on the bottom-right of the banner.
        rightbar = ctk.CTkFrame(hero, fg_color="transparent")
        rightbar.place(relx=1.0, x=-20, rely=1.0, y=-14, anchor="se")
        self.action_buttons = [self.more_btn]
        self.settings_btn = ghost_button(rightbar, text="⚙", width=46, height=46, font=font(16), command=self.on_settings)
        self.settings_btn.pack(side="right")
        self.action_buttons.append(self.settings_btn)
        if profile.game_id == "palworld":
            self.characters_btn = ghost_button(rightbar, text="🎭", width=46, height=46, font=font(15), command=self.on_characters)
            self.characters_btn.pack(side="right", padx=(0, 8))
            self.action_buttons.append(self.characters_btn)
        self.sync_btn = ghost_button(rightbar, text="⟳  Sync", width=104, height=46, font=font(13, True), command=self.on_sync)
        self.sync_btn.pack(side="right", padx=(0, 8))
        self.action_buttons.append(self.sync_btn)

        log_card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16, border_width=1, border_color=EDGE)
        log_card.pack(fill="both", expand=True)
        ctk.CTkLabel(
            log_card, text="ACTIVITY", font=font(10, True), text_color=TEXT_DIM
        ).pack(anchor="w", padx=16, pady=(10, 0))
        self.log_box = ctk.CTkTextbox(
            log_card, wrap="word", state="disabled", fg_color=PANEL,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        for style, color in LOG_COLORS.items():
            self.log_box.tag_config(style, foreground=color)
        self.append_log(f"Saves: {profile.save_dir}", "dim")
        self.append_log(f"Hub folder: {profile.cloud_dir}", "dim")

    # -- UI helpers -----------------------------------------------------

    def _on_hero_resize(self, event) -> None:
        w = max(420, event.width - 12)
        if abs(w - self._banner_w) < 40:
            return
        job = getattr(self, "_banner_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._banner_job = self.after(120, lambda: self._render_hero(w))

    def _render_hero(self, w: int) -> None:
        """Bake the full hero: art + a blurred semi-transparent band carrying the
        title, status, badges and stats. Only the buttons are overlaid on top.

        `w` is a PHYSICAL pixel width (tkinter width). We render the art at that
        resolution and hand CTkImage a scaled-down size so it displays 1:1."""
        from . import artwork

        self._banner_w = w
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        aspect = artwork.hero_aspect(self._appid) or 0.34
        h = max(int(300 * scaling), min(int(w * aspect), int(440 * scaling)))
        band_h = int(h * 0.42)  # proportional band → consistent layout at any size
        left_reserve = int(300 * scaling)  # keep stats clear of PLAY + ▾ (≈245 logical)
        img = artwork.hero_bar(
            self._appid, w, h, band_h, self._hero_title,
            self._hero_status[0], self._hero_status[1],
            self._hero_badges, self._hero_stats,
            scaling=scaling, left_reserve=left_reserve,
        )
        if img is not None and self.banner_label.winfo_exists():
            self.banner_label.configure(image=img)
        if getattr(self, "hero", None) is not None and self.hero.winfo_exists():
            self.hero.configure(height=int(round(h / scaling)))

    @staticmethod
    def _short_cloud(cloud: str) -> str:
        if not cloud or "empty" in cloud.lower():
            return "empty"
        import re

        m = re.search(r"generation (\d+)", cloud)
        return f"gen {m.group(1)}" if m else cloud.split("·")[0].strip()[:14]

    def append_log(self, text: str, style: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {text}\n", style)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, state: str, text: str) -> None:
        self._hero_status = (text, _rgb(STATE_COLORS.get(state, "#4aa8ff")))
        self._render_hero(self._banner_w or 1180)

    def set_busy(self, busy: bool) -> None:
        in_session = self.profile.id in self.app.stop_events
        state = "disabled" if busy else "normal"
        self.play_btn.configure(state="disabled" if (busy or in_session) else "normal")
        for btn in self.action_buttons:
            btn.configure(state=state)
        if in_session:
            self.end_btn.pack(side="left", padx=(0, 20), after=self.more_btn)
            self.end_btn.configure(state="normal")
        else:
            self.end_btn.pack_forget()

    def _open_actions_menu(self) -> None:
        """Steam-style dropdown for the less-common actions (kept off the main bar)."""
        import tkinter as tk

        menu = tk.Menu(
            self, tearoff=0, bg=PANEL_2, fg="#e6e6ee", activebackground=ACCENT,
            activeforeground="white", bd=0, font=("Segoe UI", 10),
        )
        menu.add_command(label="  ⬇  Pull latest from hub", command=lambda: self.on_pull(False))
        menu.add_command(label="  ⬆  Push to hub", command=lambda: self.on_push(False))
        menu.add_separator()
        menu.add_command(label="  ⬇  Pull (force - overwrite local)", command=lambda: self.on_pull(True))
        menu.add_command(label="  ⬆  Push (force - overwrite hub)", command=lambda: self.on_push(True))
        menu.add_separator()
        menu.add_command(label="  🗀  Backups…", command=self.on_backups)
        menu.add_command(label="  🔓  Unlock the hub", command=self.on_unlock)
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def refresh_async(self, quiet: bool = False) -> None:
        """Re-read local/cloud/lock state on a thread and update the header."""
        profile = self.profile

        def work():
            engine = self.app.engine_for(profile)
            summary = engine.status_summary()
            self.app.bus.put(
                ("status", profile.id, {"state": summary["verdict"], "text": summary["status_text"]})
            )
            local = f"{summary['local_size']}" if summary["local_files"] else "empty"
            cloud = self._short_cloud(summary.get("cloud", ""))
            backups = str(summary["backups"])
            # Syncthing readiness (blank when it's a non-Syncthing folder / unreachable)
            try:
                from .. import syncthing

                st = syncthing.status(profile.cloud_dir)
                sync = "up to date" if st.up_to_date else (f"{st.need_items} left" if st.need_items else (st.state or "syncing"))
                sync = sync if st.managed else "-"
            except Exception:
                sync = "-"

            def apply():
                self._hero_stats = [(local, "LOCAL SAVE"), (cloud, "ON THE HUB"), (backups, "BACKUPS"), (sync, "SYNC")]
                self._render_hero(self._banner_w or 1180)

            self.app.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    # -- actions --------------------------------------------------------

    def on_play(self) -> None:
        stop_event = threading.Event()
        self.app.stop_events[self.profile.id] = stop_event
        engine = self.app.engine_for(self.profile)
        if not self.profile.process_names:
            self.append_log(
                "No process detection for this game - click 'End session' when you stop playing.",
                "warn",
            )
        self.app.run_task(self.profile, engine.play_session, stop_event)
        self.set_busy(True)

    def on_end_session(self) -> None:
        event = self.app.stop_events.get(self.profile.id)
        if event:
            event.set()
            self.append_log("Ending the session - syncing your progress…", "info")

    def on_sync(self) -> None:
        self.app.run_task(self.profile, self.app.engine_for(self.profile).reconcile)

    def on_pull(self, force: bool) -> None:
        self.app.run_task(self.profile, self.app.engine_for(self.profile).manual_pull, force)

    def on_push(self, force: bool) -> None:
        self.app.run_task(self.profile, self.app.engine_for(self.profile).manual_push, force)

    def on_unlock(self) -> None:
        engine = self.app.engine_for(self.profile)
        info = engine.lock.read()
        if info is None:
            self.append_log("The cloud lock is already free.", "success")
            return
        stale = engine.lock.is_stale(info)
        text = (
            f"The lock is held by {info.player}"
            + (" and looks STALE (crashed session)." if stale else " and looks ACTIVE - they may be playing right now!")
            + " Release it?"
        )
        if ConfirmDialog.ask_now(self.app, text, default=stale):
            engine.lock.force_release()
            self.append_log("Lock released.", "success")
            self.refresh_async()

    def on_backups(self) -> None:
        BackupsDialog(self.app, self)

    def on_characters(self) -> None:
        CharactersDialog(self.app, self)

    def on_settings(self) -> None:
        SettingsDialog(self.app, self)


# ----------------------------------------------------------------------
# add game page
# ----------------------------------------------------------------------


class AddGamePage(ctk.CTkFrame):
    """The add-game wizard: a searchable game list (detected games first),
    the save browser (worlds/slots with last-played dates), the compatibility
    checklist, the shared-folder picker, and a fixed CREATE PROFILE bar."""

    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.selected_game = None
        self.selected_save: Path | None = None
        self.save_rows: list[tuple[ctk.CTkFrame, Path]] = []
        self.detections = detect_games()
        self.mode = "seed"  # "seed" = I have the world; "join" = pull an existing shared world

        ctk.CTkLabel(self, text="🎮  Add a game", font=font(28, True)).pack(anchor="w")
        self._wrap_labels: list = []
        self._last_wrap = 540
        subtitle = ctk.CTkLabel(
            self,
            text="Games found on this PC are listed first. SaveParty locates the saves, checks compatibility, and sets up the shared folder.",
            text_color=TEXT_DIM, font=font(12), wraplength=self._last_wrap, justify="left",
        )
        subtitle.pack(anchor="w", pady=(0, 10))
        self._wrap_labels.append(subtitle)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        left = ctk.CTkFrame(body, width=270, fg_color=PANEL, corner_radius=16, border_width=1, border_color=EDGE)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self.search = ctk.CTkEntry(
            left, placeholder_text="🔍  Search games…", fg_color=PANEL_2, border_color=EDGE
        )
        self.search.pack(fill="x", padx=10, pady=10)
        self.search.bind("<KeyRelease>", lambda _e: self._fill_games())
        self.games_frame = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.games_frame.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self._fill_games()

        right_container = ctk.CTkFrame(body, fg_color="transparent")
        right_container.pack(side="left", fill="both", expand=True)
        self.right = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.right.pack(fill="both", expand=True)
        ctk.CTkLabel(
            self.right, text="←  Choose a game from the list", text_color=TEXT_DIM, font=font(14)
        ).pack(pady=40)

        # Fixed action bar - always visible, no scrolling needed to continue.
        bar = ctk.CTkFrame(right_container, fg_color=PANEL, corner_radius=14, border_width=1, border_color=EDGE)
        bar.pack(fill="x", pady=(10, 0))
        self.create_btn = ctk.CTkButton(
            bar, text="✔   CREATE PROFILE", height=44, width=230, corner_radius=12,
            font=font(15, True), fg_color=GOOD, hover_color="#27bd77", text_color="#0c0c10",
            state="disabled", command=self._create,
        )
        self.create_btn.pack(side="left", padx=12, pady=10)
        self.bar_hint = ctk.CTkLabel(
            bar, text="Pick a game to continue", text_color=TEXT_DIM, font=font(12),
            wraplength=self._last_wrap - 240, justify="left", anchor="w",
        )
        self.bar_hint.pack(side="left", fill="x", expand=True, padx=8)
        right_container.bind("<Configure>", self._on_right_resize)

    def _wrapped_label(self, parent, **kw) -> ctk.CTkLabel:
        kw.setdefault("wraplength", self._last_wrap)
        kw.setdefault("justify", "left")
        label = ctk.CTkLabel(parent, **kw)
        self._wrap_labels.append(label)
        return label

    def _on_right_resize(self, event) -> None:
        width = max(340, event.width - 120)
        if abs(width - self._last_wrap) < 24:
            return
        self._last_wrap = width
        for label in list(self._wrap_labels):
            if label.winfo_exists():
                label.configure(wraplength=max(200, width - (240 if label is self.bar_hint else 0)))
            else:
                self._wrap_labels.remove(label)

    def _game_image(self, game):
        """Real square Steam icon for the game, falling back to a lettered chip."""
        try:
            from . import artwork

            appid = artwork.steam_appid(game.id)
            img = artwork.icon(appid, 34) if appid else None
            if img is not None:
                return img
        except Exception:
            pass
        try:
            from .art import chip

            return chip(game.id, game.title[:1], 34)
        except Exception:
            return None

    def _game_row(self, detection, needle: str) -> None:
        game = detection.game
        if needle and needle not in game.title.lower():
            return
        row = ctk.CTkFrame(self.games_frame, fg_color=PANEL_2, corner_radius=10)
        row.pack(fill="x", pady=3)
        if detection.present:
            tags = []
            if detection.installed:
                tags.append("installed")
            if detection.save_paths:
                tags.append("saves found")
            subtitle, sub_color = "  ·  ".join(tags) or "detected", GOOD
        else:
            subtitle, sub_color = "not detected - manual path", TEXT_DIM
        btn = ctk.CTkButton(
            row, text=f"   {game.title}", image=self._game_image(game), compound="left",
            anchor="w", fg_color="transparent", hover_color=EDGE,
            font=font(14, True), height=42, command=lambda g=game: self.pick_game(g),
        )
        btn.pack(fill="x", padx=6, pady=(6, 0))
        ctk.CTkLabel(row, text=subtitle, text_color=sub_color, font=font(10), anchor="w").pack(
            fill="x", padx=16, pady=(0, 6)
        )

    def _fill_games(self) -> None:
        needle = self.search.get().strip().lower() if hasattr(self, "search") else ""
        for child in self.games_frame.winfo_children():
            child.destroy()
        ctk.CTkButton(
            self.games_frame, text="✎   Custom game - sync ANY game", anchor="w", height=38,
            corner_radius=10, fg_color="transparent", border_width=1, border_color=ACCENT,
            hover_color=PANEL_2, font=font(13),
            command=lambda: self.pick_game(None),
        ).pack(fill="x", pady=(2, 10), padx=2)
        present = [d for d in self.detections if d.present]
        absent = [d for d in self.detections if not d.present]
        if present:
            ctk.CTkLabel(
                self.games_frame, text="ON THIS PC", font=font(10, True), text_color=GOOD
            ).pack(anchor="w", padx=6, pady=(2, 4))
            for detection in present:
                self._game_row(detection, needle)
        elif not needle:
            self._wrapped_label(
                self.games_frame,
                text="No supported games detected on this PC yet. Use “Custom game” above, or search the full list below.",
                text_color=TEXT_DIM, font=font(11), wraplength=228,
            ).pack(anchor="w", padx=8, pady=(2, 6))
        # The full supported-games catalogue is only revealed while searching, so
        # the default view stays clean and shows just what's installed here.
        if needle:
            matches = [d for d in absent if needle in d.game.title.lower()]
            if matches:
                ctk.CTkLabel(
                    self.games_frame, text="OTHER SUPPORTED GAMES", font=font(10, True), text_color=TEXT_DIM
                ).pack(anchor="w", padx=6, pady=(12, 4))
                for detection in matches:
                    self._game_row(detection, needle)
        elif absent:
            ctk.CTkLabel(
                self.games_frame,
                text=f"＋ {len(absent)} more supported games - type above to find one",
                font=font(11), text_color=TEXT_DIM, anchor="w", wraplength=228, justify="left",
            ).pack(anchor="w", padx=8, pady=(12, 4))

    # -- right side -----------------------------------------------------

    def _card(self, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.right, fg_color=PANEL, corner_radius=16, border_width=1, border_color=EDGE)
        card.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(card, text=title, font=font(10, True), text_color=TEXT_DIM).pack(
            anchor="w", padx=14, pady=(10, 2)
        )
        return card

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.pick_game(self.selected_game)

    def pick_game(self, game) -> None:
        """Build the right-hand panel for a chosen game (None = custom game)."""
        self.selected_game = game
        self.selected_save = None
        self.save_rows = []
        self._save_widgets = []  # per-row label refs, for the async name enrichment
        # Old panel widgets are destroyed below - drop stale references so no
        # callback can touch a dead widget (this bug used to abort the build).
        self.compat_holder = None
        self.save_entry = None
        self.cloud_entry = None
        self._wrap_labels = [l for l in self._wrap_labels if l.winfo_exists()]
        for child in self.right.winfo_children():
            child.destroy()
        title = game.title if game else "Custom game"
        icon = game.icon if game else "✎"
        ctk.CTkLabel(self.right, text=f"{icon}  {title}", font=font(22, True)).pack(anchor="w", pady=(0, 6))

        # Seed vs Join: are you the one who has the world, or joining a shared one?
        toggle = ctk.CTkFrame(self.right, fg_color="transparent")
        toggle.pack(anchor="w", pady=(0, 8))
        for label, m in (("①  I have the world", "seed"), ("②  Join our shared world", "join")):
            active = self.mode == m
            ctk.CTkButton(
                toggle, text=label, height=30, corner_radius=8, width=180,
                fg_color=ACCENT if active else PANEL_2,
                hover_color=ACCENT_HOVER if active else EDGE,
                text_color="white" if active else TEXT_DIM,
                font=font(12, active), command=lambda mm=m: self._set_mode(mm),
            ).pack(side="left", padx=(0, 6))

        if game is None:
            card = self._card("GAME NAME")
            self.custom_title = ctk.CTkEntry(card, fg_color=PANEL_2, border_color=EDGE)
            self.custom_title.pack(fill="x", padx=14, pady=(0, 12))

        if self.mode == "join":
            self._build_join_panel(game, title)
            return
        self.create_btn.configure(text="✔   CREATE PROFILE")

        # -- save picker
        card = self._card("CHOOSE YOUR SAVE")
        self.saves_holder = ctk.CTkFrame(card, fg_color="transparent")
        self.saves_holder.pack(fill="x", padx=10, pady=(0, 4))
        candidates = save_candidates(game) if game else []
        # Palworld leaves behind lots of 1-file world stubs with no Level.sav -
        # those aren't loadable worlds, so hide them and keep the list clean.
        if game and getattr(game, "id", None) == "palworld":
            candidates = [
                c for c in candidates if not c.is_subfolder or (c.path / "Level.sav").is_file()
            ]
        if candidates:
            for candidate in candidates[:14]:
                self._save_row(self.saves_holder, candidate)
            self._enrich_saves()  # swap cryptic world IDs for character names (Palworld)
        else:
            self._wrapped_label(
                self.saves_holder,
                text="No saves detected automatically - browse to the folder below.",
                text_color=WARN, font=font(12),
            ).pack(anchor="w", padx=6, pady=4)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(2, 12))
        self.save_entry = ctk.CTkEntry(row, fg_color=PANEL_2, border_color=EDGE)
        self.save_entry.pack(side="left", fill="x", expand=True)
        ghost_button(row, text="Browse…", width=90, height=30, command=self._browse_save).pack(
            side="left", padx=(8, 0)
        )

        # -- process (custom only)
        if game is None:
            card = self._card("GAME PROCESS (OPTIONAL - enables auto sync on exit)")
            prow = ctk.CTkFrame(card, fg_color="transparent")
            prow.pack(fill="x", padx=14, pady=(0, 12))
            self.proc_entry = ctk.CTkEntry(prow, placeholder_text="MyGame.exe", fg_color=PANEL_2, border_color=EDGE)
            self.proc_entry.pack(side="left", fill="x", expand=True)
            ghost_button(prow, text="Detect running…", width=130, height=30, command=self._detect_process).pack(
                side="left", padx=(8, 0)
            )

        # -- compatibility
        self.compat_card = self._card("COMPATIBILITY CHECK")
        self.compat_holder = ctk.CTkFrame(self.compat_card, fg_color="transparent")
        self.compat_holder.pack(fill="x", padx=14, pady=(0, 6))
        self._run_compat()
        ghost_button(
            self.compat_card, text="Re-check", width=110, height=28, command=self._run_compat
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # -- cloud folder
        card = self._card("SHARED CLOUD FOLDER (same folder for all friends)")
        candidates_cloud = cloud_folder_candidates()
        crow = ctk.CTkFrame(card, fg_color="transparent")
        crow.pack(fill="x", padx=14, pady=(0, 4))
        self.cloud_entry = ctk.CTkEntry(crow, fg_color=PANEL_2, border_color=EDGE)
        self.cloud_entry.pack(side="left", fill="x", expand=True)
        ghost_button(crow, text="Browse…", width=90, height=30, command=self._browse_cloud).pack(
            side="left", padx=(8, 0)
        )
        if candidates_cloud:
            base = candidates_cloud[0][1]
            self.cloud_entry.insert(0, str(base / "SaveParty" / slugify(title)))
            self._wrapped_label(
                card,
                text="Detected: " + ", ".join(f"{name} ({path})" for name, path in candidates_cloud),
                text_color=TEXT_DIM, font=font(11),
            ).pack(anchor="w", padx=14, pady=(0, 12))
        else:
            self._wrapped_label(
                card,
                text="No cloud client detected - install OneDrive / Google Drive / Dropbox, or browse to any shared folder.",
                text_color=WARN, font=font(11),
            ).pack(anchor="w", padx=14, pady=(0, 12))

        # Everything is built - now select the best save and arm the action bar.
        if candidates:
            preferred = candidates[0]
            if game and game.prefer_subfolder:
                subfolders = [c for c in candidates if c.is_subfolder]
                if subfolders:
                    preferred = subfolders[0]  # already sorted newest-first
            self._select_save(preferred.path)
        else:
            self._run_compat()
        self.create_btn.configure(state="normal")
        self.bar_hint.configure(
            text="Check the save + shared folder above, then create the profile."
        )

    # -- join mode ------------------------------------------------------

    def _build_join_panel(self, game, title: str) -> None:
        """Panel for joining a world someone else already seeded to the cloud:
        point at the shared folder, choose where it downloads locally, join."""
        self.create_btn.configure(text="⬇   JOIN & DOWNLOAD")

        card = self._card("OUR SHARED CLOUD FOLDER (where the world already is)")
        crow = ctk.CTkFrame(card, fg_color="transparent")
        crow.pack(fill="x", padx=14, pady=(0, 4))
        self.cloud_entry = ctk.CTkEntry(crow, fg_color=PANEL_2, border_color=EDGE)
        self.cloud_entry.pack(side="left", fill="x", expand=True)
        self.cloud_entry.bind("<KeyRelease>", lambda _e: self._run_join_compat())
        ghost_button(crow, text="Browse…", width=90, height=30, command=self._browse_cloud).pack(
            side="left", padx=(8, 0)
        )
        candidates_cloud = cloud_folder_candidates()
        if candidates_cloud:
            self.cloud_entry.insert(0, str(candidates_cloud[0][1] / "SaveParty" / slugify(title)))
        self._wrapped_label(
            card,
            text="The SAME folder your friend shared with you - the one that shows up locally "
            "after you accept the share (Add shortcut / sync it).",
            text_color=TEXT_DIM, font=font(11),
        ).pack(anchor="w", padx=14, pady=(0, 12))

        card = self._card("DOWNLOAD THE WORLD TO (a new folder on your PC)")
        drow = ctk.CTkFrame(card, fg_color="transparent")
        drow.pack(fill="x", padx=14, pady=(0, 4))
        self.save_entry = ctk.CTkEntry(drow, fg_color=PANEL_2, border_color=EDGE)
        self.save_entry.pack(side="left", fill="x", expand=True)
        self.save_entry.bind("<KeyRelease>", lambda _e: self._run_join_compat())
        ghost_button(drow, text="Browse…", width=90, height=30, command=self._browse_save_join).pack(
            side="left", padx=(8, 0)
        )
        self.save_entry.insert(0, str(self._suggest_join_dest(game)))
        self._wrapped_label(
            card,
            text="A fresh folder for the shared world, kept apart from your own saves. It's "
            "empty now and fills up on the first sync.",
            text_color=TEXT_DIM, font=font(11),
        ).pack(anchor="w", padx=14, pady=(0, 12))

        if game is None:
            card = self._card("GAME PROCESS (OPTIONAL - enables auto sync on exit)")
            prow = ctk.CTkFrame(card, fg_color="transparent")
            prow.pack(fill="x", padx=14, pady=(0, 12))
            self.proc_entry = ctk.CTkEntry(prow, placeholder_text="MyGame.exe", fg_color=PANEL_2, border_color=EDGE)
            self.proc_entry.pack(side="left", fill="x", expand=True)
            ghost_button(prow, text="Detect running…", width=130, height=30, command=self._detect_process).pack(
                side="left", padx=(8, 0)
            )

        self.compat_card = self._card("READY TO JOIN?")
        self.compat_holder = ctk.CTkFrame(self.compat_card, fg_color="transparent")
        self.compat_holder.pack(fill="x", padx=14, pady=(0, 6))
        ghost_button(
            self.compat_card, text="Re-check", width=110, height=28, command=self._run_join_compat
        ).pack(anchor="w", padx=14, pady=(0, 12))
        self._run_join_compat()

    def _suggest_join_dest(self, game) -> Path:
        """Where a joiner should download the shared world. For games whose
        save is a per-world subfolder (Palworld), a brand-new world folder;
        otherwise the game's normal save location."""
        import uuid

        from ..detector import _template_vars, detect_paths

        root = None
        if game:
            roots = detect_paths(game)
            root = roots[0] if roots else None
        if root is None:
            root = Path(_template_vars()["LOCALAPPDATA"])
        if game and game.prefer_subfolder:
            return root / uuid.uuid4().hex.upper()  # a new Palworld-style world folder
        return root / "SavePartyShared"

    def _read_cloud_world(self, cloud: Path):
        """Return the manifest of a seeded world in `cloud`, or None. Reads both
        SaveParty and PalSync manifests (same files+generation shape)."""
        from ..util import read_json

        try:
            data = read_json(cloud / "manifest.json")
        except Exception:
            return None
        if data and isinstance(data.get("files"), dict) and "generation" in data:
            return data
        return None

    def _browse_save_join(self) -> None:
        path = filedialog.askdirectory(title="Choose where to download the shared world")
        if path and self.save_entry.winfo_exists():
            self.save_entry.delete(0, "end")
            self.save_entry.insert(0, path)
            self._run_join_compat()

    def _run_join_compat(self) -> None:
        holder = getattr(self, "compat_holder", None)
        if holder is None or not holder.winfo_exists():
            return
        for child in holder.winfo_children():
            child.destroy()
        cloud = self.cloud_entry.get().strip() if (self.cloud_entry and self.cloud_entry.winfo_exists()) else ""
        dest = self.save_entry.get().strip() if (self.save_entry and self.save_entry.winfo_exists()) else ""
        items: list[tuple[str, str]] = []
        world = self._read_cloud_world(Path(cloud)) if cloud else None
        if not cloud:
            items.append(("fail", "Enter the shared cloud folder your friend shared with you."))
        elif not Path(cloud).is_dir():
            items.append(("fail", f"Folder not found: {cloud} - is your cloud client running and synced?"))
        elif world is None:
            items.append((
                "fail",
                "No shared world in this folder yet. Ask whoever has the world to open "
                "SaveParty and press Sync (or Play) once so it uploads.",
            ))
        else:
            items.append((
                "ok",
                f"Found shared world: generation {world['generation']} · by "
                f"{world.get('pushed_by', '?')} · {human_size(world.get('total_size', 0))}",
            ))
        if dest:
            if Path(dest).exists() and any(Path(dest).iterdir()):
                items.append(("warn", f"{dest} already has files - the shared world will merge into it. Prefer a new/empty folder."))
            elif Path(dest).parent.is_dir():
                items.append(("ok", f"Will download to {dest}"))
            else:
                items.append(("warn", f"Parent folder doesn't exist yet: {Path(dest).parent}"))
        else:
            items.append(("fail", "Choose a local folder to download the world into."))
        if self.selected_game and self.selected_game.steam_cloud in ("on", "optional"):
            items.append(("warn", "Disable Steam Cloud for this game on your PC so it doesn't fight the sync."))
        marks = {"ok": "✔", "warn": "⚠", "fail": "✘"}
        colors = {"ok": GOOD, "warn": WARN, "fail": BAD}
        for level, text in items:
            self._wrapped_label(
                holder, text=f"{marks[level]}   {text}", text_color=colors[level], font=font(11), anchor="w"
            ).pack(anchor="w", pady=1)
        ready = not any(level == "fail" for level, _t in items)
        self.create_btn.configure(state="normal" if ready else "disabled")
        self.bar_hint.configure(
            text="Downloads the shared world on join." if ready else "Point at the shared folder that already has the world."
        )

    def _create_join(self) -> None:
        game = self.selected_game
        title = game.title if game else (getattr(self, "custom_title", None) and self.custom_title.get().strip())
        if not title:
            return
        cloud_dir = self.cloud_entry.get().strip()
        save_dir = self.save_entry.get().strip()
        if not cloud_dir or not Path(cloud_dir).is_dir() or self._read_cloud_world(Path(cloud_dir)) is None or not save_dir:
            self._run_join_compat()
            return
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        self.app._save_player_name()
        profile = self._make_profile(game, title, save_dir, cloud_dir)
        self.app.store.add(profile)
        self.app._rebuild_profile_buttons()
        self.app.show_game(profile.id)
        page = self.app.pages.get(profile.id)
        if page:
            page.append_log("Joining - downloading the shared world…", "info")
            page.on_sync()  # first sync sees an empty local folder + cloud world -> pulls

    def _make_profile(self, game, title: str, save_dir: str, cloud_dir: str) -> Profile:
        if game:
            return Profile.new(
                title=title, game_id=game.id, save_dir=save_dir, cloud_dir=cloud_dir,
                process_names=list(game.process_names), launch=game.launch,
                exclude_dirs=list(game.exclude_dirs), exclude_globs=list(game.exclude_globs),
            )
        procs = [p.strip() for p in getattr(self, "proc_entry").get().split(",") if p.strip()]
        return Profile.new(
            title=title, game_id="custom", save_dir=save_dir, cloud_dir=cloud_dir, process_names=procs,
        )

    def _save_row(self, holder, candidate) -> None:
        from .. import savenames

        row = ctk.CTkFrame(holder, fg_color=PANEL_2, corner_radius=10, border_width=1, border_color=EDGE)
        row.pack(fill="x", pady=2, padx=4)
        indent = 22 if candidate.is_subfolder else 8
        icon = "🗂" if not candidate.is_subfolder else "💾"
        # Friendly name for any game: file-based saves show the names inside,
        # folder-based show a tidied folder name (Palworld is upgraded async below).
        label, subtitle = savenames.describe(getattr(self.selected_game, "id", None), candidate)
        btn = ctk.CTkButton(
            row,
            text=f"{icon}  {label}",
            anchor="w",
            fg_color="transparent",
            hover_color=EDGE,
            font=font(13, True),
            height=26,
            command=lambda p=candidate.path: self._select_save(p),
        )
        btn.pack(fill="x", padx=(indent, 6), pady=(4, 0))
        detail = ctk.CTkLabel(
            row, text=subtitle, text_color=TEXT_DIM, font=font(10), anchor="w"
        )
        detail.pack(fill="x", padx=(indent + 12, 6), pady=(0, 5))
        self.save_rows.append((row, candidate.path))
        self._save_widgets.append(
            {"path": candidate.path, "btn": btn, "detail": detail, "candidate": candidate, "icon": icon}
        )

    def _enrich_saves(self) -> None:
        """For Palworld, replace the cryptic world-ID labels with the names of the
        characters living in each world (e.g. "Chris, Luisa"). The saves are parsed
        off the UI thread since decoding a Level.sav is slow."""
        game = self.selected_game
        if not game or getattr(game, "id", None) != "palworld":
            return
        snapshot = [w for w in self._save_widgets if (w["path"] / "Level.sav").is_file()]
        if not snapshot:
            return

        def work():
            from ..palworld import list_characters, world_name

            for w in snapshot:
                try:
                    chars = list_characters(w["path"])
                except Exception:
                    continue
                names = [c.nickname for c in chars if c.nickname and c.nickname != "(unnamed)"]
                lvl = max((c.level or 0 for c in chars), default=0)
                wname = world_name(w["path"])
                self.app.after(0, lambda w=w, wname=wname, names=names, lvl=lvl: self._apply_save_names(w, wname, names, lvl))

        threading.Thread(target=work, daemon=True).start()

    def _apply_save_names(self, w, wname, names, lvl) -> None:
        btn, detail = w["btn"], w["detail"]
        if not btn.winfo_exists() or not detail.winfo_exists():
            return
        # Primary = the world's real name ("Chris and Luisa"); fall back to the
        # characters inside if Palworld didn't store a name.
        primary = wname or (", ".join(names[:3]) if names else "world")
        suffix = f"  ·  Lv {lvl}" if lvl else ""
        btn.configure(text=f"{w['icon']}  {primary}{suffix}")
        who = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "") if names else ""
        short_id = w["path"].name[:8]
        parts = [p for p in (who, w["candidate"].detail(), f"id {short_id}…") if p]
        detail.configure(text="  ·  ".join(parts))

    def _select_save(self, path: Path) -> None:
        self.selected_save = path
        for row, row_path in self.save_rows:
            if row.winfo_exists():
                row.configure(border_color=ACCENT if row_path == path else EDGE)
        entry = getattr(self, "save_entry", None)
        if entry is not None and entry.winfo_exists():
            entry.delete(0, "end")
            entry.insert(0, str(path))
        self._run_compat()

    def _browse_save(self) -> None:
        path = filedialog.askdirectory(title="Select the game's save folder")
        if path:
            self._select_save(Path(path))

    def _browse_cloud(self) -> None:
        path = filedialog.askdirectory(title="Select the shared cloud folder")
        if path and self.cloud_entry and self.cloud_entry.winfo_exists():
            self.cloud_entry.delete(0, "end")
            self.cloud_entry.insert(0, path)
            if self.mode == "join":
                self._run_join_compat()

    def _detect_process(self) -> None:
        ProcessPickerDialog(self.app, self.proc_entry)

    def _run_compat(self) -> None:
        holder = getattr(self, "compat_holder", None)
        if holder is None or not holder.winfo_exists():
            return
        for child in holder.winfo_children():
            child.destroy()
        entry = getattr(self, "save_entry", None)
        entry_ok = entry is not None and entry.winfo_exists()
        save_dir = Path(entry.get()) if entry_ok and entry.get() else None
        verdict, items = compatibility_check(save_dir, self.selected_game)
        marks = {"ok": "✔", "warn": "⚠", "fail": "✘"}
        colors = {"ok": GOOD, "warn": WARN, "fail": BAD}
        for item in items:
            self._wrapped_label(
                self.compat_holder,
                text=f"{marks[item.level]}   {item.title} - {item.detail}",
                text_color=colors[item.level], font=font(11), anchor="w",
            ).pack(anchor="w", pady=1)
        summary = {
            "ok": "Compatible - ready to sync.",
            "warn": "Compatible with warnings - read the notes above.",
            "fail": "Not ready - fix the failed item first.",
        }[verdict]
        ctk.CTkLabel(
            self.compat_holder, text=summary, font=font(12, True), text_color=colors[verdict if verdict != "fail" else "fail"],
        ).pack(anchor="w", pady=(4, 2))

    def _create(self) -> None:
        if self.mode == "join":
            self._create_join()
            return
        game = self.selected_game
        title = game.title if game else (getattr(self, "custom_title", None) and self.custom_title.get().strip())
        if not title:
            return
        save_dir = self.save_entry.get().strip()
        cloud_dir = self.cloud_entry.get().strip()
        if not save_dir or not Path(save_dir).is_dir() or not cloud_dir:
            self._run_compat()
            return
        Path(cloud_dir).mkdir(parents=True, exist_ok=True)
        self.app._save_player_name()
        profile = self._make_profile(game, title, save_dir, cloud_dir)
        self.app.store.add(profile)
        self.app._rebuild_profile_buttons()
        self.app.show_game(profile.id)


# ----------------------------------------------------------------------
# dialogs
# ----------------------------------------------------------------------


class _Modal(ctk.CTkToplevel):
    def __init__(self, app: App, title: str, width: int = 520):
        super().__init__(app, fg_color=BG)
        self.title(title)
        self.geometry(f"{width}x10")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.attributes("-topmost", True)

    def fit(self) -> None:
        self.update_idletasks()
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")


class ConfirmDialog(_Modal):
    def __init__(self, app: App, payload: dict):
        super().__init__(app, "SaveParty - confirm")
        self.payload = payload
        ctk.CTkLabel(self, text=payload["text"], wraplength=470, justify="left", font=font(13)).pack(
            padx=22, pady=18
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=(0, 16))
        ctk.CTkButton(
            row, text="Yes", width=120, height=34, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, font=font(13, True),
            command=lambda: self._answer(True),
        ).pack(side="left", padx=6)
        ghost_button(row, text="No", width=120, height=34, font=font(13), command=lambda: self._answer(False)).pack(
            side="left", padx=6
        )
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(payload.get("default", False)))
        self.fit()

    def _answer(self, value: bool) -> None:
        self.payload["box"]["result"] = value
        self.payload["done"].set()
        self.destroy()

    @staticmethod
    def ask_now(app: App, text: str, default: bool = False) -> bool:
        done = threading.Event()
        box: dict = {}
        dialog = ConfirmDialog(app, {"text": text, "default": default, "done": done, "box": box})
        app.wait_window(dialog)
        return bool(box.get("result", default))


class ConflictDialog(_Modal):
    def __init__(self, app: App, payload: dict):
        super().__init__(app, "SaveParty - save conflict", width=580)
        self.payload = payload
        info = payload["info"]
        headline = (
            "First sync on this PC: both a local save and a cloud save exist."
            if info.get("first_time")
            else "Both sides changed since the last sync - nothing is overwritten silently."
        )
        ctk.CTkLabel(self, text=headline, font=font(14, True), wraplength=540).pack(padx=22, pady=(18, 6))
        details = (
            f"Cloud:  generation {info.get('cloud_generation', '?')}  ·  pushed by "
            f"{info.get('cloud_by')}  ·  {info.get('cloud_at')}\n"
            f"Local:  {info.get('local_changed')} file(s) differ"
        )
        ctk.CTkLabel(self, text=details, justify="left", wraplength=540, text_color=TEXT_DIM, font=font(12)).pack(
            padx=22
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=16)
        ctk.CTkButton(
            row, text="Keep CLOUD version\n(local kept as backup)", width=180, height=48,
            corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER, font=font(12, True),
            command=lambda: self._answer("cloud"),
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            row, text="Keep MY LOCAL version\n(cloud backed up first)", width=180, height=48,
            corner_radius=10, fg_color=PANEL_2, hover_color=EDGE, border_width=1, border_color=ACCENT,
            font=font(12, True), command=lambda: self._answer("local"),
        ).pack(side="left", padx=5)
        ghost_button(row, text="Decide later", width=110, height=48, font=font(12), command=lambda: self._answer("defer")).pack(
            side="left", padx=5
        )
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer("defer"))
        self.fit()

    def _answer(self, value: str) -> None:
        self.payload["box"]["result"] = value
        self.payload["done"].set()
        self.destroy()


class BackupsDialog(_Modal):
    def __init__(self, app: App, page: GamePage):
        super().__init__(app, f"Backups - {page.profile.title}", width=640)
        self.app = app
        self.page = page
        self.backups = list_backups(page.profile.backups_path())
        ctk.CTkLabel(
            self, text=f"{len(self.backups)} backup(s)", font=font(15, True)
        ).pack(padx=18, pady=(16, 2))
        ctk.CTkLabel(
            self, text=str(page.profile.backups_path()), text_color=TEXT_DIM, font=font(11)
        ).pack(padx=18)
        frame = ctk.CTkScrollableFrame(self, width=590, height=300, fg_color=PANEL, corner_radius=12)
        frame.pack(padx=18, pady=8)
        for backup in self.backups[:40]:
            row = ctk.CTkFrame(frame, fg_color=PANEL_2, corner_radius=8)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row,
                text=f"{backup.name}   ·   {human_size(backup.size)}   ·   {backup.mtime.strftime('%Y-%m-%d %H:%M')}",
                anchor="w", font=font(11),
            ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
            ghost_button(row, text="Restore", width=80, height=26, command=lambda b=backup: self._restore(b)).pack(
                side="right", padx=6
            )
        ghost_button(self, text="Open backups folder", width=170, command=self._open_folder).pack(pady=(4, 16))
        self.fit()

    def _open_folder(self) -> None:
        path = self.page.profile.backups_path()
        path.mkdir(parents=True, exist_ok=True)
        webbrowser.open(str(path))

    def _restore(self, backup) -> None:
        if not ConfirmDialog.ask_now(
            self.app,
            f"Restore {backup.name} over the current local save? The current save is backed up first.",
            default=False,
        ):
            return
        engine = self.app.engine_for(self.page.profile)
        self.app.run_task(self.page.profile, engine.restore_backup, backup.path)
        self.destroy()


class SettingsDialog(_Modal):
    def __init__(self, app: App, page: GamePage):
        super().__init__(app, f"Settings - {page.profile.title}", width=620)
        self.app = app
        self.page = page
        profile = page.profile
        self.entries: dict[str, ctk.CTkEntry] = {}
        for label, key in (
            ("Save folder", "save_dir"),
            ("Cloud folder", "cloud_dir"),
            ("Process names (comma-separated)", "process_names"),
            ("Launch (steam:// URI or exe path)", "launch"),
            ("Exclude folder names (comma-separated)", "exclude_dirs"),
        ):
            ctk.CTkLabel(self, text=label, font=font(11, True), text_color=TEXT_DIM).pack(
                anchor="w", padx=18, pady=(10, 0)
            )
            entry = ctk.CTkEntry(self, width=580, fg_color=PANEL_2, border_color=EDGE)
            value = getattr(profile, key)
            entry.insert(0, ", ".join(value) if isinstance(value, list) else str(value))
            entry.pack(padx=18)
            self.entries[key] = entry

        # Global option: keep Syncthing running so the shared folder always syncs.
        self.syncthing_var = ctk.BooleanVar(value=getattr(self.app.store, "auto_start_syncthing", True))
        sync_row = ctk.CTkFrame(self, fg_color="transparent")
        sync_row.pack(anchor="w", padx=18, pady=(16, 0), fill="x")
        ctk.CTkCheckBox(
            sync_row, text="Keep Syncthing running (start it when SaveParty opens)",
            variable=self.syncthing_var, font=font(12, True),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).pack(side="left")
        try:
            from .. import syncthing

            state = "running now" if syncthing.is_running() else "not running"
        except Exception:
            state = ""
        if state:
            ctk.CTkLabel(sync_row, text=f"· {state}", text_color=TEXT_DIM, font=font(11)).pack(side="left", padx=8)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=16)
        ctk.CTkButton(
            row, text="Save", width=130, height=34, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, font=font(13, True), command=self._save,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row, text="Delete profile", width=130, height=34, corner_radius=10,
            fg_color=BAD, hover_color="#d64a66", font=font(13), command=self._delete,
        ).pack(side="left", padx=6)
        ghost_button(row, text="Cancel", width=100, height=34, font=font(13), command=self.destroy).pack(
            side="left", padx=6
        )
        self.fit()

    def _save(self) -> None:
        profile = self.page.profile
        profile.save_dir = self.entries["save_dir"].get().strip()
        profile.cloud_dir = self.entries["cloud_dir"].get().strip()
        profile.launch = self.entries["launch"].get().strip()
        profile.process_names = [
            p.strip() for p in self.entries["process_names"].get().split(",") if p.strip()
        ]
        profile.exclude_dirs = [
            d.strip() for d in self.entries["exclude_dirs"].get().split(",") if d.strip()
        ]
        self.app.store.auto_start_syncthing = bool(self.syncthing_var.get())
        self.app.store.save()
        self.app._rebuild_profile_buttons()
        self.destroy()
        self.app.show_game(profile.id)

    def _delete(self) -> None:
        if not ConfirmDialog.ask_now(
            self.app,
            f"Remove the profile for {self.page.profile.title}? Game saves, cloud files, "
            "and backups are NOT deleted - only SaveParty's profile entry.",
            default=False,
        ):
            return
        self.app.store.remove(self.page.profile.id)
        self.app._rebuild_profile_buttons()
        self.destroy()
        if self.app.store.profiles:
            self.app.show_game(self.app.store.profiles[0].id)
        else:
            self.app.show_add_game()


class CharactersDialog(_Modal):
    """Palworld only: who owns which character, plus one-click claiming.

    Whoever hosts a Palworld world controls its fixed host character slot;
    once every player has claimed their character here, SaveParty re-maps the
    save before each session so the host plays their OWN character.
    """

    def __init__(self, app: App, page: GamePage):
        super().__init__(app, f"Characters - {page.profile.title}", width=660)
        self.app = app
        self.page = page
        self._build()

    def _build(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        from .. import palworld

        profile = self.page.profile
        ctk.CTkLabel(self, text="World characters", font=font(16, True)).pack(padx=18, pady=(16, 2))
        ctk.CTkLabel(
            self,
            text="Each player clicks 'This is me' once on THEIR character - including the "
            "HOST-slot one if that's you. After that, whoever hosts plays their own "
            "character, swapped automatically at session start, with a backup.",
            text_color=TEXT_DIM, font=font(11), wraplength=600, justify="left",
        ).pack(padx=18, pady=(0, 8))
        holder = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        holder.pack(padx=18, pady=4, fill="x")
        try:
            chars = palworld.list_characters(Path(profile.save_dir))
            registry = palworld.read_registry(Path(profile.cloud_dir)) or {"format": 1, "players": {}}
            owners_by_uid = {
                info["uid"].upper(): name
                for name, info in registry.get("players", {}).items()
                if info.get("uid")
            }
            host_owner = palworld.resolve_host_owner(registry, Path(profile.save_dir))
            for char in chars:
                row = ctk.CTkFrame(holder, fg_color=PANEL_2, corner_radius=8)
                row.pack(fill="x", padx=8, pady=3)
                slot = "HOST SLOT" if char.is_host_slot else "guest"
                owner = owners_by_uid.get(char.uid)
                if char.is_host_slot and host_owner:
                    owner = owner or host_owner
                level = f"Lv {char.level}" if char.level is not None else "Lv ?"
                ctk.CTkLabel(
                    row,
                    text=f"{'👑' if char.is_host_slot else '🎮'}  {char.nickname}   ·   {level}   ·   {slot}",
                    font=font(13, True), anchor="w",
                ).pack(side="left", padx=10, pady=8)
                ctk.CTkLabel(
                    row,
                    text=f"claimed by {owner}" if owner else "unclaimed",
                    text_color=GOOD if owner else WARN, font=font(11),
                ).pack(side="left", padx=8)
                if char.is_host_slot:
                    # The host slot holds whoever is hosting. Claiming it records
                    # you as the host-slot owner (initial_host_owner), which is
                    # the right move when YOUR character currently sits there.
                    ghost_button(
                        row, text="This is me", width=90, height=26,
                        command=lambda c=char, o=host_owner: self._claim_host(c, o),
                    ).pack(side="right", padx=8)
                else:
                    ghost_button(
                        row, text="This is me", width=90, height=26,
                        command=lambda c=char: self._claim(c, owners_by_uid.get(c.uid)),
                    ).pack(side="right", padx=8)
            ctk.CTkLabel(
                self,
                text=f"Host slot currently belongs to: {host_owner or 'unknown'}   ·   "
                f"you are: {self.app.store.player_name}",
                text_color=TEXT_DIM, font=font(11),
            ).pack(padx=18, pady=(6, 4))
        except SavePartyError as exc:
            ctk.CTkLabel(
                holder, text=str(exc), text_color=WARN, font=font(12), wraplength=580, justify="left"
            ).pack(padx=12, pady=(12, 4))
            worlds = self._world_candidates()
            if worlds:
                ctk.CTkLabel(
                    holder,
                    text="Fix it here - this profile points at the whole folder. Pick your world:",
                    font=font(12, True), wraplength=580, justify="left",
                ).pack(padx=12, pady=(6, 2))
                for candidate in worlds[:8]:
                    row = ctk.CTkFrame(holder, fg_color=BG, corner_radius=8)
                    row.pack(fill="x", padx=10, pady=2)
                    ctk.CTkLabel(
                        row, text=f"💾  {candidate.label}   ·   {candidate.detail()}",
                        font=font(12), anchor="w",
                    ).pack(side="left", padx=8, pady=6)
                    ghost_button(
                        row, text="Use this world", width=110, height=26,
                        command=lambda c=candidate: self._repoint(c.path),
                    ).pack(side="right", padx=8)
        ghost_button(self, text="Close", width=100, command=self.destroy).pack(pady=(4, 16))
        self.fit()

    def _world_candidates(self):
        from ..detector import save_candidates
        from ..games_db import get_game

        try:
            candidates = save_candidates(
                get_game("palworld"), roots=[Path(self.page.profile.save_dir)]
            )
        except Exception:
            return []
        return [
            c for c in candidates
            if c.is_subfolder and (c.path / "Level.sav").is_file()
        ]

    def _repoint(self, path: Path) -> None:
        profile = self.page.profile
        profile.save_dir = str(path)
        self.app.store.save()
        self.page.append_log(f"Profile save folder set to world: {path.name}", "success")
        self.page.refresh_async()
        self._build()

    def _claim(self, char, prior_owner: str | None) -> None:
        from .. import palworld

        me = self.app.store.player_name
        if prior_owner and prior_owner != me:
            if not ConfirmDialog.ask_now(
                self.app,
                f"'{char.nickname}' is already claimed by {prior_owner}. Take it over?",
                default=False,
            ):
                return
        profile = self.page.profile
        registry = palworld.read_registry(Path(profile.cloud_dir)) or {"format": 1, "players": {}}
        if prior_owner and prior_owner != me:
            registry["players"].pop(prior_owner, None)
        registry.setdefault("players", {})[me] = {"uid": char.uid, "nickname": char.nickname}
        palworld.write_registry(Path(profile.cloud_dir), registry)
        self.page.append_log(f"Claimed '{char.nickname}' as {me}'s character.", "success")
        self._build()

    def _claim_host(self, char, current_owner: str | None) -> None:
        """Claim the host-slot character - used when your own character is the
        one currently sitting in the host slot (e.g. you created it while
        hosting). Records you as the host-slot owner without touching any guest
        slot you may already own."""
        from .. import palworld

        me = self.app.store.player_name
        if current_owner and current_owner != me:
            if not ConfirmDialog.ask_now(
                self.app,
                f"The host-slot character is currently recorded as {current_owner}'s. "
                "Take it as yours?",
                default=False,
            ):
                return
        profile = self.page.profile
        registry = palworld.read_registry(Path(profile.cloud_dir)) or {"format": 1, "players": {}}
        registry["initial_host_owner"] = me
        entry = registry.setdefault("players", {}).setdefault(me, {})
        entry["nickname"] = char.nickname
        entry.setdefault("uid", None)  # keep an existing guest slot as our off-host destination
        palworld.write_registry(Path(profile.cloud_dir), registry)
        lvl = f" (Lv {char.level})" if char.level is not None else ""
        self.page.append_log(
            f"You are now the host-slot character '{char.nickname}'{lvl}. When a friend "
            "hosts, your character moves to your own slot automatically.",
            "success",
        )
        self._build()


class ProcessPickerDialog(_Modal):
    def __init__(self, app: App, target_entry: ctk.CTkEntry):
        super().__init__(app, "Pick the game's process", width=500)
        self.target = target_entry
        ctk.CTkLabel(
            self, text="Start the game, then pick its process (newest first):",
            wraplength=460, font=font(13),
        ).pack(padx=18, pady=(16, 8))
        frame = ctk.CTkScrollableFrame(self, width=450, height=320, fg_color=PANEL, corner_radius=12)
        frame.pack(padx=18, pady=(0, 16))
        for name, _started in list_running_processes():
            ghost_button(
                frame, text=name, anchor="w", height=28, font=font(12),
                command=lambda n=name: self._pick(n),
            ).pack(fill="x", pady=1)
        self.fit()

    def _pick(self, name: str) -> None:
        self.target.delete(0, "end")
        self.target.insert(0, name)
        self.destroy()


def run(smoke: bool = False) -> int:
    app = App()
    if smoke:
        app._smoke = True
        app.withdraw()
        app.after(1500, app.destroy)
    app.mainloop()
    return 0
