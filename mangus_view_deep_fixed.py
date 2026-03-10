#!/usr/bin/env python3
"""
Mangus Viewer - Local Manga/Manhua/Manhwa Reader (Neon Violet UI)

Features (v11):
- Reading modes (2 buttons in reader controls): Vertical / Horizontal (RTL)
  - Horizontal uses a 2-page spread (pairs pages like a book) for *chapter* views
- Switching Vertical <-> Horizontal keeps your current page (no jump to top)
  - Stable for Chapter + Read-all (Continuous) modes, even after repeated switching
- Mouse-wheel sideways scrolling in Horizontal (deltaY -> deltaX)
- Fit width behavior:
  - Chapter view: does NOT force Fit width when switching to Horizontal
  - Read-all (Continuous) view: DOES force Fit width when switching to Horizontal
- Zoom/Fit changes keep your current page in Read-all mode (prevents "page jumps")
- Auto-resume last position per chapter / continuous(all) / PDF-view (persists across restarts)
- "Saves" section on Home (/browse/) to continue later; shows:
  - last page thumbnail, chapter folder path, and last updated time
- Search (header "Browse" button toggles a search bar):
  - type a manga name, press Enter, get results with chapter counts and links
- Home button fixed top-left (floating)
- Return-to-start floating button (bottom-right)
- Optional magnifier (zoom lens) overlay for images (toggle button 🔍 or key m)
  - Lens diameter increased by ~10% vs v6
- Continuous "Read all" mode:
  - chapter list is a fixed dropdown on the top-right
  - dropdown shows chapter progress like: "Ch. 12 (34%)"
  - progress updates LIVE while you scroll (best-effort)

Removed (v11):
- Open folder button in the top bar (kept server capability but not shown in UI)

Where progress/saves are stored:
- ~/.mangus_viewer_state.json

Notes:
- PDF thumbnails require PyMuPDF: pip install pymupdf
- The viewer runs locally, serving files from the selected root directory.

Run:
  python mangus_view_fixed_v11.py "/home/OTH/Documents/Mangus" --debug
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Tuple

# Optional PDF thumbnails (first page render)
try:
    import fitz  # PyMuPDF

    HAS_PDF_THUMB = True
except Exception:  # pragma: no cover
    fitz = None  # type: ignore[assignment]
    HAS_PDF_THUMB = False


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}

mimetypes.init()


def natural_key(s: str) -> list[object]:
    parts = re.split(r"(\d+)", s)
    out: list[object] = []
    for p in parts:
        out.append(int(p) if p.isdigit() else p.casefold())
    return out


def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.casefold() in IMAGE_EXTS


def is_pdf_file(p: Path) -> bool:
    return p.is_file() and p.suffix.casefold() in PDF_EXTS


def guess_mime(p: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(p))
    if ctype:
        return ctype
    ext = p.suffix.casefold()
    fallback = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".pdf": "application/pdf",
    }
    return fallback.get(ext, "application/octet-stream")


def url_quote_path(rel_posix: str) -> str:
    return urllib.parse.quote(rel_posix, safe="/")


def safe_resolve_under_root(root: Path, rel_posix: str) -> Path:
    root = root.resolve()
    rel_posix = rel_posix.lstrip("/")
    rel_parts = [x for x in rel_posix.split("/") if x not in ("", ".")]
    if not rel_parts:
        return root
    candidate = root.joinpath(*rel_parts).resolve()
    if candidate == root:
        return candidate
    if root not in candidate.parents:
        raise PermissionError(f"Path escapes root: {rel_posix}")
    return candidate


def to_posix_rel(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


def build_img_url(rel_dir: str, filename: str) -> str:
    if rel_dir:
        return f"/img/{url_quote_path(rel_dir)}/{urllib.parse.quote(filename)}"
    return f"/img/{urllib.parse.quote(filename)}"


def build_pdf_url(rel_dir: str, filename: str) -> str:
    if rel_dir:
        return f"/pdf/{url_quote_path(rel_dir)}/{urllib.parse.quote(filename)}"
    return f"/pdf/{urllib.parse.quote(filename)}"


def build_pdf_thumb_url(rel_dir: str, filename: str) -> str:
    if rel_dir:
        return f"/pdfthumb/{url_quote_path(rel_dir)}/{urllib.parse.quote(filename)}"
    return f"/pdfthumb/{urllib.parse.quote(filename)}"


def view_id(kind: str, rel_dir: str, pdf_name: str = "") -> str:
    return f"{kind}|{rel_dir}|{pdf_name}"


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return ""


@dataclass(frozen=True)
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 0
    open_browser: bool = True
    debug: bool = False


class Logger:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._lock = threading.Lock()

    def info(self, msg: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            print(msg, flush=True)

    def error(self, msg: str) -> None:
        with self._lock:
            print(msg, file=sys.stderr, flush=True)


class StateStore:
    def __init__(self, path: Path, logger: Logger, max_items: int = 80) -> None:
        self._path = path
        self._logger = logger
        self._lock = threading.RLock()
        self._max_items = max_items
        self._data: dict[str, Any] = {"version": 1, "saves": []}
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                if self._path.exists():
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                if "saves" not in self._data or not isinstance(self._data["saves"], list):
                    self._data["saves"] = []
            except Exception as e:
                self._logger.error(f"[state] load failed: {e}")
                self._data = {"version": 1, "saves": []}

    def _save(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._path)
            except Exception as e:
                self._logger.error(f"[state] save failed: {e}")

    def list_saves(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._data.get("saves", []))
        items.sort(key=lambda x: float(x.get("updated_at", 0.0)), reverse=True)
        return items[:limit]

    def get(self, kind: str, rel_dir: str, pdf_name: str = "") -> Optional[dict[str, Any]]:
        vid = view_id(kind, rel_dir, pdf_name)
        with self._lock:
            for s in self._data.get("saves", []):
                if s.get("id") == vid:
                    return dict(s)
        return None

    def upsert(self, item: dict[str, Any]) -> None:
        vid = item.get("id")
        if not vid:
            return
        with self._lock:
            saves = self._data.get("saves", [])
            for i, s in enumerate(saves):
                if s.get("id") == vid:
                    if not item.get("thumb") and s.get("thumb"):
                        item["thumb"] = s.get("thumb")
                    saves[i] = item
                    break
            else:
                saves.append(item)

            saves.sort(key=lambda x: float(x.get("updated_at", 0.0)), reverse=True)
            self._data["saves"] = saves[: self._max_items]
        self._save()

    def delete(self, save_id: str) -> None:
        with self._lock:
            saves = [s for s in self._data.get("saves", []) if s.get("id") != save_id]
            self._data["saves"] = saves
        self._save()


class FolderPickBroker:
    def __init__(self, logger: Logger):
        self._logger = logger
        self._req_q: queue.Queue[Tuple[threading.Event, dict]] = queue.Queue()

    def request_pick(self, title: str = "Select a folder") -> Optional[Path]:
        evt = threading.Event()
        payload: dict = {"title": title, "result": None, "error": None}
        self._req_q.put((evt, payload))
        evt.wait()
        err = payload.get("error")
        if err:
            self._logger.error(f"[pick-root] picker error: {err}")
        return payload.get("result")

    def _fulfill_one(self) -> bool:
        try:
            evt, payload = self._req_q.get_nowait()
        except queue.Empty:
            return False

        title = payload.get("title") or "Select a folder"
        try:
            picked = pick_folder_tk(title)
            payload["result"] = picked
        except Exception as e:
            payload["error"] = str(e)
        finally:
            evt.set()
        return True

    def pump(self) -> None:
        while self._fulfill_one():
            pass


def pick_folder_tk(title: str) -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError(f"Tkinter unavailable: {e}") from e

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.lift()
        root.focus_force()
        root.update()
    except Exception:
        pass

    selected = filedialog.askdirectory(title=title)

    try:
        root.destroy()
    except Exception:
        pass

    if not selected:
        return None
    return Path(selected)


class Library:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def list_subdirs(self, rel_dir: str) -> list[Path]:
        folder = safe_resolve_under_root(self.root, rel_dir)
        if not folder.exists() or not folder.is_dir():
            return []
        out: list[Path] = []
        try:
            for entry in os.scandir(folder):
                if entry.is_dir():
                    out.append(Path(entry.path))
        except OSError:
            return []
        out.sort(key=lambda p: natural_key(p.name))
        return out

    def list_images(self, rel_dir: str) -> list[Path]:
        folder = safe_resolve_under_root(self.root, rel_dir)
        if not folder.exists() or not folder.is_dir():
            return []
        imgs: list[Path] = []
        try:
            for entry in os.scandir(folder):
                if entry.is_file():
                    p = Path(entry.path)
                    if is_image_file(p):
                        imgs.append(p)
        except OSError:
            return []
        imgs.sort(key=lambda p: natural_key(p.name))
        return imgs

    def list_pdfs(self, rel_dir: str) -> list[Path]:
        folder = safe_resolve_under_root(self.root, rel_dir)
        if not folder.exists() or not folder.is_dir():
            return []
        pdfs: list[Path] = []
        try:
            for entry in os.scandir(folder):
                if entry.is_file():
                    p = Path(entry.path)
                    if is_pdf_file(p):
                        pdfs.append(p)
        except OSError:
            return []
        pdfs.sort(key=lambda p: natural_key(p.name))
        return pdfs

    def folder_has_images(self, rel_dir: str) -> bool:
        return bool(self.list_images(rel_dir))

    def folder_has_pdfs(self, rel_dir: str) -> bool:
        return bool(self.list_pdfs(rel_dir))

    def get_first_image(self, rel_dir: str) -> Optional[Path]:
        imgs = self.list_images(rel_dir)
        return imgs[0] if imgs else None

    def chapter_kind(self, rel_dir: str) -> Optional[str]:
        if self.folder_has_images(rel_dir):
            return "chapter"
        if self.folder_has_pdfs(rel_dir):
            return "pdf_folder"
        return None

    def chapter_children(self, rel_dir: str) -> list[str]:
        out: list[str] = []
        for d in self.list_subdirs(rel_dir):
            r = to_posix_rel(self.root, d)
            if self.chapter_kind(r) is not None:
                out.append(r)
        return out

    def siblings_with_content(self, rel_dir: str) -> tuple[Optional[str], Optional[str]]:
        if not rel_dir:
            return None, None
        current = safe_resolve_under_root(self.root, rel_dir)
        parent = current.parent
        if parent == current:
            return None, None

        parent_rel = to_posix_rel(self.root, parent)
        sibs = self.list_subdirs(parent_rel)
        sib_rels = [to_posix_rel(self.root, s) for s in sibs]
        chapters = [r for r in sib_rels if self.chapter_kind(r) is not None]

        if not chapters:
            return None, None
        try:
            idx = chapters.index(rel_dir)
        except ValueError:
            return None, None

        prev_rel = chapters[idx - 1] if idx > 0 else None
        next_rel = chapters[idx + 1] if idx + 1 < len(chapters) else None
        return prev_rel, next_rel


def search_library(lib: Library, query: str, limit: int = 40) -> list[dict[str, Any]]:
    q = (query or "").strip().casefold()
    if not q:
        return []

    results: list[dict[str, Any]] = []
    root = lib.root

    def want_dir(name: str) -> bool:
        return not name.startswith(".")

    for dirpath, dirnames, _filenames in os.walk(root):
        rel = Path(dirpath).resolve().relative_to(root).as_posix() if Path(dirpath).resolve() != root else ""
        depth = 0 if not rel else rel.count("/") + 1
        dirnames[:] = [d for d in dirnames if want_dir(d)]
        if depth > 6:
            dirnames[:] = []
            continue

        name = Path(dirpath).name if rel else root.name
        if q not in name.casefold():
            continue

        rel_dir = rel
        kind = lib.chapter_kind(rel_dir)
        kids = lib.chapter_children(rel_dir)
        if not kind and not kids:
            continue

        results.append({"rel": rel_dir, "name": name, "kind": kind or "folder", "chapters": len(kids)})
        if len(results) >= limit:
            break

    results.sort(key=lambda x: natural_key(x.get("name", "")))
    return results[:limit]


def render_base_html(title: str, body_html: str, page_kind: str) -> bytes:
    css = """
    :root{
      --bg0:#050009;
      --bg1:#090012;
      --card:rgba(20,8,35,.72);
      --card2:rgba(30,12,55,.62);
      --fg:#f7eaff;
      --muted:rgba(247,234,255,.75);
      --accent:#b84dff;
      --accent2:#62f5ff;
      --accent3:#ff4df6;
      --glow: 0 0 14px rgba(184,77,255,.55), 0 0 34px rgba(184,77,255,.30);
      --glow2: 0 0 12px rgba(98,245,255,.45), 0 0 26px rgba(98,245,255,.20);
      --radius:18px;
      --shadow: 0 18px 70px rgba(0,0,0,.60);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    html{
      background:
        radial-gradient(1200px 800px at 20% 10%, rgba(184,77,255,.18), transparent 55%),
        radial-gradient(900px 700px at 80% 0%, rgba(98,245,255,.12), transparent 60%),
        radial-gradient(900px 700px at 70% 90%, rgba(255,77,246,.10), transparent 60%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      background-attachment: fixed, fixed, fixed, fixed;
      background-repeat: no-repeat;
      background-size: cover;
    }
    body{
      margin:0;
      font-family:var(--sans);
      color:var(--fg);
      background: transparent;
      overflow-y: scroll;
      overflow-x: hidden;
    }
    a{color:inherit; text-decoration:none}
    .wrap{max-width:1400px; margin:0 auto; padding:18px}

    .header{
      position:sticky; top:0; z-index:100;
      padding:14px 18px;
      background: linear-gradient(180deg, rgba(30,10,55,.78), rgba(15,5,30,.58));
      border-bottom:1px solid rgba(184,77,255,.22);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }
    .header-inner{
      max-width:1400px; margin:0 auto;
      display:flex; align-items:center; gap:12px; flex-wrap:wrap;
    }
    .brand{display:flex; flex-direction:column; gap:2px; min-width: 240px}
    .brand .t{font-weight:900; letter-spacing:.2px}
    .brand .s{font-size:12px; color:var(--muted); font-family:var(--mono); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:520px}
    .spacer{flex:1}

    .btn{
      display:inline-flex; align-items:center; justify-content:center; gap:8px;
      padding:10px 14px;
      border-radius: 14px;
      border:1px solid rgba(184,77,255,.35);
      background: linear-gradient(180deg, rgba(184,77,255,.18), rgba(30,12,55,.35));
      box-shadow: var(--glow);
      color:var(--fg);
      cursor:pointer;
      user-select:none;
      transition: transform .15s ease, filter .15s ease;
      white-space: nowrap;
    }
    .btn:hover{transform: translateY(-1px); filter:brightness(1.05)}
    .btn:active{transform: translateY(0px); filter:brightness(.98)}
    .btn.secondary{
      border-color: rgba(98,245,255,.28);
      background: linear-gradient(180deg, rgba(98,245,255,.10), rgba(30,12,55,.35));
      box-shadow: var(--glow2);
    }
    .btn.ghost{
      background: rgba(255,255,255,.02);
      box-shadow:none;
      border-color: rgba(247,234,255,.14);
    }
    .btn.small{padding:7px 10px; border-radius: 12px; font-size: 12px}
    .btn:disabled{opacity:.45; cursor:not-allowed}
    .btn.active{
      border-color: rgba(98,245,255,.38);
      box-shadow: var(--glow2);
      filter: brightness(1.04);
    }

    .pill{
      padding:8px 10px;
      border-radius: 999px;
      border:1px solid rgba(184,77,255,.26);
      background: rgba(0,0,0,.20);
      font-size:12px;
      color:var(--muted);
      font-family:var(--mono);
      white-space: nowrap;
    }

    .crumbs{
      margin-top:16px;
      padding:10px 12px;
      border-radius: var(--radius);
      border: 1px solid rgba(184,77,255,.18);
      background: rgba(0,0,0,.18);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      overflow:auto;
      white-space: nowrap;
    }
    .crumbs a{color: rgba(247,234,255,.92)}
    .crumbs .sep{opacity:.6; padding:0 8px}

    .hint{
      margin-top:12px;
      padding:12px 14px;
      border-radius: var(--radius);
      border: 1px solid rgba(98,245,255,.18);
      background: rgba(0,0,0,.14);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      line-height:1.55;
    }
    .kbd{font-family:var(--mono); padding:2px 6px; border-radius:8px;
         border:1px solid rgba(184,77,255,.26); background: rgba(0,0,0,.22); color:var(--fg)}

    .grid{
      margin-top:14px;
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap:14px;
    }
    .card{
      border-radius: var(--radius);
      background: linear-gradient(180deg, var(--card), var(--card2));
      border:1px solid rgba(184,77,255,.22);
      box-shadow: 0 12px 44px rgba(0,0,0,.45);
      overflow:hidden;
    }
    .thumb{
      width:100%;
      aspect-ratio: 2/3;
      background: radial-gradient(420px 120px at 25% 10%, rgba(184,77,255,.18), transparent 55%),
                  linear-gradient(135deg, rgba(0,0,0,.20), rgba(184,77,255,.06));
      display:flex; align-items:center; justify-content:center;
      position:relative;
      overflow:hidden;
    }
    .thumb img{width:100%; height:100%; object-fit:cover}
    .thumb .ph{font-size:48px; opacity:.45}
    .info{padding:12px 12px 10px 12px}
    .info .h{margin:0 0 6px 0; font-weight:800; font-size: 14px; word-break:break-word}
    .info .m{margin:0; color:var(--muted); font-size: 12px; font-family: var(--mono); word-break:break-word}
    .actions{display:flex; gap:10px; padding:0 12px 12px 12px; flex-wrap:wrap}
    .actions .btn{flex:1; min-width: 110px}

    /* Reader */
    .progressWrap{position:fixed; left:0; top:0; width:100%; height:3px; z-index:9999; background: rgba(255,255,255,.03)}
    .progressBar{
      height:100%; width:0%;
      background: linear-gradient(90deg, rgba(184,77,255,.90), rgba(98,245,255,.85), rgba(255,77,246,.85));
      box-shadow: 0 0 14px rgba(184,77,255,.55);
      transition: width .08s linear;
    }
    .reader{max-width:1100px; margin: 0 auto; padding: 18px}
    .reader-controls{
      position: sticky;
      top: 76px;
      z-index: 90;
      margin-top: 12px;
      border-radius: var(--radius);
      border: 1px solid rgba(184,77,255,.22);
      background: rgba(0,0,0,.20);
      backdrop-filter: blur(10px);
      padding: 10px 12px;
      display:flex;
      align-items:center;
      gap:10px;
      flex-wrap:wrap;
      box-shadow: 0 12px 44px rgba(0,0,0,.30);
    }
    .pages{margin-top: 14px; display:flex; flex-direction:column; gap:12px; align-items:center}
    .page{width:100%; display:flex; justify-content:center}
    .page img{
      width: var(--imgWidth, 60%);
      max-width: var(--imgMax, 1400px);
      height:auto;
      border-radius: 14px;
      border:1px solid rgba(184,77,255,.16);
      box-shadow: 0 18px 60px rgba(0,0,0,.55), var(--glow);
      background: rgba(0,0,0,.25);
    }
    .sheet{width: 50vw; display:flex; justify-content:center}
    .sheet img{width: var(--imgWidth, 100%); max-width: 100%;}

    input[type="range"]{accent-color: var(--accent);}

    /* PDF */
    .pdfwrap{
      width:100%;
      border-radius: var(--radius);
      border: 1px solid rgba(184,77,255,.22);
      overflow:hidden;
      box-shadow: 0 18px 60px rgba(0,0,0,.55), var(--glow);
      background: rgba(0,0,0,.25);
      height: calc(100vh - 230px);
      min-height: 560px;
    }
    .pdfwrap iframe{width:100%; height:100%; border:0; background: transparent}
    select{
      background: rgba(0,0,0,.25);
      border: 1px solid rgba(184,77,255,.26);
      color: var(--fg);
      border-radius: 12px;
      padding: 8px 10px;
      font-family: var(--mono);
    }

    /* Floating buttons */
    .fab{
      position: fixed;
      right: 18px;
      bottom: 18px;
      width: 46px;
      height: 46px;
      border-radius: 16px;
      border: 1px solid rgba(184,77,255,.32);
      background: linear-gradient(180deg, rgba(184,77,255,.20), rgba(30,12,55,.45));
      box-shadow: var(--glow);
      color: var(--fg);
      cursor: pointer;
      z-index: 10000;
      opacity: 0;
      pointer-events: none;
      transform: translateY(6px);
      transition: opacity .15s ease, transform .15s ease, filter .15s ease;
      display:flex; align-items:center; justify-content:center;
      text-decoration:none;
    }
    .fab:hover{filter:brightness(1.06)}
    .fab.show{opacity:1; pointer-events:auto; transform: translateY(0)}
    .fabPage{bottom: 72px;}

    .reader-controls.can-hide{
      transition: transform .18s ease, opacity .18s ease, top .18s ease;
      transform: translateY(0);
    }
    .reader-controls.can-hide.hiddenTop{
      transform: translateY(calc(-100% + 12px));
      opacity: .72;
    }

    .fabHome{
      position: fixed;
      left: 18px;
      top: 18px;
      width: 46px;
      height: 46px;
      border-radius: 16px;
      border: 1px solid rgba(184,77,255,.32);
      background: linear-gradient(180deg, rgba(184,77,255,.20), rgba(30,12,55,.45));
      box-shadow: var(--glow);
      color: var(--fg);
      cursor: pointer;
      z-index: 10000;
      display:flex; align-items:center; justify-content:center;
      text-decoration:none;
      transition: transform .15s ease, filter .15s ease;
    }
    .fabHome:hover{transform: translateY(-1px); filter:brightness(1.06)}
    .fabHome:active{transform: translateY(0px); filter:brightness(.98)}

    .fabToc{
      position: fixed;
      right: 18px;
      top: 18px;
      z-index: 10000;
      min-width: 220px;
      max-width: 520px;
      display: none;
    }
    .fabToc.show{display:block}
    .fabToc select{
      width: 100%;
      border-radius: 16px;
      padding: 10px 12px;
      border: 1px solid rgba(184,77,255,.32);
      background: linear-gradient(180deg, rgba(184,77,255,.10), rgba(0,0,0,.28));
      box-shadow: var(--glow);
      color: var(--fg);
    }

    /* Magnifier (diameter +10%) */
    .magnifier{
      position: fixed;
      width: 242px;
      height: 242px;
      border-radius: 50%;
      border: 2px solid rgba(98,245,255,.35);
      box-shadow: var(--glow2);
      background-repeat: no-repeat;
      z-index: 10001;
      pointer-events: none;
      display: none;
      backdrop-filter: blur(2px);
    }
    .magnifier.on{display:block}

    /* Search panel */
    .searchPanel{
      display:none;
      width: 100%;
      max-width: 1400px;
      margin: 10px auto 0 auto;
      padding: 0 18px 12px 18px;
    }
    .searchPanel.show{display:block}
    .searchRow{
      display:flex; gap:10px; align-items:center; flex-wrap:wrap;
      padding: 10px 12px;
      border-radius: var(--radius);
      border: 1px solid rgba(98,245,255,.18);
      background: rgba(0,0,0,.14);
      box-shadow: 0 12px 44px rgba(0,0,0,.25);
    }
    .searchRow input{
      flex: 1;
      min-width: 220px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(184,77,255,.26);
      background: rgba(0,0,0,.22);
      color: var(--fg);
      font-family: var(--mono);
      outline: none;
    }

    /* Horizontal mode: only on reader pages */
    html[data-page="reader"][data-reading="h"] body{overflow: hidden;}
    html[data-page="reader"][data-reading="h"] .reader{max-width:none; padding: 0;}
    html[data-page="reader"][data-reading="h"] .reader-controls{margin: 12px 18px 10px 18px;}
    html[data-page="reader"][data-reading="h"] .pages{
      flex-direction: row;
      direction: rtl;
      overflow-x: auto;
      overflow-y: hidden;
      align-items: center;
      width: 100vw;
      height: calc(100vh - 190px);
      -webkit-overflow-scrolling: touch;
      padding: 0 18px;
      margin: 0;
      gap: 18px;
    }
    html[data-page="reader"][data-reading="h"] .page{
      flex: 0 0 auto;
      width: auto;
      align-items: center;
      justify-content: center;
      padding: 0;
    }
    html[data-page="reader"][data-reading="h"] .page img{
      width: var(--imgWidth, 60%);
      max-width: none;
      max-height: calc(100vh - 220px);
      height: auto;
    }

    /* Spread layout (chapter view) */
    html[data-page="reader"][data-reading="h"] .pages.spreadOn{
      scroll-snap-type: x mandatory;
      gap: 0;
      padding: 0;
    }
    html[data-page="reader"][data-reading="h"] .page.spread{
      flex: 0 0 100vw;
      width: 100vw;
      scroll-snap-align: center;
      padding: 0 18px;
      display:flex;
      flex-direction: row;
      direction: rtl;
      align-items: center;
      justify-content: center;
      gap: 18px;
    }
    html[data-page="reader"][data-reading="h"] .page.spread img{
      max-height: calc(100vh - 220px);
      height: auto;
    }

    html[data-page="reader"][data-reading="h"] .pages::-webkit-scrollbar{height:10px}

    ::-webkit-scrollbar{width:10px}
    ::-webkit-scrollbar-track{background: rgba(0,0,0,.18)}
    ::-webkit-scrollbar-thumb{
      background: rgba(184,77,255,.30);
      border: 1px solid rgba(184,77,255,.35);
      border-radius: 999px;
      box-shadow: var(--glow);
    }
    """

    js = r"""
    function clamp(n, a, b){ return Math.max(a, Math.min(b, n)); }
    function pct(idx, total){
      total = Math.max(1, total|0);
      if (total <= 1) return null;
      idx = clamp(idx|0, 0, total-1);
      return Math.round((idx / (total-1)) * 100);
    }

    function getReadingMode(){ return (document.documentElement.dataset.reading || 'v'); }
    function setReadingMode(mode){
      localStorage.setItem('mv_reading', mode);
      document.documentElement.dataset.reading = mode;
    }
    function getPageKind(){ return document.documentElement.dataset.page || 'browse'; }

    function getScroller(){
      const mode = getReadingMode();
      if (getPageKind() === 'reader' && mode === 'h') {
        const pages = document.querySelector('.pages');
        if (pages) return pages;
      }
      return document.scrollingElement || document.documentElement;
    }

    function setCSSVars(widthPct, fitWidth){
      const r = document.documentElement;
      if (fitWidth) {
        r.style.setProperty('--imgWidth', '100%');
        r.style.setProperty('--imgMax', '100%');
      } else {
        r.style.setProperty('--imgWidth', widthPct + '%');
        r.style.setProperty('--imgMax', '1400px');
      }
    }

    function throttle(fn, ms){
      let last = 0;
      let timer = null;
      return function(...args){
        const now = Date.now();
        const rem = ms - (now - last);
        if (rem <= 0) { last = now; fn.apply(this, args); }
        else if (!timer) {
          timer = setTimeout(() => { timer = null; last = Date.now(); fn.apply(this, args); }, rem);
        }
      };
    }

    function getViewMeta(){
      const el = document.getElementById('viewMeta');
      if(!el) return null;
      return { kind: el.dataset.kind||'', rel: el.dataset.rel||'', pdf: el.dataset.pdf||'', title: el.dataset.title||'', key: el.dataset.key||'' };
    }

    function encPath(p){
      if (!p) return '';
      return p.split('/').map(encodeURIComponent).join('/');
    }

    function makePdfThumb(rel, pdf){
      if (!pdf) return '';
      const relPart = rel ? encPath(rel) + '/' : '';
      return '/pdfthumb/' + relPart + encodeURIComponent(pdf);
    }

    async function apiGetProgress(meta){
      const u = new URL('/api/progress', location.origin);
      u.searchParams.set('kind', meta.kind);
      u.searchParams.set('rel', meta.rel);
      u.searchParams.set('pdf', meta.pdf || '');
      const res = await fetch(u.toString(), {cache:'no-store'});
      if(!res.ok) return null;
      return await res.json();
    }

    function apiSendProgress(payload){
      const body = JSON.stringify(payload);
      try{
        if (navigator.sendBeacon) {
          const blob = new Blob([body], {type:'application/json'});
          navigator.sendBeacon('/api/progress', blob);
          return;
        }
      }catch(e){}
      fetch('/api/progress', {method:'POST', headers:{'Content-Type':'application/json'}, body, keepalive:true}).catch(()=>{});
    }

    function imgEls(){
      return Array.from(document.querySelectorAll('img[data-img-index]'));
    }

    function idxFromVerticalScroll(){
      const imgs = imgEls();
      if (!imgs.length) return 0;
      const targetY = window.scrollY + Math.min(240, window.innerHeight * 0.35);
      let lo = 0, hi = imgs.length - 1, best = 0;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const top = imgs[mid].getBoundingClientRect().top + window.scrollY;
        if (top <= targetY) { best = mid; lo = mid + 1; }
        else { hi = mid - 1; }
      }
      const v = imgs[best].dataset.imgIndex;
      return v ? (parseInt(v, 10) || 0) : 0;
    }

    function findImgsAt(x, y){
      const els = document.elementsFromPoint(x, y);
      const imgs = [];
      for (const el of els) {
        if (el instanceof HTMLImageElement && el.dataset.imgIndex !== undefined) { imgs.push(el); continue; }
        if (el.closest) {
          const img = el.closest('img[data-img-index]');
          if (img) imgs.push(img);
        }
      }
      // Dedup by index
      const seen = new Set();
      const out = [];
      for (const img of imgs) {
        const idx = img.dataset.imgIndex || '';
        if (seen.has(idx)) continue;
        seen.add(idx);
        out.push(img);
      }
      return out;
    }

    function idxFromHorizontalCenter(){
      const cx = window.innerWidth * 0.5;
      const cy = window.innerHeight * 0.5;
      const probes = [
        [cx, cy],
        [window.innerWidth*0.35, cy],
        [window.innerWidth*0.65, cy],
        [cx, window.innerHeight*0.35],
        [cx, window.innerHeight*0.65],
      ];

      const candidates = [];
      for (const [x,y] of probes) {
        for (const img of findImgsAt(x,y)) candidates.push(img);
      }
      if (!candidates.length) return 0;

      // Choose nearest image center to viewport center
      let best = candidates[0];
      let bestD = 1e18;
      for (const img of candidates) {
        const r = img.getBoundingClientRect();
        const ix = (r.left + r.right) * 0.5;
        const iy = (r.top + r.bottom) * 0.5;
        const d = (ix-cx)*(ix-cx) + (iy-cy)*(iy-cy);
        if (d < bestD) { bestD = d; best = img; }
      }
      return parseInt(best.dataset.imgIndex || '0', 10) || 0;
    }

    function currentImgIndex(){
      const meta = getViewMeta();
      if (!meta) return 0;
      const mode = getReadingMode();
      if (mode === 'h' && getPageKind() === 'reader') return idxFromHorizontalCenter();
      return idxFromVerticalScroll();
    }

    function storeSwitchIndex(meta){
      if(!meta) return;
      sessionStorage.setItem('mv_switch_' + meta.key, String(currentImgIndex()));
    }

    function consumeSwitchIndex(meta){
      if(!meta) return null;
      const k = 'mv_switch_' + meta.key;
      const v = sessionStorage.getItem(k);
      if (v === null) return null;
      sessionStorage.removeItem(k);
      const idx = parseInt(v, 10);
      return Number.isFinite(idx) ? idx : null;
    }

    function scrollToImgIndex(idx){
      const pagesContainer = document.querySelector('.pages');
      if (!pagesContainer) return false;
      idx = Math.max(0, idx|0);
      const target = pagesContainer.querySelector(`img[data-img-index="${idx}"]`);
      if (!target) return false;

      const mode = getReadingMode();
      if (mode === 'h' && getPageKind() === 'reader') {
        target.closest('.page')?.scrollIntoView({behavior:'auto', inline:'center', block:'nearest'});
      } else {
        const top = target.getBoundingClientRect().top + window.scrollY - 140;
        window.scrollTo({top: Math.max(0, top), behavior:'auto'});
      }
      return true;
    }

    function keepCenteredAfterLayoutChange(fn){
      const meta = getViewMeta();
      if (!meta) { fn(); return; }
      // Only requested for Read-all stability
      if (meta.kind !== 'series') { fn(); return; }
      const idx = currentImgIndex();
      fn();
      // Recenter after layout settles
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToImgIndex(idx);
        });
      });
    }

    function desiredFitWidth(meta, mode){
      if (!meta || getPageKind() !== 'reader') return false;
      return meta.kind === 'series' && mode === 'h';
    }

    function applyModeFitRule(meta, mode){
      const fitWidth = desiredFitWidth(meta, mode);
      localStorage.setItem('mv_fit', fitWidth ? '1' : '0');
      return fitWidth;
    }

    function setupModeButtons(){
      const meta = getViewMeta();
      const vBtn = document.querySelector('[data-mode="v"]');
      const hBtn = document.querySelector('[data-mode="h"]');
      if(!vBtn || !hBtn || !meta) return;

      function sync(){
        const mode = getReadingMode();
        vBtn.classList.toggle('active', mode === 'v');
        hBtn.classList.toggle('active', mode === 'h');
      }

      vBtn.addEventListener('click', () => {
        if (getReadingMode() === 'v') return;
        storeSwitchIndex(meta);
        applyModeFitRule(meta, 'v');
        setReadingMode('v');
        location.reload();
      });

      hBtn.addEventListener('click', () => {
        if (getReadingMode() === 'h') return;
        storeSwitchIndex(meta);
        applyModeFitRule(meta, 'h');
        setReadingMode('h');
        location.reload();
      });

      sync();
    }

    function applySpreadGrouping(){
      const meta = getViewMeta();
      if(!meta || meta.kind !== 'chapter') return;
      if (getReadingMode() !== 'h' || getPageKind() !== 'reader') return;

      const container = document.querySelector('.pages');
      if(!container) return;

      const imgs = Array.from(container.querySelectorAll('img[data-img-index]'));
      if (imgs.length <= 1) return;

      container.classList.add('spreadOn');

      const frag = document.createDocumentFragment();
      for (let i = 0; i < imgs.length; i += 2) {
        const img1 = imgs[i];
        const img2 = (i+1 < imgs.length) ? imgs[i+1] : null;

        const spread = document.createElement('div');
        spread.className = 'page spread';

        const sheet1 = document.createElement('div'); sheet1.className = 'sheet';
        const sheet2 = document.createElement('div'); sheet2.className = 'sheet';

        sheet1.appendChild(img1);
        if (img2) sheet2.appendChild(img2);

        spread.appendChild(sheet1);
        if (img2) spread.appendChild(sheet2);
        frag.appendChild(spread);
      }

      Array.from(container.children).forEach(ch => ch.remove());
      container.appendChild(frag);
    }

    function setupHorizontalWheel(){
      if (getReadingMode() !== 'h' || getPageKind() !== 'reader') return;
      const scroller = document.querySelector('.pages');
      if(!scroller) return;

      requestAnimationFrame(() => { scroller.scrollTo({left: 0, behavior:'instant'}); });

      scroller.addEventListener('wheel', (e) => {
        if (e.ctrlKey) return;
        const delta = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
        if (delta === 0) return;
        e.preventDefault();
        const dir = getComputedStyle(scroller).direction;
        const dx = (dir === 'rtl') ? -delta : delta;
        scroller.scrollBy({left: dx, behavior:'auto'});
      }, {passive:false});
    }

    function scrollStep(sign, smooth){
      const scroller = getScroller();
      const behavior = smooth ? 'smooth' : 'auto';
      if (getReadingMode() === 'h' && getPageKind() === 'reader' && scroller instanceof Element) {
        const dir = getComputedStyle(scroller).direction;
        const base = window.innerWidth * 0.28;
        const dx = (dir === 'rtl' ? -1 : 1) * sign * base;
        scroller.scrollBy({left: dx, behavior});
        return;
      }
      window.scrollBy({top: sign * window.innerHeight * 0.85, behavior});
    }

    function advancePage(smooth){
      scrollStep(1, smooth);
    }

    function setupToStart(){
      const fab = document.querySelector('#toStart');
      const pageFab = document.querySelector('#pageAdvance');
      if(!fab) return;
      const scroller = getScroller();

      function scrollPos(){
        if (getReadingMode() === 'h' && getPageKind() === 'reader' && scroller instanceof Element) return Math.abs(scroller.scrollLeft||0);
        return (document.documentElement.scrollTop || document.body.scrollTop || 0);
      }

      function updateFab(){
        const pos = scrollPos();
        const show = pos > 240;
        fab.classList.toggle('show', show);
        pageFab?.classList.toggle('show', show);
      }

      function scrollToStart(){
        if (getReadingMode() === 'h' && getPageKind() === 'reader' && scroller instanceof Element) { scroller.scrollTo({left:0, behavior:'smooth'}); return; }
        window.scrollTo({top:0, behavior:'smooth'});
      }

      fab.addEventListener('click', scrollToStart);
      pageFab?.addEventListener('click', (e) => {
        e.preventDefault();
        advancePage(true);
      });

      const onScroll = throttle(updateFab, 100);
      if (scroller instanceof Element && scroller !== document.documentElement && scroller !== document.body) scroller.addEventListener('scroll', onScroll, {passive:true});
      else window.addEventListener('scroll', onScroll, {passive:true});
      updateFab();
    }

    function setupSearchBar(){
      const toggle = document.querySelector('#browseToggle');
      const panel = document.querySelector('#searchPanel');
      const input = document.querySelector('#searchInput');
      const go = document.querySelector('#searchGo');
      if(!toggle || !panel || !input) return;

      function show(on){
        panel.classList.toggle('show', on);
        if (on) setTimeout(() => input.focus(), 0);
      }

      toggle.addEventListener('click', () => { show(!panel.classList.contains('show')); });

      function submit(){
        const q = (input.value || '').trim();
        if (!q) return;
        const u = new URL('/search', location.origin);
        u.searchParams.set('q', q);
        location.href = u.toString();
      }

      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
        if (e.key === 'Escape') { e.preventDefault(); show(false); }
      });

      go?.addEventListener('click', submit);
    }

    function setupTocDropdown(){
      const toc = document.querySelector('#tocFloating');
      const sel = document.querySelector('#tocSelect');
      if(!toc || !sel) return;
      toc.classList.add('show');

      sel.addEventListener('change', () => {
        const id = sel.value;
        if (!id) return;
        const el = document.getElementById(id);
        if (el) el.scrollIntoView({behavior:'smooth', block:'start'});
        location.hash = '#' + id;
      });

      // Live progress update (best-effort)
      const update = throttle(() => {
        const cur = currentImgIndex();
        const opts = Array.from(sel.querySelectorAll('option[data-start][data-count][data-base]'));
        for (const opt of opts) {
          const start = parseInt(opt.dataset.start || '0', 10) || 0;
          const count = parseInt(opt.dataset.count || '0', 10) || 0;
          const base = opt.dataset.base || opt.textContent || '';
          if (count <= 1) { opt.textContent = base; continue; }

          let p = 0;
          if (cur < start) p = 0;
          else if (cur >= start + count) p = 100;
          else {
            const rel = cur - start;
            const pp = pct(rel, count);
            p = (pp === null) ? 0 : pp;
          }
          opt.textContent = `${base} (${p}%)`;
        }
      }, 350);

      const scroller = getScroller();
      if (scroller instanceof Element && scroller !== document.documentElement && scroller !== document.body) scroller.addEventListener('scroll', update, {passive:true});
      else window.addEventListener('scroll', update, {passive:true});

      window.addEventListener('resize', update, {passive:true});
      update();
    }

    function setupProgressAndResume(){
      const meta = getViewMeta();
      const bar = document.querySelector('.progressBar');
      const pagesContainer = document.querySelector('.pages');
      if(!meta || !pagesContainer) return;

      let restored = false;
      let pendingSaved = null;

      function setBar(idx, total){
        if(!bar) return;
        const t = Math.max(1, total);
        const pctv = t <= 1 ? 100 : (idx / (t - 1)) * 100;
        bar.style.width = pctv.toFixed(2) + '%';
      }

      function totalImages(){
        const imgs = pagesContainer.querySelectorAll('img[data-img-index]');
        return imgs.length || 1;
      }

      function currentThumb(idx){
        const img = pagesContainer.querySelector(`img[data-img-index="${idx}"]`);
        if (img) return img.getAttribute('src') || img.src || '';
        if (meta.kind === 'pdf') return makePdfThumb(meta.rel, meta.pdf);
        return '';
      }

      function currentChapterProgress(){
        if (meta.kind !== 'series') return null;
        const sel = document.querySelector('#tocSelect');
        if (!sel) return null;
        const out = {};
        const cur = currentImgIndex();
        const opts = Array.from(sel.querySelectorAll('option[data-start][data-count][data-base]'));
        for (const opt of opts) {
          const start = parseInt(opt.dataset.start || '0', 10) || 0;
          const count = parseInt(opt.dataset.count || '0', 10) || 0;
          const base = opt.dataset.base || opt.textContent || '';
          if (!base) continue;
          let p = 0;
          if (count > 1) {
            if (cur < start) p = 0;
            else if (cur >= start + count) p = 100;
            else {
              const rel = cur - start;
              const pp = pct(rel, count);
              p = (pp === null) ? 0 : pp;
            }
          }
          out[base] = p;
        }
        return out;
      }

      let lastSentAt = 0;
      function sendProgress(idx, force){
        if (!restored && !force) return;
        const now = Date.now();
        if (!force && now - lastSentAt < 800) return;
        lastSentAt = now;

        apiSendProgress({
          kind: meta.kind,
          rel: meta.rel,
          pdf: meta.pdf || '',
          title: meta.title || '',
          thumb: currentThumb(idx),
          page_index: idx,
          total_pages: totalImages(),
          reading: getReadingMode(),
          chapter_progress: currentChapterProgress(),
          updated_at: Date.now()/1000
        });
      }

      const onScroll = throttle(() => {
        const idx = currentImgIndex();
        setBar(idx, totalImages());
        sendProgress(idx, false);
      }, 120);

      function bindScrollHandlers(){
        const scroller = getScroller();
        if (scroller instanceof Element && scroller !== document.documentElement && scroller !== document.body) scroller.addEventListener('scroll', onScroll, {passive:true});
        else window.addEventListener('scroll', onScroll, {passive:true});
      }

      function applySavedChapterProgress(saved){
        if (!saved || meta.kind !== 'series') return;
        const sel = document.querySelector('#tocSelect');
        if (!sel) return;
        const cp = saved.chapter_progress;
        if (!cp || typeof cp !== 'object') return;
        const opts = Array.from(sel.querySelectorAll('option[data-base]'));
        for (const opt of opts) {
          const base = opt.dataset.base || '';
          if (!base) continue;
          if (!Object.prototype.hasOwnProperty.call(cp, base)) continue;
          const val = parseInt(cp[base], 10);
          const pctv = Number.isFinite(val) ? clamp(val, 0, 100) : 0;
          opt.textContent = `${base} (${pctv}%)`;
        }
      }

      function resumeToIndex(idx, attemptsLeft){
        scrollToImgIndex(idx);
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            const cur = currentImgIndex();
            if (Math.abs(cur - idx) <= 1 || attemptsLeft <= 0) {
              restored = true;
              setBar(cur, totalImages());
              sendProgress(cur, true);
              return;
            }
            setTimeout(() => resumeToIndex(idx, attemptsLeft - 1), 120);
          });
        });
      }

      bindScrollHandlers();
      window.addEventListener('resize', onScroll, {passive:true});
      document.querySelectorAll('img[data-img-index]').forEach(img => img.addEventListener('load', () => {
        if (pendingSaved !== null && !restored) resumeToIndex(pendingSaved, 6);
        else onScroll();
      }, {passive:true}));
      window.addEventListener('beforeunload', () => sendProgress(currentImgIndex(), true));

      (async () => {
        try{
          // Switching modes/controls (session) overrides saved.
          const switchIdx = consumeSwitchIndex(meta);
          if (switchIdx !== null) {
            pendingSaved = switchIdx;
            resumeToIndex(switchIdx, 8);
            return;
          }

          const saved = await apiGetProgress(meta);
          applySavedChapterProgress(saved);
          if(!saved) {
            restored = true;
            onScroll();
            return;
          }

          const savedMode = saved.reading || '';
          const curMode = getReadingMode();
          if (savedMode && savedMode !== curMode && (savedMode === 'v' || savedMode === 'h')) {
            const token = 'mv_resume_mode_' + meta.key;
            if (!sessionStorage.getItem(token)) {
              sessionStorage.setItem(token, '1');
              applyModeFitRule(meta, savedMode);
              setReadingMode(savedMode);
              location.reload();
              return;
            }
          }

          const idx = parseInt(saved.page_index || 0, 10) || 0;
          pendingSaved = idx;
          resumeToIndex(idx, 8);
        }catch(e){
          restored = true;
          onScroll();
        }
      })();
    }

    function setupReaderControls(){
      const meta = getViewMeta();
      const zoom = document.querySelector('#zoom');
      const fit = document.querySelector('#fit');
      const zoomOut = document.querySelector('#zoomOut');
      const zoomIn = document.querySelector('#zoomIn');
      const reset = document.querySelector('#zoomReset');
      const magBtn = document.querySelector('#magnifierToggle');
      const pinBtn = document.querySelector('#controlsPinToggle');
      const controls = document.querySelector('.reader-controls');
      if(!zoom || !fit) return;

      const mode = getReadingMode();

      let widthPct = parseInt(localStorage.getItem('mv_zoom') || '60', 10);
      let fitWidth = (localStorage.getItem('mv_fit') || '0') === '1';
      widthPct = clamp(widthPct, 40, 160);

      // Explicit rule:
      // - Read-all + horizontal => fit ON
      // - Read-all + vertical => fit OFF
      // - Single chapter/PDF => fit OFF in both modes
      fitWidth = applyModeFitRule(meta, mode);

      zoom.value = String(widthPct);
      fit.checked = fitWidth;
      setCSSVars(widthPct, fitWidth);

      function persist(){
        localStorage.setItem('mv_zoom', String(widthPct));
        localStorage.setItem('mv_fit', fitWidth ? '1' : '0');
      }

      const hiddenPref = localStorage.getItem('mv_controls_hidden_top') === '1';
      function syncControlsPinned(){
        if (!controls || !pinBtn) return;
        controls.classList.add('can-hide');
        controls.classList.toggle('hiddenTop', hiddenPrefState);
        pinBtn.classList.toggle('active', hiddenPrefState);
        pinBtn.textContent = hiddenPrefState ? 'Unpin' : 'Pin top';
        pinBtn.title = hiddenPrefState ? 'Show toolbar normally' : 'Pin toolbar into the top edge';
      }
      let hiddenPrefState = hiddenPref;
      syncControlsPinned();
      pinBtn?.addEventListener('click', () => {
        hiddenPrefState = !hiddenPrefState;
        localStorage.setItem('mv_controls_hidden_top', hiddenPrefState ? '1' : '0');
        syncControlsPinned();
      });

      zoom.addEventListener('input', () => {
        keepCenteredAfterLayoutChange(() => {
          widthPct = clamp(parseInt(zoom.value, 10), 40, 160);
          // slider implies manual sizing -> uncheck fit
          fitWidth = false; fit.checked = false;
          setCSSVars(widthPct, fitWidth);
          persist();
        });
      });

      fit.addEventListener('change', () => {
        keepCenteredAfterLayoutChange(() => {
          fitWidth = !!fit.checked;
          setCSSVars(widthPct, fitWidth);
          persist();
        });
      });

      function bump(delta){
        keepCenteredAfterLayoutChange(() => {
          widthPct = clamp(widthPct + delta, 40, 160);
          zoom.value = String(widthPct);
          fitWidth = false; fit.checked = false;
          setCSSVars(widthPct, fitWidth);
          persist();
        });
      }

      zoomOut?.addEventListener('click', () => bump(-10));
      zoomIn?.addEventListener('click', () => bump(+10));

      reset?.addEventListener('click', () => {
        keepCenteredAfterLayoutChange(() => {
          widthPct = 60;
          zoom.value='60';
          fitWidth=false; fit.checked=false;
          setCSSVars(widthPct, fitWidth);
          persist();
        });
      });

      // Magnifier
      const magnifier = document.querySelector('#magnifier');
      let magOn = false;
      const magZoom = 2.2;
      const magR = 121;

      function setMag(on){
        magOn = on;
        if (!magnifier) return;
        magnifier.classList.toggle('on', on);
        if (magBtn) magBtn.textContent = on ? '🔎' : '🔍';
      }
      magBtn?.addEventListener('click', () => setMag(!magOn));

      document.addEventListener('mousemove', (e) => {
        if (!magOn || !magnifier) return;
        const t = e.target;
        if (!(t instanceof HTMLImageElement)) { magnifier.style.display = 'none'; return; }
        const rect = t.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) { magnifier.style.display = 'none'; return; }
        const x = e.clientX, y = e.clientY;
        if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) { magnifier.style.display = 'none'; return; }

        magnifier.style.display = 'block';

        let mx = x + 18, my = y + 18;
        mx = Math.min(mx, window.innerWidth - magR*2 - 10);
        my = Math.min(my, window.innerHeight - magR*2 - 10);
        magnifier.style.left = mx + 'px';
        magnifier.style.top = my + 'px';

        const rx = (x - rect.left) / rect.width;
        const ry = (y - rect.top) / rect.height;
        const bgW = rect.width * magZoom;
        const bgH = rect.height * magZoom;

        magnifier.style.backgroundImage = `url("${t.currentSrc || t.src}")`;
        magnifier.style.backgroundSize = `${bgW}px ${bgH}px`;
        magnifier.style.backgroundPosition = `${(-rx*bgW + magR)}px ${(-ry*bgH + magR)}px`;
      });

      document.addEventListener('keydown', (e) => {
        if (e.target && ['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;

        const prev = document.querySelector('[data-prev]');
        const next = document.querySelector('[data-next]');

        if (e.key === '[' && prev && !prev.disabled) { e.preventDefault(); prev.click(); }
        if (e.key === ']' && next && !next.disabled) { e.preventDefault(); next.click(); }

        if (e.key === '+' || e.key === '=') { e.preventDefault(); bump(+10); }
        if (e.key === '-') { e.preventDefault(); bump(-10); }
        if (e.key === '0') { e.preventDefault(); reset?.click(); }
        if (e.key === 'f') { e.preventDefault(); fit.click(); }
        if (e.key === 'm') { e.preventDefault(); setMag(!magOn); }

        if (e.key === 'ArrowLeft') { e.preventDefault(); scrollStep(-1, true); }
        if (e.key === 'ArrowRight') { e.preventDefault(); scrollStep(1, true); }

        if (e.key === ' ') {
          e.preventDefault();
          advancePage(true);
        }
      });

      document.addEventListener('click', (e) => {
        if (getPageKind() !== 'reader') return;
        if (e.button !== 0) return;
        if (e.defaultPrevented) return;
        const t = e.target;
        if (!(t instanceof Element)) return;
        if (t.closest('a, button, input, label, select, option, .reader-controls, .fab, .fabHome, .fabToc')) return;
        if (window.getSelection && String(window.getSelection()).trim()) return;
        advancePage(true);
      });
    }

    document.addEventListener('DOMContentLoaded', () => {
      setupSearchBar();
      setupModeButtons();
      applySpreadGrouping();        // must run before resume scroll
      setupHorizontalWheel();
      setupToStart();
      setupReaderControls();        // apply width/fit rules before restoring position
      setupProgressAndResume();
      setupTocDropdown();           // series page only
    });
    """

    init_script = f"""
    <script>
    (function(){{
      try{{
        document.documentElement.dataset.page = "{page_kind}";
        var m = localStorage.getItem('mv_reading') || 'v';
        if (!['v','h'].includes(m)) m = 'v';
        document.documentElement.dataset.reading = m;
        localStorage.setItem('mv_reading', m);
      }}catch(e){{}}
    }})();
    </script>
    """

    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{html.escape(title)}</title>
  {init_script}
  <style>{css}</style>
</head>
<body>
  <a class="fabHome" href="/browse/" title="Home">⌂</a>
  {body_html}
  <a id="pageAdvance" class="fab fabPage" title="Page down / advance">⇵</a>
  <a id="toStart" class="fab" title="Return to start">↑</a>
  <div id="magnifier" class="magnifier" aria-hidden="true"></div>
  <script>{js}</script>
</body>
</html>
"""
    return doc.encode("utf-8")


def render_breadcrumbs(root_name: str, rel_dir: str) -> str:
    rel_dir = rel_dir.strip("/")
    parts = rel_dir.split("/") if rel_dir else []
    crumbs = [f'<a href="/browse/">{html.escape(root_name)}</a>']
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}" if cur else p
        crumbs.append(f'<span class="sep">›</span><a href="/browse/{url_quote_path(cur)}">{html.escape(p)}</a>')
    return f'<div class="crumbs">{"".join(crumbs)}</div>'


