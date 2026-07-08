"""Rich terminal UI: step progress plus a diorama cross-section that
"3D-prints" while the pipeline runs.

The cross-section is a fixed canvas of category-coded cells (terrain, road,
water, buildings, roofs, trees, base slab). Finished pipeline steps reveal
rows bottom-up in full colour, like a print growing on the bed; the rest of
the model shows as a dim ghost (the "sliced preview"), with a printhead
hovering over the current layer. Below the panel: one overall step bar and
transient per-file download bars (fed by download.set_progress_hook).

Everything degrades gracefully: when stdout is not a terminal the class
falls back to the plain prints the pipeline always had, and stray print()
calls from deeper modules are captured by Live and shown above the display.
"""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.constrain import Constrain
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn,
                           TextColumn, TimeElapsedColumn, TransferSpeedColumn)
from rich.table import Table
from rich.text import Text

from . import download
from .config import CATEGORY_COLORS

# Cross-section canvas (art by the project author): buildings with windows,
# a bridged river inlay cutting down through the terrain, a road layer with
# lane markings, and the base slab. Rows are padded to the widest row at
# import time.
_ART = [
    "                 ▄█▄                  ██",
    "                 ███          ▄▄▄▄▄▄  ██▄▄▄▄",
    "        ▄▄▄▄▄    ███          █▀▀▀▀█  ██████",
    "        █▪▪▪█    ███   ▄▄▄▄▄  █▪██▪█  █▪██▪█",
    "        █▪▪▪█  ▄▄███▄▄ █▪█▪█  █▪██▪█  █▪██▪█        ▄▄▄▄▄▄▄",
    "        █▪▪▪█  █▪█▪█▪█ █▪█▪█  █▪██▪█  █▪██▪█   ♣♣   █▪█▪█▪█",
    "   ♣♣   █▪▪▪█  █▪█▪█▪█ █▪█▪█  █▪██▪█  █▪██▪█  ♣♣♣♣  █▪█▪█▪█",
    "  ♣♣♣♣  █▪▪▪█  █▪█▪█▪█ █▪█▪█  █▪██▪█  █▪██▪█   ││   █▪█▪█▪█",
    "   ││   █▪▪▪█  █▪█▪█▪█ █▪█▪█  █▪██▪█ ╔═▀▀▀▀═╗  ││   █▪█▪█▪█",
    "━━┷┷┷━━━┷┷┷┷┷━━┷┷┷┷┷┷┷━┷┷┷┷┷━━┷┷┷┷┷┷━╣≈≈≈≈≈≈╠━━┷┷━━━┷┷┷┷┷┷┷━",
    " ▒▒▒ ─ ─ ▒▒▒▒ ─ ─ ▒▒▒▒ ─ ─ ▒▒▒▒ ─ ─ ▒║≈≈≈≈≈≈║▒ ─ ─ ▒▒▒▒ ─ ─ ▒",
    "▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓║≈≈≈≈≈≈║▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓",
    "▓▓▓▒▒▓▓▓▓▓▒▒▒▓▓▓▓▓▓▒▒▓▓▓▓▓▓▓▒▒▒▓▓▓▓▓▓╚≈≈≈≈≈≈╝▓▓▒▒▓▓▓▓▓▒▒▒▓▓▓▓",
    "▓░░▓▓▓░░▓▓▓▓░░▓▓▓░░▓▓▓▓░░▓▓▓░░▓▓▓▓░░▓▓▓≈≈≈≈▓▓▓▓░░▓▓▓░░▓▓▓▓░░▓",
    "░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░",
]
_WIDTH = max(len(r) for r in _ART)
_ART = [r.ljust(_WIDTH) for r in _ART]

_GHOST_STYLE = "grey30"
_NOZZLE_STYLE = "bold cyan"


def _srgb_hex(rgb: tuple[float, float, float]) -> str:
    """The pipeline's colours are linear RGB; the terminal wants sRGB."""
    return "#" + "".join(f"{round(255 * c ** (1 / 2.2)):02x}" for c in rgb)


_C = {cat: _srgb_hex(CATEGORY_COLORS[cat])
      for cat in ("terrain", "buildings", "roofs", "roads", "water",
                  "trees", "base")}
