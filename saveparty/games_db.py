"""Built-in database of known games and where they keep their saves.

Path templates use placeholders ({LOCALAPPDATA}, {LOCALLOW}, {APPDATA},
{DOCUMENTS}, {SAVEDGAMES}, {USERPROFILE}) and may contain one `*` segment for
per-user subfolders. Templates are *candidates*: the detector only offers
paths that actually exist on the user's PC, and every profile can be pointed
at any folder manually - plus ANY game with local file saves works through
the Custom game option.

`steam_cloud` records whether the game's own Steam Cloud sync would fight
with SaveParty ("on" = known enabled by default, "optional" = per-save
toggle, "off" = none, "unknown"). `coop_notes` explains how the game handles
player characters in a shared save - most games have no Palworld-style host
problem because characters are either client-side files or keyed by account
inside the save.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GameDef:
    id: str
    title: str
    steam_appid: int | None
    save_paths: list[str]
    process_names: list[str] = field(default_factory=list)
    steam_cloud: str = "unknown"  # on / optional / off / unknown
    notes: str = ""
    coop_notes: str = ""
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=lambda: ["*.tmp"])
    prefer_subfolder: bool = False  # the real save is a subfolder (world/slot), not the root
    require_file: str = ""  # a file the chosen folder must contain (validation)

    @property
    def launch(self) -> str:
        return f"steam://rungameid/{self.steam_appid}" if self.steam_appid else ""

    @property
    def icon(self) -> str:
        return ICONS.get(self.id, "🎮")


ICONS = {
    "palworld": "🐾",
    "valheim": "⚔️",
    "stardew-valley": "🌾",
    "terraria": "⛏️",
    "minecraft-java": "🧱",
    "satisfactory": "🏭",
    "factorio": "⚙️",
    "baldurs-gate-3": "🎲",
    "elden-ring": "💀",
    "sons-of-the-forest": "🌲",
    "the-forest": "🌳",
    "core-keeper": "💎",
    "v-rising": "🦇",
    "project-zomboid": "🧟",
    "7-days-to-die": "☣️",
    "raft": "🌊",
    "green-hell": "🐍",
    "grounded": "🐜",
    "enshrouded": "🌫️",
    "dont-starve-together": "🍖",
    "lethal-company": "📦",
    "astroneer": "🚀",
    "rimworld": "🛰️",
    "vintage-story": "🏺",
}


GAMES: list[GameDef] = [
    GameDef(
        id="palworld",
        title="Palworld",
        steam_appid=1623730,
        save_paths=[r"{LOCALAPPDATA}\Pal\Saved\SaveGames\*"],
        process_names=["Palworld-Win64-Shipping.exe"],
        steam_cloud="off",
        notes="Pick ONE world folder (the one containing Level.sav).",
        coop_notes=(
            "Palworld maps whoever hosts onto a fixed host character. SaveParty fixes "
            "this automatically: each player claims their character once (Characters "
            "button) and every session hosts you as YOUR character."
        ),
        exclude_dirs=["backup"],
        prefer_subfolder=True,
        require_file="Level.sav",
    ),
    GameDef(
        id="valheim",
        title="Valheim",
        steam_appid=892970,
        save_paths=[r"{LOCALLOW}\IronGate\Valheim\worlds_local", r"{LOCALLOW}\IronGate\Valheim"],
        process_names=["valheim.exe"],
        steam_cloud="on",
        notes=(
            "Sync the worlds_local folder ONLY - characters stay personal on each PC. "
            "Disable Steam Cloud for Valheim on every friend's PC."
        ),
        coop_notes=(
            "Characters are separate local files on each PC - everyone automatically "
            "plays their own character; only the world is shared."
        ),
        exclude_dirs=["characters", "characters_local", "worlds"],
    ),
    GameDef(
        id="stardew-valley",
        title="Stardew Valley",
        steam_appid=413150,
        save_paths=[r"{APPDATA}\StardewValley\Saves"],
        process_names=["Stardew Valley.exe", "StardewModdingAPI.exe"],
        steam_cloud="on",
        notes="Disable Steam Cloud for Stardew Valley on every friend's PC.",
        coop_notes=(
            "Farmhands live inside the farm save - joiners pick their farmhand "
            "in-game. No fixing needed."
        ),
    ),
    GameDef(
        id="terraria",
        title="Terraria",
        steam_appid=105600,
        save_paths=[r"{DOCUMENTS}\My Games\Terraria\Worlds"],
        process_names=["Terraria.exe"],
        steam_cloud="optional",
        notes=(
            "Sync the Worlds folder ONLY - player characters (.plr) stay personal on "
            "each PC. Keep worlds saved locally, not 'cloud', in Terraria's menus."
        ),
        coop_notes=(
            "Characters are client-side files on each player's PC - everyone keeps "
            "their own; only worlds are shared."
        ),
    ),
    GameDef(
        id="minecraft-java",
        title="Minecraft (Java)",
        steam_appid=None,
        save_paths=[r"{APPDATA}\.minecraft\saves"],
        process_names=[],
        steam_cloud="off",
        notes=(
            "Pick a single world subfolder. No process detection - use the End "
            "Session button when you stop."
        ),
        prefer_subfolder=True,
        coop_notes=(
            "Each world stores per-account playerdata - every player keeps their own "
            "character and inventory automatically."
        ),
    ),
    GameDef(
        id="satisfactory",
        title="Satisfactory",
        steam_appid=526870,
        save_paths=[r"{LOCALAPPDATA}\FactoryGame\Saved\SaveGames"],
        process_names=["FactoryGameSteam-Win64-Shipping.exe", "FactoryGame-Win64-Shipping.exe"],
        steam_cloud="unknown",
        notes="If Steam Cloud is enabled for Satisfactory, disable it on every friend's PC.",
        coop_notes="Sessions belong to the world save; joiners just join the host - no character issues.",
    ),
    GameDef(
        id="factorio",
        title="Factorio",
        steam_appid=427520,
        save_paths=[r"{APPDATA}\Factorio\saves"],
        process_names=["factorio.exe"],
        steam_cloud="on",
        notes="Disable Steam Cloud sync for Factorio on every friend's PC (in-game options too).",
        coop_notes="One save file per map; players are just names in the save - nothing to fix.",
    ),
    GameDef(
        id="baldurs-gate-3",
        title="Baldur's Gate 3",
        steam_appid=1086940,
        save_paths=[r"{LOCALAPPDATA}\Larian Studios\Baldur's Gate 3\PlayerProfiles\Public\Savegames"],
        process_names=["bg3.exe", "bg3_dx11.exe"],
        steam_cloud="on",
        notes=(
            "Disable Steam Cloud AND Larian cross-saves on every friend's PC, or the "
            "cloud systems will fight this sync."
        ),
        coop_notes=(
            "The whole party lives in the save - whoever hosts continues the campaign; "
            "joiners take over party members in the lobby."
        ),
    ),
    GameDef(
        id="elden-ring",
        title="Elden Ring (seamless co-op)",
        steam_appid=1245620,
        save_paths=[r"{APPDATA}\EldenRing\*", r"{APPDATA}\EldenRing"],
        process_names=["eldenring.exe"],
        steam_cloud="on",
        notes=(
            "Disable Steam Cloud for Elden Ring on every friend's PC. With the Seamless "
            "Co-op mod, sync the folder containing the .co2 saves."
        ),
        coop_notes=(
            "The save IS one character's journey - passing it around means sharing one "
            "character, like a couch playthrough."
        ),
    ),
    GameDef(
        id="sons-of-the-forest",
        title="Sons Of The Forest",
        steam_appid=1326470,
        save_paths=[r"{LOCALLOW}\Endnight\SonsOfTheForest\Saves"],
        process_names=["SonsOfTheForest.exe"],
        steam_cloud="unknown",
        coop_notes="The host's save carries the shared world; joiners keep their own gear via the game's own system.",
    ),
    GameDef(
        id="the-forest",
        title="The Forest",
        steam_appid=242760,
        save_paths=[r"{LOCALLOW}\SKS\TheForest"],
        process_names=["TheForest.exe"],
        steam_cloud="unknown",
        coop_notes="The host's save carries the shared world; joiners re-equip on join (game behavior).",
    ),
    GameDef(
        id="core-keeper",
        title="Core Keeper",
        steam_appid=1621690,
        save_paths=[r"{LOCALLOW}\Pugstorm\Core Keeper\Steam", r"{LOCALLOW}\Pugstorm\Core Keeper"],
        process_names=["CoreKeeper.exe"],
        steam_cloud="unknown",
        coop_notes="Characters and worlds are separate saves - share the world; characters stay personal.",
    ),
    GameDef(
        id="v-rising",
        title="V Rising",
        steam_appid=1604030,
        save_paths=[r"{LOCALLOW}\Stunlock Studios\VRising\Saves"],
        process_names=["VRising.exe"],
        steam_cloud="unknown",
        coop_notes="Players are keyed by platform account inside the world save - everyone keeps their own vampire.",
    ),
    GameDef(
        id="project-zomboid",
        title="Project Zomboid",
        steam_appid=108600,
        save_paths=[r"{USERPROFILE}\Zomboid\Saves"],
        process_names=["ProjectZomboid64.exe"],
        steam_cloud="off",
        coop_notes="Characters are stored per-account inside the world save - everyone keeps their own survivor.",
    ),
    GameDef(
        id="7-days-to-die",
        title="7 Days to Die",
        steam_appid=251570,
        save_paths=[r"{APPDATA}\7DaysToDie\Saves"],
        process_names=["7DaysToDie.exe"],
        steam_cloud="off",
        coop_notes="Player characters are keyed by account inside the world save - everyone keeps their own automatically.",
    ),
    GameDef(
        id="raft",
        title="Raft",
        steam_appid=648800,
        save_paths=[r"{LOCALLOW}\Redbeet Interactive\Raft\User\*"],
        process_names=["Raft.exe"],
        steam_cloud="unknown",
        notes="Pick your User_<steamid> folder.",
        coop_notes="The host's save carries the raft and world; joiners spawn with their own inventory per session.",
        prefer_subfolder=True,
    ),
    GameDef(
        id="green-hell",
        title="Green Hell",
        steam_appid=815370,
        save_paths=[r"{LOCALLOW}\CreepyJar\GreenHell"],
        process_names=["GH.exe"],
        steam_cloud="unknown",
        coop_notes="The host's save carries the shared expedition.",
    ),
    GameDef(
        id="grounded",
        title="Grounded",
        steam_appid=962130,
        save_paths=[r"{LOCALAPPDATA}\Maine\Saved\SaveGames"],
        process_names=["Maine-Win64-Shipping.exe"],
        steam_cloud="unknown",
        coop_notes="Shared-world saves carry every player's character, keyed by account - nothing to fix.",
    ),
    GameDef(
        id="enshrouded",
        title="Enshrouded",
        steam_appid=1203620,
        save_paths=[r"{SAVEDGAMES}\Enshrouded"],
        process_names=["enshrouded.exe"],
        steam_cloud="unknown",
        coop_notes="Characters are per-player and local - only the world is shared; everyone stays themselves.",
    ),
    GameDef(
        id="dont-starve-together",
        title="Don't Starve Together",
        steam_appid=322330,
        save_paths=[r"{DOCUMENTS}\Klei\DoNotStarveTogether"],
        process_names=["dontstarve_steam.exe"],
        steam_cloud="unknown",
        notes="Sync the whole folder (cluster saves live in subfolders per slot).",
        coop_notes="Characters are picked per session in-game - no character data to fix.",
    ),
    GameDef(
        id="lethal-company",
        title="Lethal Company",
        steam_appid=1966720,
        save_paths=[r"{LOCALLOW}\ZeekerssRBLX\Lethal Company"],
        process_names=["Lethal Company.exe"],
        steam_cloud="unknown",
        coop_notes="Only the ship/quota save matters - crew members are interchangeable.",
    ),
    GameDef(
        id="astroneer",
        title="Astroneer",
        steam_appid=361420,
        save_paths=[r"{LOCALAPPDATA}\Astro\Saved\SaveGames"],
        process_names=["Astro-Win64-Shipping.exe"],
        steam_cloud="unknown",
        coop_notes="The world save carries everything; joiners keep their suit/loadout per session.",
    ),
    GameDef(
        id="rimworld",
        title="RimWorld (pass-the-colony)",
        steam_appid=294100,
        save_paths=[r"{LOCALLOW}\Ludeon Studios\RimWorld by Ludeon Studios\Saves"],
        process_names=["RimWorldWin64.exe"],
        steam_cloud="optional",
        notes="Single-player game - great for taking turns running one colony.",
        coop_notes="No player characters - you all steer the same colony in turns.",
    ),
    GameDef(
        id="vintage-story",
        title="Vintage Story",
        steam_appid=None,
        save_paths=[r"{APPDATA}\VintagestoryData\Saves"],
        process_names=["Vintagestory.exe"],
        steam_cloud="off",
        coop_notes="Players are keyed by account inside the world save - everyone keeps their own character.",
    ),
]


def get_game(game_id: str) -> GameDef | None:
    return next((g for g in GAMES if g.id == game_id), None)