def render_header(lib_root: str, mode: str) -> str:
    # "Open folder…" removed from UI as requested.
    return f"""
    <div class="header">
      <div class="header-inner">
        <div class="brand">
          <div class="t">Mangus Viewer</div>
          <div class="s">{html.escape(lib_root)}</div>
        </div>
        <div class="spacer"></div>
        <button class="btn ghost" id="browseToggle" type="button" title="Search your library">Browse</button>
        <span class="pill">{html.escape(mode)}</span>
      </div>
    </div>
    <div id="searchPanel" class="searchPanel">
      <div class="searchRow">
        <input id="searchInput" type="text" placeholder="Search manga/manhua name… (press Enter)" />
        <button id="searchGo" class="btn secondary" type="button">Search</button>
      </div>
    </div>
    """


def _save_cards_html(saves: list[dict[str, Any]]) -> str:
    if not saves:
        return '<div class="hint">No saves yet. Open a chapter to start tracking your progress.</div>'

    cards = []
    for s in saves:
        kind = s.get("kind", "")
        rel = s.get("rel", "")
        pdf = s.get("pdf", "")
        title = s.get("title") or (rel.split("/")[-1] if rel else kind)
        reading = s.get("reading", "v")
        page_index = int(s.get("page_index") or 0)
        total = int(s.get("total_pages") or 0)
        updated = float(s.get("updated_at") or 0.0)
        thumb = s.get("thumb") or ""

        if kind == "pdf":
            href = f"/pdfview/{url_quote_path(rel)}/{urllib.parse.quote(pdf)}"
        elif kind == "series":
            href = f"/series/{url_quote_path(rel)}"
        else:
            href = f"/chapter/{url_quote_path(rel)}"

        mode_name = {"v": "Vertical", "h": "Horizontal"}.get(reading, "Vertical")
        info1 = f"Mode: {mode_name} · Page {page_index+1}" + (f"/{total}" if total > 0 else "")
        info2 = f"Path: {rel or '/'}"
        info3 = f"Updated: {fmt_ts(updated)}" if updated else ""

        thumb_html = '<div class="ph">▶</div>'
        if isinstance(thumb, str) and thumb.startswith("/"):
            thumb_html = f'<img src="{html.escape(thumb)}" alt="last page" loading="lazy">'

        cards.append(
            f"""
            <div class="card">
              <div class="thumb">{thumb_html}</div>
              <div class="info">
                <p class="h">{html.escape(title)}</p>
                <p class="m">{html.escape(info1)}</p>
                <p class="m">{html.escape(info2)}</p>
                <p class="m">{html.escape(info3)}</p>
              </div>
              <div class="actions">
                <a class="btn secondary" href="{href}">Continue</a>
                <a class="btn ghost" href="/delete-save/{urllib.parse.quote(s.get('id',''))}">Remove</a>
              </div>
            </div>
            """
        )

    return f"""
    <details class="card" open>
      <summary style="padding:12px 12px; cursor:pointer; font-weight:900;">Saves (Continue reading)</summary>
      <div style="padding:0 12px 12px 12px;">
        <div class="grid" style="margin-top:12px;">
          {''.join(cards)}
        </div>
      </div>
    </details>
    """