_CHAR_STYLES = {
    "█": _C["buildings"], "▪": "grey35",                    # walls, windows
    "▄": _C["roofs"], "▀": _C["roofs"],                     # roof caps/decks
    "♣": _C["trees"], "│": "#8b694a",                       # crowns, trunks
    "━": _C["terrain"], "┷": _C["terrain"], "▓": _C["terrain"],
    "▒": _C["roads"], "─": "grey70",                        # road, markings
    "≈": _C["water"], "║": _C["water"], "═": _C["water"],   # river + channel
    "╔": _C["water"], "╗": _C["water"], "╚": _C["water"],
    "╝": _C["water"], "╠": _C["water"], "╣": _C["water"],
    "░": _C["base"],
}
# legacy consoles (cp1252 etc.) cannot encode the block glyphs
_ASCII_TRANS = str.maketrans("█▄▀▪♣│━┷─▒▓≈║╔╗╚╝╠╣═░",
                             "###.*|==-:%~|++++++=.")


def _console_mode(console: Console) -> tuple[bool, str, str, str]:
    """(unicode ok, nozzle, done marker, spinner) the console can encode.

    Rich downgrades its own bars and boxes on legacy consoles, but not custom
    glyphs or spinner frames, so cp1252 consoles get the ASCII variants.
    """
    probe = "".join(_CHAR_STYLES) + "▼✓⠋"
    enc = getattr(console.file, "encoding", None) or "ascii"
    try:
        probe.encode(enc)
        return True, "▼", "✓ finished", "dots"
    except (UnicodeEncodeError, LookupError):
        return False, "v", "finished", "line"