def render_search_page(lib: Library, query: str, results: list[dict[str, Any]]) -> bytes:
    root_name = lib.root.name or str(lib.root)
    breadcrumbs = render_breadcrumbs(root_name, "")

    cards: list[str] = []
    for r in results:
        rel = r["rel"]
        name = r["name"]
        kind = r["kind"]
        chapters = int(r.get("chapters") or 0)

        cover = None
        if chapters > 0:
            kids = lib.chapter_children(rel)
            if kids:
                first = lib.get_first_image(kids[0])
                if first:
                    cover = build_img_url(kids[0], first.name)
        if not cover and lib.chapter_kind(rel) == "chapter":
            first = lib.get_first_image(rel)
            if first:
                cover = build_img_url(rel, first.name)

        thumb_html = '<div class="ph">🔎</div>'
        if cover:
            thumb_html = f'<img src="{cover}" alt="cover" loading="lazy">'

        actions = []
        kids = lib.chapter_children(rel)
        if kids:
            actions.append(f'<a class="btn secondary" href="/series/{url_quote_path(rel)}">Read all</a>')
        if lib.chapter_kind(rel) == "chapter":
            actions.append(f'<a class="btn secondary" href="/chapter/{url_quote_path(rel)}">Read</a>')
        if lib.chapter_kind(rel) == "pdf_folder":
            pdfs = lib.list_pdfs(rel)
            actions.append(
                f'<a class="btn secondary" href="/pdfview/{url_quote_path(rel)}/{urllib.parse.quote(pdfs[0].name)}">Read PDFs</a>'
            )
        actions.append(f'<a class="btn ghost" href="/browse/{url_quote_path(rel)}">Open</a>')

        subtitle = f"{chapters} chapters" if chapters else kind

        cards.append(
            f"""
            <div class="card">
              <a href="/browse/{url_quote_path(rel)}"><div class="thumb">{thumb_html}</div></a>
              <div class="info">
                <p class="h">{html.escape(name)}</p>
                <p class="m">{html.escape(subtitle)}</p>
                <p class="m">{html.escape(rel or '/')}</p>
              </div>
              <div class="actions">{''.join(actions)}</div>
            </div>
            """
        )

    grid = f'<div class="grid">{"".join(cards) if cards else "<div class=hint>No results.</div>"}</div>'

    body = f"""
    {render_header(str(lib.root), "Search")}
    <div class="wrap">
      {breadcrumbs}
      <div class="hint">Search: <span class="kbd">{html.escape(query)}</span></div>
      {grid}
    </div>
    """
    return render_base_html("Mangus Viewer — Search", body, page_kind="browse")


def render_browse_page(lib: Library, rel_dir: str, saves: list[dict[str, Any]], msg: str = "") -> bytes:
    rel_dir = rel_dir.strip("/")
    folder = safe_resolve_under_root(lib.root, rel_dir)
    root_name = lib.root.name or str(lib.root)

    breadcrumbs = render_breadcrumbs(root_name, rel_dir)
    subdirs = lib.list_subdirs(rel_dir)

    cards: list[str] = []

    if rel_dir:
        parent_rel = to_posix_rel(lib.root, folder.parent)
        cards.append(
            f"""
        <div class="card">
          <div class="thumb"><div class="ph">⬆️</div></div>
          <div class="info">
            <p class="h">Parent Folder</p>
            <p class="m">{html.escape(parent_rel or "/")}</p>
          </div>
          <div class="actions">
            <a class="btn ghost" href="/browse/{url_quote_path(parent_rel)}">Go up</a>
          </div>
        </div>
        """
        )

    chapter_children = lib.chapter_children(rel_dir)
    if chapter_children:
        cards.append(
            f"""
        <div class="card">
          <a href="/series/{url_quote_path(rel_dir)}"><div class="thumb"><div class="ph">∞</div></div></a>
          <div class="info">
            <p class="h">Read all (continuous)</p>
            <p class="m">{len(chapter_children)} chapters · one long scroll</p>
          </div>
          <div class="actions">
            <a class="btn secondary" href="/series/{url_quote_path(rel_dir)}">Read all</a>
            <a class="btn ghost" href="/browse/{url_quote_path(rel_dir)}">Stay</a>
          </div>
        </div>
        """
        )

    kind = lib.chapter_kind(rel_dir)
    if kind == "chapter":
        first_img = lib.get_first_image(rel_dir)
        thumb_html = '<div class="ph">📖</div>'
        if first_img:
            thumb_html = f'<img src="{build_img_url(rel_dir, first_img.name)}" alt="cover" loading="lazy">'
        img_count = len(lib.list_images(rel_dir))
        cards.append(
            f"""
        <div class="card">
          <a href="/chapter/{url_quote_path(rel_dir)}"><div class="thumb">{thumb_html}</div></a>
          <div class="info">
            <p class="h">Read this folder</p>
            <p class="m">📖 Images · {img_count} pages</p>
          </div>
          <div class="actions">
            <a class="btn secondary" href="/chapter/{url_quote_path(rel_dir)}">Read</a>
            <a class="btn ghost" href="/browse/{url_quote_path(rel_dir)}">Stay</a>
          </div>
        </div>
        """
        )

    if kind == "pdf_folder":
        pdfs = lib.list_pdfs(rel_dir)
        first_pdf = pdfs[0]
        thumb_html = '<div class="ph">📄</div>'
        if HAS_PDF_THUMB:
            thumb_html = f'<img src="{build_pdf_thumb_url(rel_dir, first_pdf.name)}" alt="cover" loading="lazy">'
        cards.append(
            f"""
        <div class="card">
          <a href="/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(first_pdf.name)}"><div class="thumb">{thumb_html}</div></a>
          <div class="info">
            <p class="h">Read this folder</p>
            <p class="m">📄 PDF · {len(pdfs)} file(s)</p>
          </div>
          <div class="actions">
            <a class="btn secondary" href="/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(first_pdf.name)}">Read PDFs</a>
            <a class="btn ghost" href="/browse/{url_quote_path(rel_dir)}">Stay</a>
          </div>
        </div>
        """
        )

    for d in subdirs:
        r = to_posix_rel(lib.root, d)
        d_kind = lib.chapter_kind(r)
        title = d.name

        if d_kind == "chapter":
            first_img = lib.get_first_image(r)
            thumb_html = '<div class="ph">📖</div>'
            if first_img:
                thumb_html = f'<img src="{build_img_url(r, first_img.name)}" alt="cover" loading="lazy">'
            count = len(lib.list_images(r))
            cards.append(
                f"""
            <div class="card">
              <a href="/chapter/{url_quote_path(r)}"><div class="thumb">{thumb_html}</div></a>
              <div class="info">
                <p class="h">{html.escape(title)}</p>
                <p class="m">📖 Images · {count} pages</p>
              </div>
              <div class="actions">
                <a class="btn secondary" href="/chapter/{url_quote_path(r)}">Read</a>
                <a class="btn ghost" href="/browse/{url_quote_path(r)}">Open</a>
              </div>
            </div>
            """
            )
        elif d_kind == "pdf_folder":
            pdfs = lib.list_pdfs(r)
            first_pdf = pdfs[0]
            thumb_html = '<div class="ph">📄</div>'
            if HAS_PDF_THUMB:
                thumb_html = f'<img src="{build_pdf_thumb_url(r, first_pdf.name)}" alt="cover" loading="lazy">'
            cards.append(
                f"""
            <div class="card">
              <a href="/pdfview/{url_quote_path(r)}/{urllib.parse.quote(first_pdf.name)}"><div class="thumb">{thumb_html}</div></a>
              <div class="info">
                <p class="h">{html.escape(title)}</p>
                <p class="m">📄 PDF · {len(pdfs)} file(s)</p>
              </div>
              <div class="actions">
                <a class="btn secondary" href="/pdfview/{url_quote_path(r)}/{urllib.parse.quote(first_pdf.name)}">Read PDFs</a>
                <a class="btn ghost" href="/browse/{url_quote_path(r)}">Open</a>
              </div>
            </div>
            """
            )
        else:
            kids = lib.chapter_children(r)
            extra = f'<a class="btn secondary" href="/series/{url_quote_path(r)}">Read all</a>' if kids else ""
            cards.append(
                f"""
            <div class="card">
              <a href="/browse/{url_quote_path(r)}"><div class="thumb"><div class="ph">📁</div></div></a>
              <div class="info">
                <p class="h">{html.escape(title)}</p>
                <p class="m">Folder{f" · {len(kids)} chapters" if kids else ""}</p>
              </div>
              <div class="actions">
                {extra}
                <a class="btn ghost" href="/browse/{url_quote_path(r)}">Open</a>
              </div>
            </div>
            """
            )

    grid = f'<div class="grid">{"".join(cards) if cards else "<div class=hint>This folder has no subfolders.</div>"}</div>'

    hint_lines = [
        "Shortcuts: [ / ] prev/next · + / - zoom · 0 reset · f fit · m magnifier · Space scroll/advance.",
        "Horizontal mode: switch Vertical/Horizontal in the reader toolbar (keeps your page).",
        "Read-all: zoom/fit changes keep the current page centered.",
    ]
    if msg:
        hint_lines.insert(0, f"Message: {html.escape(msg)}")
    hint = f'<div class="hint">{"<br/>".join(hint_lines)}</div>'

    saves_html = _save_cards_html(saves) if rel_dir == "" else ""

    body = f"""
    {render_header(str(lib.root), "Browse")}
    <div class="wrap">
      {saves_html}
      {breadcrumbs}
      {hint}
      {grid}
    </div>
    """
    return render_base_html("Mangus Viewer — Browse", body, page_kind="browse")