class PipelineUI:
    """Live progress display for one `generate()` run.

    ``n_steps`` fixes the overall bar's total and how fast the print grows;
    call :meth:`step` at the start of each step and :meth:`log` for detail
    lines. Use as a context manager.
    """

    def __init__(self, n_steps: int, title: str,
                 console: Console | None = None):
        self.console = console or Console()
        self.enabled = self.console.is_terminal
        self._total = max(1, n_steps)
        self._done = 0
        self._started = False
        self._finished = False
        (self._unicode, self._nozzle, self._done_marker,
         spinner) = _console_mode(self.console)
        # expand + Constrain (in _render) pin both bars to the panel's width
        self._steps = Progress(
            SpinnerColumn(spinner_name=spinner),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=self.console, expand=True)
        self._task = self._steps.add_task(title, total=self._total)
        self._downloads = Progress(
            TextColumn("  [dim]{task.description}[/dim]"),
            BarColumn(bar_width=None),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=self.console, expand=True)
        self._dl_tasks: dict[str, int] = {}
        self._title = title
        self._art_cache: tuple | None = None
        # modest refresh rate: every frame rewrites the whole (tall) live
        # region, which reads as flicker when overdone
        self._live = Live(get_renderable=self._render, console=self.console,
                          refresh_per_second=4)

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "PipelineUI":
        if self.enabled:
            download.set_progress_hook(self._on_download)
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.enabled:
            download.set_progress_hook(None)
            if exc_type is None:
                self._finished = True
                self._done = self._total
                self._steps.update(self._task, completed=self._total,
                                   description=self._title)
            self._live.__exit__(exc_type, exc, tb)

    # -- pipeline-facing API ------------------------------------------------

    def step(self, label: str) -> None:
        """Mark the previous step finished and start a new one."""
        if self._started:
            self._done += 1
        self._started = True
        if self.enabled:
            self._steps.update(self._task, completed=self._done,
                               description=label)
        else:
            print(f"[{self._done + 1}/{self._total}] {label}")

    def log(self, msg: str = "") -> None:
        if self.enabled:
            self._live.console.print(msg, highlight=False)
        else:
            print(msg)

    def detail(self, msg: str) -> None:
        """A secondary, consistently styled log line under the current step."""
        if self.enabled:
            self._live.console.print(Text("      " + msg, style="grey62"))
        else:
            print("      " + msg)

    def model_table(self, parts) -> None:
        """Per-part mesh stats; ``parts`` rows are (name, verts, faces, rgb)."""
        swatch = "■" if self._unicode else "#"
        if not self.enabled:
            for name, v, f, _rgb in parts:
                print(f"      {name:14} {len(v):>10,} verts "
                      f"{len(f):>10,} faces")
            return
        table = Table(box=box.SIMPLE, header_style="bold grey62",
                      padding=(0, 2), pad_edge=False)
        table.add_column("part")
        table.add_column("vertices", justify="right")
        table.add_column("faces", justify="right")
        for name, v, f, rgb in parts:
            label = Text(swatch + " ", style=_srgb_hex(rgb))
            label.append(name, style="default")
            table.add_row(label, f"{len(v):,}", f"{len(f):,}")
        self._live.console.print(Padding(table, (0, 0, 0, 6)))

    # -- rendering ----------------------------------------------------------

    def _art_panel(self) -> Panel:
        """The print panel; cached, since it only changes on step boundaries."""
        state = (self._done, self._finished)
        if self._art_cache is not None and self._art_cache[0] == state:
            return self._art_cache[1]

        rows = len(_ART)
        printed = round(rows * self._done / self._total)
        cut = rows - printed          # first fully printed row index
        art = Text()

        # printhead: sweeps with progress, lifts off the print at 100%
        frac = self._done / self._total
        x = min(_WIDTH - 1, round(frac * (_WIDTH - 1)))
        art.append(" " * x + (self._nozzle if not self._finished else " ")
                   + "\n", style=_NOZZLE_STYLE)

        for i, row in enumerate(_ART):
            if not self._unicode:
                row = row.translate(_ASCII_TRANS)
            for ch, orig in zip(row, _ART[i]):
                if orig == " ":
                    art.append(" ")
                    continue
                art.append(ch, style=(_CHAR_STYLES[orig] if i >= cut
                                      else _GHOST_STYLE))
            art.append("\n")
        art.rstrip()

        subtitle = (f"[green]{self._done_marker}[/green]" if self._finished
                    else f"layer {printed}/{rows}")
        panel = Panel.fit(art, title=f"[bold]{self._title}[/bold]",
                          subtitle=subtitle, border_style="grey50",
                          padding=(0, 2))
        self._art_cache = (state, panel)
        return panel

    def _render(self):
        # constant-height layout: a download bar slot is always reserved, so
        # bars appearing/vanishing never shift the region (which flickers).
        # Both bars are constrained to the panel's width (art + padding/border)
        width = _WIDTH + 6
        return Group(self._art_panel(),
                     Constrain(self._steps.get_renderable(), width),
                     Constrain(self._downloads.get_renderable(), width)
                     if self._dl_tasks else Text(""))

    # -- download progress hook ----------------------------------------------

    def _on_download(self, name: str, completed: int, total: int) -> None:
        if not self.enabled:
            return
        tid = self._dl_tasks.get(name)
        if tid is None:
            tid = self._downloads.add_task(name, total=total or None)
            self._dl_tasks[name] = tid
        self._downloads.update(tid, completed=completed,
                               total=total or None)
        if total and completed >= total:
            self._downloads.remove_task(tid)
            del self._dl_tasks[name]


def print_outputs(manifest: dict) -> None:
    """Final file listing: one row per output, clickable where the terminal
    supports OSC 8 hyperlinks (rich drops the links elsewhere)."""
    console = Console()
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 1))
    table.add_column(style="bold")
    table.add_column()
    rows = [("glb", "3D scene (GLB)"), ("threemf", "print file (3MF)"),
            ("blend", "Blender file"), ("preview", "preview render")]
    for key, label in rows:
        path = Path(manifest.get(key, ""))
        if path.name and path.exists():
            size = path.stat().st_size / 1e6
            table.add_row(label, f"[link={path.as_uri()}]{path.name}[/link] "
                                 f"[dim]({size:.1f} MB)[/dim]")
        else:
            table.add_row(label, "[dim]skipped[/dim]")
    out_dir = Path(manifest["glb"]).parent
    console.print()
    console.print(table)
    console.print(f" [dim]in[/dim] [link={out_dir.as_uri()}]{out_dir}[/link]")