def reader_shortcuts_hint() -> str:
    return """
    <div class="hint">
      <span class="kbd">[</span>/<span class="kbd">]</span> prev/next ·
      <span class="kbd">+</span>/<span class="kbd">-</span> zoom ·
      <span class="kbd">0</span> reset zoom ·
      <span class="kbd">f</span> fit width ·
      <span class="kbd">m</span> magnifier ·
      <span class="kbd">←</span>/<span class="kbd">→</span> scroll ·
      <span class="kbd">Space</span> scroll/advance ·
      <span class="kbd">Ctrl+C</span> stop server
    </div>
    """


def render_reader_controls(title: str, subtitle: str, back_href: str, prev_href: Optional[str], next_href: Optional[str]) -> str:
    prev_btn = f'<a class="btn" href="{prev_href}" data-prev>◀ Prev</a>' if prev_href else '<button class="btn ghost" disabled>◀ Prev</button>'
    next_btn = f'<a class="btn" href="{next_href}" data-next>Next ▶</a>' if next_href else '<button class="btn ghost" disabled>Next ▶</button>'

    return f"""
      <div class="reader-controls">
        {prev_btn}
        {next_btn}
        <a class="btn secondary" href="{back_href}" data-back>Back</a>
        <div class="spacer"></div>

        <button class="btn small ghost" data-mode="v" type="button" title="Vertical scroll">Vertical</button>
        <button class="btn small ghost" data-mode="h" type="button" title="Horizontal (RTL)">Horizontal</button>

        <button class="btn small ghost" id="zoomOut" title="Zoom out">−</button>
        <input id="zoom" type="range" min="40" max="160" value="60" />
        <button class="btn small ghost" id="zoomIn" title="Zoom in">+</button>

        <label class="btn small ghost" title="Fit width">
          <input id="fit" type="checkbox" style="transform:scale(1.1); margin-right:8px" />
          Fit
        </label>

        <button class="btn small ghost" id="zoomReset" title="Reset zoom">Reset</button>
        <button class="btn small ghost" id="magnifierToggle" title="Magnifier (m)">🔍</button>
        <button class="btn small ghost" id="controlsPinToggle" type="button" title="Pin toolbar into the top edge">Pin top</button>

        <span class="pill">{html.escape(title)} · {html.escape(subtitle)}</span>
      </div>
    """


def render_chapter_images_page(lib: Library, rel_dir: str) -> bytes:
    rel_dir = rel_dir.strip("/")
    root_name = lib.root.name or str(lib.root)
    images = lib.list_images(rel_dir)

    prev_rel, next_rel = lib.siblings_with_content(rel_dir)
    breadcrumbs = render_breadcrumbs(root_name, rel_dir)

    title = rel_dir.split("/")[-1] if rel_dir else (lib.root.name or "Root")
    if not images:
        body = f"""
        {render_header(str(lib.root), "Reader")}
        <div class="wrap">
          {breadcrumbs}
          <div class="hint">No images found in this folder.</div>
        </div>
        """
        return render_base_html("Mangus Viewer — Empty", body, page_kind="reader")

    img_tags: list[str] = []
    for idx, p in enumerate(images):
        src = build_img_url(rel_dir, p.name)
        alt = html.escape(p.name)
        img_tags.append(
            f'<div class="page"><img data-img-index="{idx}" loading="lazy" decoding="async" src="{src}" alt="{alt}"></div>'
        )

    controls = render_reader_controls(
        title=title,
        subtitle=f"{len(images)} pages",
        back_href=f"/browse/{url_quote_path(rel_dir)}",
        prev_href=f"/chapter/{url_quote_path(prev_rel)}" if prev_rel else None,
        next_href=f"/chapter/{url_quote_path(next_rel)}" if next_rel else None,
    )

    key = view_id("chapter", rel_dir, "")
    view_meta = (
        f'<div id="viewMeta" data-kind="chapter" data-rel="{html.escape(rel_dir)}" '
        f'data-title="{html.escape(title)}" data-key="{html.escape(key)}" style="display:none"></div>'
    )

    body = f"""
    <div class="progressWrap"><div class="progressBar"></div></div>
    {render_header(str(lib.root), "Reader")}
    <div class="reader">
      {view_meta}
      {controls}
      {breadcrumbs}
      {reader_shortcuts_hint()}
      <div class="pages">
        {''.join(img_tags)}
      </div>
    </div>
    """
    return render_base_html(f"Mangus Viewer — {title}", body, page_kind="reader")


def _pdf_nav(lib: Library, rel_dir: str, pdf_name: str) -> tuple[str, Optional[str], Optional[str], list[str]]:
    pdfs = lib.list_pdfs(rel_dir)
    names = [p.name for p in pdfs]
    if not names:
        return pdf_name, None, None, []
    if pdf_name not in names:
        pdf_name = names[0]
    idx = names.index(pdf_name)
    prev_name = names[idx - 1] if idx > 0 else None
    next_name = names[idx + 1] if idx + 1 < len(names) else None
    return pdf_name, prev_name, next_name, names


def render_pdf_view_page(lib: Library, rel_dir: str, pdf_name: str) -> bytes:
    rel_dir = rel_dir.strip("/")
    root_name = lib.root.name or str(lib.root)

    prev_ch, next_ch = lib.siblings_with_content(rel_dir)
    breadcrumbs = render_breadcrumbs(root_name, rel_dir)

    pdf_name, prev_file, next_file, all_files = _pdf_nav(lib, rel_dir, pdf_name)
    title = rel_dir.split("/")[-1] if rel_dir else (lib.root.name or "Root")
    if not all_files:
        body = f"""
        {render_header(str(lib.root), "PDF")}
        <div class="wrap">
          {breadcrumbs}
          <div class="hint">No PDFs found in this folder.</div>
        </div>
        """
        return render_base_html("Mangus Viewer — Empty", body, page_kind="reader")

    pdf_url = build_pdf_url(rel_dir, pdf_name)

    controls = render_reader_controls(
        title=title,
        subtitle=f"PDF {all_files.index(pdf_name)+1}/{len(all_files)}",
        back_href=f"/browse/{url_quote_path(rel_dir)}",
        prev_href=f"/chapter/{url_quote_path(prev_ch)}" if prev_ch else None,
        next_href=f"/chapter/{url_quote_path(next_ch)}" if next_ch else None,
    )

    prev_file_btn = (
        f'<a class="btn ghost" href="/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(prev_file)}">◀ Prev PDF</a>'
        if prev_file
        else '<button class="btn ghost" disabled>◀ Prev PDF</button>'
    )
    next_file_btn = (
        f'<a class="btn ghost" href="/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(next_file)}">Next PDF ▶</a>'
        if next_file
        else '<button class="btn ghost" disabled>Next PDF ▶</button>'
    )
    options = "\n".join(
        f'<option value="/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(name)}" {"selected" if name == pdf_name else ""}>{html.escape(name)}</option>'
        for name in all_files
    )

    key = view_id("pdf", rel_dir, pdf_name)
    view_meta = (
        f'<div id="viewMeta" data-kind="pdf" data-rel="{html.escape(rel_dir)}" data-pdf="{html.escape(pdf_name)}" '
        f'data-title="{html.escape(title)}" data-key="{html.escape(key)}" style="display:none"></div>'
    )

    body = f"""
    {render_header(str(lib.root), "PDF")}
    <div class="reader">
      {view_meta}
      {controls}

      <div class="reader-controls" style="margin-top:10px;">
        <div class="spacer"></div>
        {prev_file_btn}
        {next_file_btn}
        <select onchange="location=this.value" title="Select PDF file">{options}</select>
      </div>

      {breadcrumbs}
      {reader_shortcuts_hint()}

      <div class="pdfwrap page">
        <iframe src="{pdf_url}" title="{html.escape(pdf_name)}"></iframe>
      </div>

      <div class="hint">
        Note: resume for PDFs tracks the selected PDF file + mode; scrolling inside the PDF viewer depends on your browser.
      </div>
    </div>
    """
    return render_base_html(f"Mangus Viewer — {title} (PDF)", body, page_kind="reader")


def render_series_page(lib: Library, rel_dir: str, state: StateStore) -> bytes:
    rel_dir = rel_dir.strip("/")
    root_name = lib.root.name or str(lib.root)
    breadcrumbs = render_breadcrumbs(root_name, rel_dir)

    chapters = lib.chapter_children(rel_dir)
    title = rel_dir.split("/")[-1] if rel_dir else (lib.root.name or "Root")

    if not chapters:
        body = f"""
        {render_header(str(lib.root), "Continuous")}
        <div class="wrap">
          {breadcrumbs}
          <div class="hint">No chapter folders found inside this folder.</div>
        </div>
        """
        return render_base_html("Mangus Viewer — Continuous", body, page_kind="reader")

    pages_html: list[str] = []
    img_idx = 0

    toc_options = ['<option value="">Chapters…</option>']

    for i, ch_rel in enumerate(chapters, start=1):
        ch_name = ch_rel.split("/")[-1]
        kind = lib.chapter_kind(ch_rel)
        anchor = f"ch{i}"

        if kind == "chapter":
            imgs = lib.list_images(ch_rel)
            start = img_idx
            count = len(imgs)

            toc_options.append(
                f'<option value="{anchor}" data-base="{html.escape(ch_name)}" data-start="{start}" data-count="{count}">{html.escape(ch_name)}</option>'
            )

            pages_html.append(f'<div id="{anchor}" class="crumbs">{html.escape(ch_name)} · {len(imgs)} pages</div>')
            for p in imgs:
                pages_html.append(
                    f'<div class="page"><img data-img-index="{img_idx}" loading="lazy" decoding="async" src="{build_img_url(ch_rel, p.name)}" alt="{html.escape(p.name)}"></div>'
                )
                img_idx += 1

        elif kind == "pdf_folder":
            pdfs = lib.list_pdfs(ch_rel)
            toc_options.append(f'<option value="{anchor}">{html.escape(ch_name)} (PDF)</option>')
            pages_html.append(f'<div id="{anchor}" class="crumbs">{html.escape(ch_name)} · {len(pdfs)} PDF file(s)</div>')
            for pdf in pdfs:
                pdf_url = build_pdf_url(ch_rel, pdf.name)
                pages_html.append(f'<div class="hint">PDF: <span class="kbd">{html.escape(pdf.name)}</span></div>')
                pages_html.append(
                    f'<div class="page"><div class="pdfwrap" style="height:80vh;"><iframe src="{pdf_url}" title="{html.escape(pdf.name)}"></iframe></div></div>'
                )

    controls = render_reader_controls(
        title=title,
        subtitle=f"{len(chapters)} chapters",
        back_href=f"/browse/{url_quote_path(rel_dir)}",
        prev_href=None,
        next_href=None,
    )

    key = view_id("series", rel_dir, "")
    view_meta = (
        f'<div id="viewMeta" data-kind="series" data-rel="{html.escape(rel_dir)}" '
        f'data-title="{html.escape(title)}" data-key="{html.escape(key)}" style="display:none"></div>'
    )

    toc_floating = f"""
    <div id="tocFloating" class="fabToc">
      <select id="tocSelect" title="Jump to chapter">
        {''.join(toc_options)}
      </select>
    </div>
    """

    body = f"""
    <div class="progressWrap"><div class="progressBar"></div></div>
    {render_header(str(lib.root), "Continuous")}
    {toc_floating}
    <div class="reader">
      {view_meta}
      {controls}
      {breadcrumbs}
      {reader_shortcuts_hint()}
      <div class="pages">
        {''.join(pages_html)}
      </div>
    </div>
    """
    return render_base_html(f"Mangus Viewer — {title} (Continuous)", body, page_kind="reader")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MangusViewer/2.1"

    @property
    def lib(self) -> Library:
        return self.server.lib  # type: ignore[attr-defined]

    @property
    def logger(self) -> Logger:
        return self.server.logger  # type: ignore[attr-defined]

    @property
    def picker(self) -> FolderPickBroker:
        return self.server.picker  # type: ignore[attr-defined]

    @property
    def state(self) -> StateStore:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        self.logger.info(f"[http] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/" or path == "":
                self._redirect("/browse/")
                return

            # kept for compatibility (no UI button)
            if path == "/pick-root":
                self._handle_pick_root()
                return

            if path.startswith("/browse"):
                rel_dir = urllib.parse.unquote(path[len("/browse") :].lstrip("/"))
                msg = qs.get("msg", [""])[0]
                saves = self.state.list_saves(limit=12) if rel_dir.strip("/") == "" else []
                self._send_html(render_browse_page(self.lib, rel_dir, saves=saves, msg=msg))
                return

            if path.startswith("/search"):
                q = (qs.get("q") or [""])[0]
                res = search_library(self.lib, q, limit=40)
                self._send_html(render_search_page(self.lib, q, res))
                return

            if path.startswith("/series"):
                rel_dir = urllib.parse.unquote(path[len("/series") :].lstrip("/"))
                self._send_html(render_series_page(self.lib, rel_dir, self.state))
                return

            if path.startswith("/chapter"):
                rel_dir = urllib.parse.unquote(path[len("/chapter") :].lstrip("/"))
                kind = self.lib.chapter_kind(rel_dir)
                if kind == "pdf_folder" and not self.lib.folder_has_images(rel_dir):
                    pdfs = self.lib.list_pdfs(rel_dir)
                    self._redirect(f"/pdfview/{url_quote_path(rel_dir)}/{urllib.parse.quote(pdfs[0].name)}")
                    return
                self._send_html(render_chapter_images_page(self.lib, rel_dir))
                return

            if path.startswith("/pdfview/"):
                rest = urllib.parse.unquote(path[len("/pdfview/") :].lstrip("/"))
                if "/" in rest:
                    rel_dir, pdf_name = rest.rsplit("/", 1)
                else:
                    rel_dir, pdf_name = "", rest
                self._send_html(render_pdf_view_page(self.lib, rel_dir, pdf_name))
                return

            if path.startswith("/delete-save/"):
                save_id = urllib.parse.unquote(path[len("/delete-save/") :].lstrip("/"))
                self.state.delete(save_id)
                self._redirect("/browse/?msg=" + urllib.parse.quote("Save removed."))
                return

            if path.startswith("/api/progress"):
                self._handle_api_progress_get(qs)
                return

            if path.startswith("/pdfthumb/"):
                self._serve_pdf_thumb(path[len("/pdfthumb/") :])
                return

            if path.startswith("/img/"):
                self._serve_file(path[len("/img/") :], kind="img")
                return

            if path.startswith("/pdf/"):
                self._serve_file(path[len("/pdf/") :], kind="pdf")
                return

            self._send_text("Not found", HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except PermissionError as e:
            self._send_text(f"Forbidden: {e}", HTTPStatus.FORBIDDEN)
        except Exception as e:
            try:
                self._send_text(f"Server error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/progress"):
                self._handle_api_progress_post()
                return
            self._send_text("Not found", HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                self._send_text(f"Server error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _handle_api_progress_get(self, qs: dict[str, list[str]]) -> None:
        kind = (qs.get("kind") or [""])[0]
        rel = (qs.get("rel") or [""])[0]
        pdf = (qs.get("pdf") or [""])[0]
        item = self.state.get(kind, rel, pdf) or {}
        data = json.dumps(item).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_api_progress_post(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        kind = str(payload.get("kind") or "")
        rel = str(payload.get("rel") or "")
        pdf = str(payload.get("pdf") or "")
        title = str(payload.get("title") or (rel.split("/")[-1] if rel else kind))
        reading = str(payload.get("reading") or "v")
        page_index = int(payload.get("page_index") or 0)
        total_pages = int(payload.get("total_pages") or 0)
        updated_at = float(payload.get("updated_at") or now_ts())
        thumb = str(payload.get("thumb") or "")

        if not kind:
            self._send_text("bad request", HTTPStatus.BAD_REQUEST)
            return

        if reading not in {"v", "h"}:
            reading = "v"

        chapter_progress = payload.get("chapter_progress")
        if not isinstance(chapter_progress, dict):
            chapter_progress = None
        else:
            cleaned_progress: dict[str, int] = {}
            for k, v in chapter_progress.items():
                try:
                    key = str(k)
                    val = max(0, min(100, int(v)))
                except Exception:
                    continue
                if key:
                    cleaned_progress[key] = val
            chapter_progress = cleaned_progress

        item = {
            "id": view_id(kind, rel, pdf),
            "kind": kind,
            "rel": rel,
            "pdf": pdf,
            "title": title,
            "thumb": thumb,
            "reading": reading,
            "page_index": page_index,
            "total_pages": total_pages,
            "updated_at": updated_at,
        }
        if chapter_progress is not None:
            item["chapter_progress"] = chapter_progress
        self.state.upsert(item)
        self._send_text("ok", HTTPStatus.OK)

    def _handle_pick_root(self) -> None:
        self.logger.info("[pick-root] requested")
        picked = self.picker.request_pick("Select your Manhua folder (library or chapter)")
        if not picked:
            self.logger.info("[pick-root] canceled or failed")
            self._redirect("/browse/?msg=" + urllib.parse.quote("Folder picker canceled or unavailable."))
            return

        self.server.set_root(picked)  # type: ignore[attr-defined]
        self.logger.info(f"[pick-root] new root: {picked}")

        if self.lib.chapter_kind("") is not None:
            self._redirect("/chapter/")
        else:
            self._redirect("/browse/?msg=" + urllib.parse.quote("Root folder switched."))

    def _redirect(self, to: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", to)
        self.end_headers()

    def _send_html(self, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_text(self, text: str, status: HTTPStatus) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, rel_rest: str, kind: str) -> None:
        rel_rest = urllib.parse.unquote(rel_rest.lstrip("/"))
        if "/" in rel_rest:
            rel_dir, filename = rel_rest.rsplit("/", 1)
        else:
            rel_dir, filename = "", rel_rest

        folder = safe_resolve_under_root(self.lib.root, rel_dir)
        file_path = (folder / filename).resolve()

        try:
            file_path.relative_to(self.lib.root)
        except ValueError:
            raise PermissionError("File outside library root")

        if not file_path.exists():
            self._send_text("Not found", HTTPStatus.NOT_FOUND)
            return
        if kind == "img" and not is_image_file(file_path):
            self._send_text("Not found", HTTPStatus.NOT_FOUND)
            return
        if kind == "pdf" and not is_pdf_file(file_path):
            self._send_text("Not found", HTTPStatus.NOT_FOUND)
            return

        ctype = guess_mime(file_path)

        try:
            st = file_path.stat()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()

            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_pdf_thumb(self, rel_rest: str) -> None:
        if not HAS_PDF_THUMB:
            self._send_text("PDF thumbnails not available (install PyMuPDF)", HTTPStatus.NOT_FOUND)
            return

        rel_rest = urllib.parse.unquote(rel_rest.lstrip("/"))
        if "/" in rel_rest:
            rel_dir, filename = rel_rest.rsplit("/", 1)
        else:
            rel_dir, filename = "", rel_rest

        folder = safe_resolve_under_root(self.lib.root, rel_dir)
        file_path = (folder / filename).resolve()

        try:
            file_path.relative_to(self.lib.root)
        except ValueError:
            raise PermissionError("File outside library root")

        if not file_path.exists() or not is_pdf_file(file_path):
            self._send_text("Not found", HTTPStatus.NOT_FOUND)
            return

        data = self.server.get_pdf_thumb(file_path)  # type: ignore[attr-defined]
        if not data:
            self._send_text("Thumbnail generation failed", HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local manga/manhua reader (neon violet UI).")
    p.add_argument("path", nargs="?", help="Root folder to browse (optional).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--debug", action="store_true", help="Enable console logs.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logger = Logger(enabled=bool(args.debug))

    if args.path:
        root = Path(args.path).expanduser()
        if not (root.exists() and root.is_dir()):
            logger.error(f"Invalid folder: {root}")
            return 1
    else:
        root = Path.cwd()

    picker = FolderPickBroker(logger)
    state_path = Path.home() / ".mangus_viewer_state.json"
    state = StateStore(state_path, logger)

    class _Server(ThreadingHTTPServer):
        def __init__(self, server_address, handler_cls):
            super().__init__(server_address, handler_cls)
            self._lock = threading.RLock()
            self.lib = Library(root)
            self.logger = logger
            self.picker = picker
            self.state = state
            self._pdf_thumb_cache: dict[str, tuple[float, bytes]] = {}

        def set_root(self, new_root: Path) -> None:
            with self._lock:
                self.lib = Library(new_root)
                self._pdf_thumb_cache.clear()

        def get_pdf_thumb(self, file_path: Path) -> Optional[bytes]:
            if not HAS_PDF_THUMB or fitz is None:
                return None
            try:
                st = file_path.stat()
                key = str(file_path)
                cached = self._pdf_thumb_cache.get(key)
                if cached and cached[0] == st.st_mtime:
                    return cached[1]

                doc = fitz.open(str(file_path))
                try:
                    page = doc.load_page(0)
                    mat = fitz.Matrix(1.25, 1.25)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    data = pix.tobytes("png")
                finally:
                    doc.close()

                self._pdf_thumb_cache[key] = (st.st_mtime, data)
                return data
            except Exception:
                return None

    httpd = _Server((args.host, args.port), AppHandler)
    host, port = httpd.server_address
    url = f"http://{host}:{port}/browse/"

    t = threading.Thread(target=httpd.serve_forever, name="httpd", daemon=True)
    t.start()

    print("=" * 72)
    print("Mangus Viewer running")
    print(f"URL  : {url}")
    print(f"Root : {httpd.lib.root}")
    print(f"State: {state_path}")
    print("PDF thumbs: enabled (PyMuPDF)" if HAS_PDF_THUMB else "PDF thumbs: disabled (install PyMuPDF)")
    print("Tip: Home button is top-left. Browse opens search. Add --debug for logs.")
    print("=" * 72)

    if not args.no_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()

    try:
        while t.is_alive():
            picker.pump()
            threading.Event().wait(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
