#!/usr/bin/env python3
"""Datierungstool: Korrespondenz-Scans über den Dateinamen datieren.

Benennt Bilder nach dem Schema

    d1899-12-31_s2121_e10815_p1.jpg

um (d = Datum, s = Sender, e = Empfänger, p = Seite). Abschriften bekommen
zusätzlich _abschrift vor der Seitenzahl; Bilder, die kein Brief sind, behalten
ihren Namen und bekommen nur _sonstiges angehängt. Liegt im Ordner eine Datei
mit Datierung im Namen (z. B. DLA_d1926-12-08_s14886_e2121.txt), wird sie
vorgeschlagen. Braucht nur Python 3.9+, keine weiteren Pakete.

Start:  python3 date_tool.py [ORDNER]
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from calendar import monthrange
from datetime import date as Date
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- Einstellungen

PORT = 5050
# Erlaubter Zeitraum (fängt Tippfehler ab).
YEAR_MIN, YEAR_MAX = 1879, 1931
# Datum, das beim Start im Feld steht.
DEFAULT_DATE = "1912-05-15"
# Daten, die immer als Schnellauswahl beim Datumsfeld angeboten werden.
QUICK_DATES = ["1912-05-15"]
# Nummern, die immer als Schnellauswahl angeboten werden. Die erste ist der
# Standard-Empfänger.
QUICK_IDS = {"2121": "Schnitzler"}
# Markierung für »unsicher«, direkt hinter dem Datum: d1899-12-31u_…
UNCERTAIN_MARK = "u"
# Markierung für Abschriften, vor der Seitenzahl: …_e10815_abschrift_p1.jpg
COPY_MARK = "abschrift"
# Für Bilder, die nicht datiert werden: IMG_0001.jpg -> IMG_0001_sonstiges.jpg
OTHER_MARK = "sonstiges"
# Ordner mit unbeschnittenen Originalen (im Bildordner oder daneben).
ORIG_DIR_NAME = "originale"
# Protokoll »alter Name → neuer Name« in jedem Bildordner; "" schaltet es ab.
LOG_NAME = "umbenennungen.csv"

# Namen zu den Personennummern kommen aus der PMB ({id} = Nummer);
# "" schaltet die Abfrage ab. Nummern aus QUICK_IDS werden nie abgefragt.
PMB_API = "https://pmb.acdh.oeaw.ac.at/apis/api/entities/person/{id}/?format=json"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"}
CONFIG_FILE = Path.home() / ".dating-correspondences.json"
# Einmal abgefragte Namen bleiben hier gespeichert und stehen auch ohne Netz bereit.
NAMES_FILE = Path.home() / ".dating-correspondences-names.json"
PREVIEW_CACHE = Path(tempfile.gettempdir()) / "dating_orig_preview"
# So lange wartet das Tool nach dem Schließen des Tabs, bevor es sich beendet
# (ein Neuladen der Seite meldet sich in dieser Zeit wieder an).
QUIT_DELAY = 4.0

if sys.platform == "darwin":
    FILE_MANAGER = "Finder"
elif os.name == "nt":
    FILE_MANAGER = "Explorer"
else:
    FILE_MANAGER = "Dateimanager"

# ---------------------------------------------------------------------- Zustand

ROOT: Path | None = None
SERVER: ThreadingHTTPServer | None = None

ISO_RE = re.compile(r"^[\dX]{4}-[\dX]{2}-[\dX]{2}$", re.IGNORECASE)
YEAR_RE = re.compile(r"^\d{4}$")
YEARMO_RE = re.compile(r"^\d{4}-\d{2}$")
# Dateien, die schon benannt sind, gelten als erledigt.
DONE_RE = re.compile(r"^(d[\dX]{4}-[\dX]{2}-[\dX]{2}|undatiert)", re.IGNORECASE)


def is_done(name: str) -> bool:
    """Schon datiert oder als »sonstiges« abgelegt?"""
    stem = os.path.splitext(name)[0].lower()
    return bool(DONE_RE.match(name)) or stem.endswith("_" + OTHER_MARK.lower())


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------ Datum

def year_ok(year: int, any_year: bool) -> bool:
    return any_year or YEAR_MIN <= year <= YEAR_MAX


def validate_iso(s: str, any_year: bool = False) -> bool:
    """JJJJ-MM-TT, X für unlesbare Ziffern. any_year: Zeitraum nicht prüfen."""
    su = s.upper()
    if not ISO_RE.match(su):
        return False
    if "X" not in su:
        try:
            d = Date.fromisoformat(s)
        except ValueError:
            return False
        return year_ok(d.year, any_year)
    year_part, month_part, day_part = su[:4], su[5:7], su[8:10]
    if "X" not in year_part and not year_ok(int(year_part), any_year):
        return False
    if "X" not in month_part and not (1 <= int(month_part) <= 12):
        return False
    if "X" not in day_part and not (1 <= int(day_part) <= 31):
        return False
    return True


def validate_partial(s: str, any_year: bool = False) -> bool:
    """Erlaubt JJJJ, JJJJ-MM oder JJJJ-MM-TT."""
    if YEAR_RE.match(s):
        return year_ok(int(s), any_year)
    if YEARMO_RE.match(s):
        y, m = int(s[:4]), int(s[5:7])
        return year_ok(y, any_year) and 1 <= m <= 12
    return validate_iso(s, any_year)


def expand_partial(s: str) -> tuple[str, str]:
    """(Beginn, Ende) als ISO-Daten; gleich, wenn s schon ein Tagesdatum ist."""
    if YEAR_RE.match(s):
        y = int(s)
        return f"{y:04d}-01-01", f"{y:04d}-12-31"
    if YEARMO_RE.match(s):
        y, m = int(s[:4]), int(s[5:7])
        _, last = monthrange(y, m)
        return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"
    return s, s


def make_date_part(data: dict, trusted: set[str] = frozenset()) -> str:
    """trusted: Daten aus einem Vorschlag – für sie gilt der erlaubte Zeitraum nicht."""
    mode = data.get("mode")
    suffix = UNCERTAIN_MARK if data.get("uncertain") else ""

    if mode == "when":
        d = str(data.get("when", "")).upper()
        if not validate_partial(d, d in trusted):
            raise ApiError(f"Ungültiges Datum: {d}")
        start, end = expand_partial(d)
        return f"{d}{suffix}" if start == end else f"{start}_{end}{suffix}"
    if mode in ("notBeforeAfter", "fromTo"):
        keys = ("notBefore", "notAfter") if mode == "notBeforeAfter" else ("from", "to")
        a, b = (str(data.get(k, "")).upper() for k in keys)
        if not validate_iso(a, a in trusted) or not validate_iso(b, b in trusted):
            raise ApiError(f"Ungültige Daten: {a} / {b}")
        if "X" not in a + b and a > b:
            raise ApiError("Das zweite Datum liegt vor dem ersten")
        return f"{a}_{b}{suffix}"
    raise ApiError(f"Unbekannter Modus: {mode}")


# ------------------------------------------------------------------ Dateinamen

def parse_ids(raw) -> list[str]:
    """'2121, 10815' -> ['2121', '10815'] (ohne Doppelte)."""
    out: list[str] = []
    for tok in re.split(r"[^0-9A-Za-z]+", str(raw or "")):
        if tok and tok not in out:
            out.append(tok)
    return out


def folder_number(name: str) -> str:
    """Nummer aus dem Ordnernamen: 'pmb10815' bzw. die letzte Zifferngruppe."""
    m = re.search(r"pmb[_\- ]?(\d+)", name, re.IGNORECASE)
    if m:
        return m.group(1)
    groups = re.findall(r"\d+", name)
    return groups[-1] if groups else ""


def name_parts(data: dict, trusted: set[str] = frozenset()) -> list[list[str]]:
    """Die Bausteine des Namens ohne Seitenzahl, z. B. [['d', 'd1899-12-31'], ['s', '_s2121']]."""
    if data.get("undatiert"):
        label = re.sub(r"[^0-9A-Za-z]", "", str(data.get("label", "")))
        parts = [["d", "undatiert" + label]]
    else:
        parts = [["d", "d" + make_date_part(data, trusted)]]
    senders = parse_ids(data.get("senders"))
    receivers = parse_ids(data.get("receivers"))
    if senders:
        parts.append(["s", "_s" + "-".join(senders)])
    if receivers:
        parts.append(["e", "_e" + "-".join(receivers)])
    if data.get("copy"):
        parts.append(["a", "_" + COPY_MARK])
    return parts


def next_page(folder: Path, base: str) -> int:
    """Erste freie Seitenzahl für diesen Namen – unabhängig von der Dateiendung."""
    try:
        stems = {f.stem.lower() for f in folder.iterdir()}
    except OSError:
        stems = set()
    n = 1
    while f"{base}_p{n}".lower() in stems:
        n += 1
    return n


def rename_target(data: dict) -> tuple[Path, Path, list[list[str]]]:
    """(Quelle, Ziel, Bausteine des neuen Namens) für ein Bild."""
    folder = resolve_folder(data.get("folder"))
    src = resolve_file(folder, data.get("filename"))
    if data.get("other"):
        # Kein Brief: Name bleibt, nur die Markierung kommt dazu.
        parts = [["name", src.stem], ["o", "_" + OTHER_MARK], ["ext", src.suffix]]
        target = folder / "".join(text for _, text in parts)
        if target.exists():
            raise ApiError(f"{target.name} existiert bereits")
        return src, target, parts
    parts = name_parts(data, suggested_dates(folder_suggestion(folder)))
    base = "".join(text for _, text in parts)
    ext = src.suffix.lower()
    parts += [["p", f"_p{next_page(folder, base)}"], ["ext", ext]]
    return src, folder / "".join(text for _, text in parts), parts


# -------------------------------------------------------------------- Vorschlag

# Eine Datierung irgendwo in einem Dateinamen, z. B. DLA_d1926-12-08_s14886_e2121.txt
_DATE = r"[\dX]{4}-[\dX]{2}-[\dX]{2}"
HINT_RE = re.compile(
    rf"(?:^|_)d(?P<a>{_DATE})(?:_(?P<b>{_DATE}))?(?P<u>{re.escape(UNCERTAIN_MARK)})?"
    rf"(?:_s(?P<s>\d+(?:-\d+)*))?(?:_[er](?P<e>\d+(?:-\d+)*))?"
    rf"(?P<copy>_{re.escape(COPY_MARK)})?(?=_|$)",
    re.IGNORECASE)


def parse_hint(stem: str) -> dict | None:
    """Datierung aus einem Dateinamen – in der Form, in der man sie eintippen würde."""
    m = HINT_RE.search(stem)
    if not m:
        return None
    a, b = m["a"].upper(), (m["b"] or "").upper()
    if not validate_iso(a, True) or (b and not validate_iso(b, True)):
        return None
    if not b or a == b:
        date = {"mode": "when", "when": a}
    elif expand_partial(a[:4]) == (a, b):            # ganzes Jahr
        date = {"mode": "when", "when": a[:4]}
    elif expand_partial(a[:7]) == (a, b):            # ganzer Monat
        date = {"mode": "when", "when": a[:7]}
    else:
        date = {"mode": "notBeforeAfter", "notBefore": a, "notAfter": b}
    ids = lambda g: " ".join((m[g] or "").split("-"))  # noqa: E731
    return {**date, "uncertain": bool(m["u"]), "copy": bool(m["copy"]),
            "senders": ids("s"), "receivers": ids("e")}


def folder_suggestion(folder: Path) -> dict | None:
    """Liegt im Ordner neben den Bildern eine Datei mit Datierung im Namen (und gibt es
    nur eine Datierung), wird sie vorgeschlagen."""
    found: dict[str, list[str]] = {}
    try:
        entries = sorted(folder.iterdir(), key=lambda f: f.name.lower())
    except OSError:
        return None
    for f in entries:
        if f.name.startswith(".") or is_image_name(f.name) or f.name == LOG_NAME:
            continue
        hint = parse_hint(f.stem)
        if hint and f.is_file():
            found.setdefault(json.dumps(hint, sort_keys=True), []).append(f.name)
    if len(found) != 1:
        return None
    key, sources = next(iter(found.items()))
    return {**json.loads(key), "sources": sources}


def suggested_dates(suggestion: dict | None) -> set[str]:
    if not suggestion:
        return set()
    return {suggestion[k] for k in ("when", "notBefore", "notAfter") if k in suggestion}


# -------------------------------------------------------------------- Protokoll

def log_rename(folder: Path, old: str, new: str) -> None:
    """Hält fest, wie ein Bild vorher hieß."""
    if not LOG_NAME:
        return
    path = folder / LOG_NAME
    try:
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["zeit", "alter_name", "neuer_name"])
            writer.writerow([datetime.now().isoformat(timespec="seconds"), old, new])
    except OSError:
        pass  # das Umbenennen selbst hat geklappt


def unlog_rename(folder: Path, old: str, new: str) -> None:
    """Streicht den Eintrag einer zurückgenommenen Umbenennung."""
    if not LOG_NAME:
        return
    path = folder / LOG_NAME
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        for i in range(len(rows) - 1, 0, -1):
            if rows[i][1:3] == [old, new]:
                del rows[i]
                break
        else:
            return
        if len(rows) <= 1:
            path.unlink()
        else:
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
    except OSError:
        pass


# ----------------------------------------------------------------------- Ordner

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def set_root(path: str) -> None:
    global ROOT
    p = Path(path).expanduser()
    if not path or not p.is_dir():
        raise ApiError(f"Ordner nicht gefunden: {path}", 404)
    ROOT = p.resolve()
    recent = [str(ROOT)] + [r for r in load_config().get("recent", []) if r != str(ROOT)]
    try:
        CONFIG_FILE.write_text(
            json.dumps({"root": str(ROOT), "recent": recent[:6]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass


def load_names() -> dict:
    try:
        names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        return names if isinstance(names, dict) else {}
    except (OSError, ValueError):
        return {}


def is_image_name(name: str) -> bool:
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in IMAGE_SUFFIXES


def pending_images(folder: Path) -> list[str]:
    try:
        names = [
            f.name for f in folder.iterdir()
            if is_image_name(f.name) and not is_done(f.name) and f.is_file()
        ]
    except OSError:
        return []
    return sorted(names, key=str.lower)


def get_folders() -> list[dict]:
    if ROOT is None:
        return []
    entries = [(".", ROOT)]
    try:
        subdirs = sorted(
            (e for e in ROOT.iterdir()
             if e.is_dir() and not e.name.startswith(".") and e.name != ORIG_DIR_NAME),
            key=lambda e: e.name.lower(),
        )
    except OSError:
        subdirs = []
    entries += [(e.name, e) for e in subdirs]
    folders = []
    for key, path in entries:
        count = len(pending_images(path))
        if count:
            folders.append({"key": key, "name": path.name or str(path), "count": count,
                            "number": folder_number(path.name), "self": key == "."})
    return folders


def resolve_folder(key) -> Path:
    if ROOT is None:
        raise ApiError("Kein Ordner gewählt")
    key = str(key or ".")
    if key == ".":
        return ROOT
    if Path(key).name != key or key == "..":
        raise ApiError("Ungültiger Ordner")
    folder = ROOT / key
    if not folder.is_dir():
        raise ApiError("Ordner nicht gefunden", 404)
    return folder


def resolve_file(folder: Path, filename, must_exist: bool = True) -> Path:
    filename = str(filename or "")
    if not filename or Path(filename).name != filename:
        raise ApiError("Ungültiger Dateiname")
    path = folder / filename
    if must_exist and not path.is_file():
        raise ApiError("Datei nicht gefunden", 404)
    return path


# ------------------------------------------------------------------ Ordnerwahl

def count_images(folder) -> int | None:
    """Anzahl der Bilder direkt im Ordner (None = kein Zugriff)."""
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it if is_image_name(e.name) and e.is_file())
    except OSError:
        return None


def places() -> list[dict]:
    """Einstiegspunkte für die Ordnerwahl."""
    home = Path.home()
    out = [{"label": "Benutzerordner", "path": str(home)}]
    for label, name in (("Schreibtisch", "Desktop"), ("Dokumente", "Documents"),
                        ("Bilder", "Pictures"), ("Downloads", "Downloads")):
        if (home / name).is_dir():
            out.append({"label": label, "path": str(home / name)})
    if sys.platform == "darwin":
        out.append({"label": "Laufwerke", "path": "/Volumes"})
    elif os.name == "nt":
        out += [{"label": f"{c}:", "path": f"{c}:\\"}
                for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
    else:
        out += [{"label": p, "path": p} for p in ("/media", "/mnt") if os.path.isdir(p)]
    return out


def subdir_images(entry) -> int | None:
    """Bilder in einem Unterordner – der Originale-Ordner zählt nicht als Korrespondenz."""
    return 0 if entry.name == ORIG_DIR_NAME else count_images(entry.path)


def hub_dirs() -> set[Path]:
    """Sammelorte (Benutzerordner, Laufwerksliste …), die selbst nie der Bildordner sind."""
    home = Path.home().resolve()
    return {home, home.parent, Path(home.anchor), Path("/Volumes")}


def api_browse(q: dict) -> dict:
    """Inhalt eines Ordners für die Ordnerwahl im Browser."""
    raw = (q.get("path") or "").strip()
    path = Path(raw).expanduser() if raw else (ROOT or Path.home())
    if not path.is_dir():
        raise ApiError(f"Ordner nicht gefunden: {raw}", 404)
    path = path.resolve()
    error = None
    try:
        with os.scandir(path) as it:
            subdirs = sorted((e for e in it if not e.name.startswith(".") and e.is_dir()),
                             key=lambda e: e.name.lower())
    except OSError:
        subdirs, error = [], "Kein Zugriff auf diesen Ordner."
    # In Sammelorten nicht in jeden Unterordner schauen: macOS fragt sonst für
    # Schreibtisch, Dokumente, Downloads … einzeln um Erlaubnis.
    hub = path in hub_dirs()
    count_them = not hub and len(subdirs) <= 400
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "dirs": [{"name": e.name, "path": e.path,
                  "images": subdir_images(e) if count_them else None} for e in subdirs],
        "images": count_images(path) or 0,
        "usable": not hub,
        "error": error,
        "places": places(),
        "recent": [r for r in load_config().get("recent", []) if os.path.isdir(r)],
    }


# -------------------------------------------------------------------- Originale

def orig_dir() -> Path | None:
    if ROOT is None:
        return None
    for cand in (ROOT / ORIG_DIR_NAME, ROOT.parent / ORIG_DIR_NAME):
        if cand.is_dir():
            return cand
    return None


def find_original(filename: str) -> tuple[str, Path | None]:
    """Unbeschnittenes Original = Nummer des Ausschnitts + 1.

    'B0410_CUL_FOTO_CUT_2767.jpg' -> ('2768', …/CUL_FOTO_ORIG_2768.heic)
    """
    m = re.search(r"(\d+)(?=\.[^.]+$)", filename)
    if not m:
        raise ApiError("Keine Nummer im Dateinamen")
    num = m.group(1)
    orig_num = str(int(num) + 1).zfill(len(num))
    return orig_num, find_original_by_number(orig_num)


def find_original_by_number(orig_num: str) -> Path | None:
    base = orig_dir()
    if base is None:
        return None
    pattern = re.compile(rf"(^|\D){re.escape(orig_num)}$")
    for f in sorted(base.iterdir()):
        if f.is_file() and not f.name.startswith(".") and pattern.search(f.stem):
            return f
    return None


def to_jpeg(src: Path, dst: Path) -> bool:
    """Original als JPG ablegen (HEIC wird mit macOS-`sips` umgewandelt)."""
    if src.suffix.lower() in (".jpg", ".jpeg"):
        shutil.copyfile(src, dst)
        return True
    if not shutil.which("sips"):
        return False
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and dst.is_file()


# ----------------------------------------------------------- Beenden mit dem Tab

PAGES: set[str] = set()
_quit_timer: threading.Timer | None = None
_quit_lock = threading.Lock()


def stop_server(reason: str = "") -> None:
    if reason:
        print(f"Beendet: {reason}.")
    if SERVER:
        SERVER.shutdown()


def page_seen(page_id: str) -> None:
    """Ein Tab arbeitet mit dem Tool – ein geplantes Beenden entfällt."""
    global _quit_timer
    with _quit_lock:
        PAGES.add(page_id)
        if _quit_timer:
            _quit_timer.cancel()
            _quit_timer = None


def page_gone(page_id: str) -> None:
    """Ein Tab wurde geschlossen; war es der letzte, beendet sich das Tool."""
    global _quit_timer
    with _quit_lock:
        PAGES.discard(page_id)
        if not PAGES and _quit_timer is None:
            _quit_timer = threading.Timer(QUIT_DELAY, stop_server, args=("Browser-Tab geschlossen",))
            _quit_timer.daemon = True
            _quit_timer.start()


# -------------------------------------------------------------------------- API

def api_state(_q=None) -> dict:
    return {
        "root": str(ROOT) if ROOT else None,
        "rootName": (ROOT.name or str(ROOT)) if ROOT else None,
        "folders": get_folders(),
        "hasOriginals": orig_dir() is not None,
        "yearMin": YEAR_MIN, "yearMax": YEAR_MAX,
        "defaultDate": DEFAULT_DATE,
        "quickIds": QUICK_IDS,
        "quickDates": QUICK_DATES,
        "copyMark": COPY_MARK,
        "otherMark": OTHER_MARK,
        "fileManager": FILE_MANAGER,
        "pmbApi": PMB_API,
        "names": load_names(),
    }


def api_root(data: dict) -> dict:
    set_root(str(data.get("path", "")).strip())
    return api_state()


def api_images(q: dict) -> dict:
    folder = resolve_folder(q.get("folder"))
    return {"images": pending_images(folder), "suggestion": folder_suggestion(folder)}


def api_preview(data: dict) -> dict:
    """Der Name, den das Bild bekäme – ohne umzubenennen."""
    _, target, parts = rename_target(data)
    return {"new": target.name, "parts": parts}


def api_rename(data: dict) -> dict:
    src, target, _ = rename_target(data)
    src.rename(target)
    log_rename(src.parent, src.name, target.name)
    return {"old": src.name, "new": target.name}


def api_undo(data: dict) -> dict:
    folder = resolve_folder(data.get("folder"))
    new = resolve_file(folder, data.get("new"))
    old = resolve_file(folder, data.get("old"), must_exist=False)
    if not is_done(new.name):
        raise ApiError("Nur eigene Umbenennungen können zurückgenommen werden")
    if old.exists():
        raise ApiError(f"{old.name} existiert bereits")
    new.rename(old)
    unlog_rename(folder, old.name, new.name)
    return {"restored": old.name}


def api_reveal(data: dict) -> dict:
    """Zeigt den Ordner im Dateimanager."""
    folder = resolve_folder(data.get("folder"))
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as e:
        raise ApiError(f"Ordner lässt sich nicht öffnen: {e}", 500)
    return {"ok": True}


def api_original_info(q: dict) -> dict:
    filename = q.get("file", "")
    try:
        orig_num, path = find_original(filename)
    except ApiError as e:
        return {"found": False, "error": str(e)}
    if path is None:
        return {"found": False, "error": f"Kein Original mit der Nummer {orig_num} gefunden"}
    return {"found": True, "orig_num": orig_num, "orig_name": path.name}


def original_preview_path(q: dict) -> Path:
    orig_num = q.get("num", "")
    if not re.fullmatch(r"\d+", orig_num):
        raise ApiError("ungültig")
    path = find_original_by_number(orig_num)
    if path is None:
        raise ApiError("Original nicht gefunden", 404)
    if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
        return path
    PREVIEW_CACHE.mkdir(exist_ok=True)
    cached = PREVIEW_CACHE / f"{path.stem}.jpg"
    if not cached.is_file() and not to_jpeg(path, cached):
        raise ApiError("Konvertierung fehlgeschlagen (HEIC braucht macOS)", 500)
    return cached


def api_use_original(data: dict) -> dict:
    """Ersetzt den Ausschnitt durch das unbeschnittene Original (als JPG)."""
    folder = resolve_folder(data.get("folder"))
    cut_path = resolve_file(folder, data.get("filename"))
    orig_num, orig_path = find_original(cut_path.name)
    if orig_path is None:
        raise ApiError(f"Kein Original mit der Nummer {orig_num} gefunden", 404)

    base = cut_path.stem
    if "CUT" in base:
        new_base = re.sub(r"CUT_?\d+", f"ORIG_{orig_num}", base)
    else:
        new_base = re.sub(r"\d+$", "", base).rstrip("_") + f"_ORIG_{orig_num}"
    out_path = folder / f"{new_base}.jpg"
    if out_path.exists():
        raise ApiError(f"{out_path.name} existiert bereits")
    if not to_jpeg(orig_path, out_path):
        raise ApiError("Konvertierung fehlgeschlagen (HEIC braucht macOS)", 500)
    cut_path.unlink()
    return {"new_filename": out_path.name}


_names_lock = threading.Lock()


def fetch_json(url: str):
    """Holt JSON von einer Adresse; None, wenn es den Eintrag nicht gibt (404)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "dating-correspondences-tool"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except urllib.error.URLError:
        # Manchen Python-Installationen (v. a. unter macOS) fehlen die Zertifikate; curl hat sie.
        if not shutil.which("curl"):
            raise
        result = subprocess.run(["curl", "-sS", "-m", "8", "-H", "Accept: application/json",
                                 "-w", "\n%{http_code}", url], capture_output=True, text=True)
        body, _, status = result.stdout.rpartition("\n")
        if result.returncode != 0 or status not in ("200", "404"):
            raise
        return json.loads(body) if status == "200" else None


def api_person(q: dict) -> dict:
    """Name und Lebensjahre zu einer Personennummer – aus dem Speicher oder aus der PMB."""
    person_id = q.get("id", "")
    if not PMB_API or not re.fullmatch(r"\d{1,12}", person_id):
        raise ApiError("Keine Personennummer")
    known = load_names().get(person_id)
    if known:
        return known
    try:
        person = fetch_json(PMB_API.replace("{id}", person_id))
    except Exception as e:  # noqa: BLE001 – kein Netz, Zeitüberschreitung, unerwartete Antwort
        raise ApiError(f"PMB nicht erreichbar: {e}", 502)
    if not isinstance(person, dict):
        return {"missing": True}
    year = lambda d: str(d or "")[:4]  # noqa: E731
    born, died = year(person.get("start_date")), year(person.get("end_date"))
    info = {
        "name": " ".join(str(x).strip() for x in (person.get("first_name"), person.get("name")) if x)
                or f"Nr. {person_id}",
        "dates": f"{born or '?'}–{died or '?'}" if born or died else "",
    }
    with _names_lock:
        names = load_names()
        names[person_id] = info
        try:
            NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return info


def api_bye(data: dict) -> dict:
    page_gone(str(data.get("page", "")))
    return {"ok": True}


def api_shutdown(_data: dict) -> dict:
    threading.Timer(0.3, stop_server).start()
    return {"ok": True}


GET_ROUTES = {
    "/api/state": api_state,
    "/api/images": api_images,
    "/api/browse": api_browse,
    "/api/person": api_person,
    "/api/original-info": api_original_info,
}
POST_ROUTES = {
    "/api/root": api_root,
    "/api/preview": api_preview,
    "/api/rename": api_rename,
    "/api/undo": api_undo,
    "/api/reveal": api_reveal,
    "/api/use-original": api_use_original,
    "/api/bye": api_bye,
    "/api/shutdown": api_shutdown,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # ruhig bleiben
        pass

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {"Cache-Control": "no-store"}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _file(self, path: Path) -> None:
        """Bild ausliefern; der Browser darf es behalten, fragt aber jedes Mal nach."""
        st = path.stat()
        headers = {"Cache-Control": "no-cache", "ETag": f'"{st.st_mtime_ns:x}-{st.st_size:x}"'}
        if self.headers.get("If-None-Match") == headers["ETag"]:
            self.send_response(304)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype, headers)

    def _local_only(self) -> bool:
        """Nur Anfragen des eigenen Browser-Tabs zulassen."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1")

    def _handle(self, fn) -> None:
        try:
            if not self._local_only():
                raise ApiError("Nur lokal erreichbar", 403)
            page = self.headers.get("X-Page")
            if page:
                page_seen(page)
            fn()
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 – Fehler im Browser anzeigen
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self) -> None:
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _get(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/image":
            self._file(resolve_file(resolve_folder(q.get("folder")), q.get("file")))
        elif url.path == "/api/original-preview":
            self._file(original_preview_path(q))
        elif url.path in GET_ROUTES:
            self._json(GET_ROUTES[url.path](q))
        else:
            raise ApiError("not found", 404)

    def _post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        if path not in POST_ROUTES:
            raise ApiError("not found", 404)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError("JSON erwartet", 415)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError("Ungültiges JSON")
        if not isinstance(data, dict):
            raise ApiError("JSON-Objekt erwartet")
        self._json(POST_ROUTES[path](data))


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Datierungstool</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #1a1a1a; --panel: #212121; --side: #242424; --line: #333; --text: #e0e0e0; --muted: #8c8c8c;
  --accent: #5b8dd9; --accent-bg: #2a3f60; --accent-text: #9ecfff;
  /* Farben der Namensbausteine: Datum, Sender, Empfänger, Abschrift, Seite */
  --c-d: #e8c36a; --c-s: #7fd1a0; --c-e: #7fb4f0; --c-a: #ec9a7e; --c-p: #c9a0e8;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
[hidden] { display: none !important; }
html, body { height: 100%; }
body { font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--text); display: flex; overflow: hidden; }
button, input { font: inherit; color: inherit; }
button { cursor: pointer; }
input[type="text"], input[type="search"] {
  padding: 6px 9px; border-radius: 6px; border: 1px solid #444; background: #2e2e2e; outline: none;
}
input[type="text"]:focus, input[type="search"]:focus { border-color: var(--accent); }
kbd {
  display: inline-block; min-width: 18px; padding: 0 4px; text-align: center;
  font: 11px/16px var(--mono); border: 1px solid #555; border-radius: 4px; color: #bbb;
}
.caps { font-size: 11px; font-weight: 600; color: #999; letter-spacing: .06em; text-transform: uppercase; }

/* ---- links: Ordner ---- */
#sidebar { width: 250px; flex: none; background: var(--side); border-right: 1px solid var(--line);
           display: flex; flex-direction: column; }
body.nosidebar #sidebar { width: 30px; cursor: pointer; }
body.nosidebar #sidebar > :not(#side-head) { display: none; }
body.nosidebar #side-head { flex: 1; flex-direction: column-reverse; justify-content: flex-end; gap: 12px;
                            padding: 7px 0; border-bottom: none; }
body.nosidebar #side-head .caps { writing-mode: vertical-rl; }
#side-head { display: flex; align-items: center; justify-content: space-between;
             padding: 7px 8px 7px 14px; background: #1e1e1e; border-bottom: 1px solid var(--line); }
.icon-btn { background: transparent; border: 1px solid transparent; border-radius: 5px; color: #999;
            padding: 1px 8px; font-size: 15px; line-height: 20px; }
.icon-btn:hover { background: #2e2e2e; color: #ddd; }
#root-btn { display: flex; align-items: baseline; gap: 8px; width: 100%; text-align: left;
            padding: 9px 14px; background: transparent; border: none; border-bottom: 1px solid var(--line); }
#root-btn:hover { background: #2e2e2e; }
#root-name { flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#root-btn span:last-child { font-size: 11px; color: var(--accent-text); white-space: nowrap; }
#search { margin: 10px 10px 6px; }
#folder-list { overflow-y: auto; flex: 1; min-height: 60px; }
.folder-item { padding: 8px 14px; cursor: pointer; border-bottom: 1px solid #2c2c2c;
               display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.folder-item span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.folder-item:hover { background: #2e2e2e; }
.folder-item.active { background: var(--accent-bg); }
.badge { background: #333; color: #999; padding: 1px 7px; border-radius: 10px; font-size: 11px; }
.folder-item.active .badge { background: #1e3a6e; color: #7aadff; }
#side-foot { display: flex; align-items: center; gap: 8px; padding: 8px 10px 8px 14px;
             border-top: 1px solid var(--line); font-size: 11px; color: #777; }
#count-info { flex: 1; }
#btn-shutdown { padding: 3px 10px; background: transparent; border: 1px solid #622; border-radius: 5px;
                color: #b66; font-size: 11px; }
#btn-shutdown:hover { background: #3a1010; color: #e88; border-color: #a44; }

/* ---- Mitte: Bild – ohne Bedienelemente, damit nichts den Brief verdeckt ---- */
#viewer { flex: 1; min-width: 0; position: relative; overflow: hidden; background: #111;
          display: flex; align-items: center; justify-content: center; }
#img-display { max-width: 100%; max-height: 100%; display: none; user-select: none;
               -webkit-user-drag: none; transition: transform .08s ease-out; }
#img-display.zoomed { cursor: grab; }
#img-display.dragging { cursor: grabbing; transition: none; }
body.has-image #img-display { display: block; }
body.has-image #placeholder { display: none; }
#placeholder { color: #888; font-size: 15px; text-align: center; line-height: 1.7; padding: 24px; }
#placeholder b { color: #ccc; font-weight: 600; }
.big-btn { display: inline-block; margin-top: 14px; padding: 10px 22px; font-size: 15px;
           background: var(--accent-bg); border: 1px solid var(--accent); border-radius: 7px;
           color: var(--accent-text); }
.big-btn:hover, .big-btn:focus { background: #34507a; outline: none; }

/* ---- rechts: Eingabe ---- */
#panel { width: 372px; flex: none; background: var(--panel); border-left: 1px solid var(--line);
         display: flex; flex-direction: column; gap: 14px; padding: 14px 16px 12px; overflow-y: auto; }
#panel.off .needs-folder { opacity: .35; pointer-events: none; }
#folder-row { display: flex; align-items: baseline; gap: 10px; }
#folder-title { flex: 1; font-size: 15px; font-weight: 600; color: #dfe8fb;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link-btn { background: none; border: none; color: var(--accent-text); font-size: 11px; padding: 0;
            white-space: nowrap; }
.link-btn:hover { text-decoration: underline; }
#image-row { display: flex; align-items: center; gap: 4px; margin-top: 9px; }
#img-counter { min-width: 92px; text-align: center; color: #ccc; font-size: 12px; white-space: nowrap; }
.spacer { flex: 1; }
.tool { width: 28px; height: 26px; flex: none; background: transparent; border: 1px solid #444;
        border-radius: 6px; color: #bbb; display: flex; align-items: center; justify-content: center; }
.tool:hover { background: #2e2e2e; color: #fff; border-color: #666; }
.tool svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.7;
            stroke-linecap: round; stroke-linejoin: round; }
#suggest { display: flex; align-items: baseline; gap: 8px; margin-top: 8px; padding: 5px 9px; font-size: 11.5px;
           border-radius: 6px; background: #1b2a21; border: 1px solid #2e5039; color: #9fd8b0; }
#suggest-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#suggest-text code { font-family: var(--mono); font-size: 11px; }
#suggest.changed { background: transparent; border-color: #3a3a3a; color: #8c8c8c; }
#file-info { font-size: 11px; color: var(--muted); margin-top: 7px; min-height: 16px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.sect { display: flex; flex-direction: column; gap: 7px; }
.sect-title { font-size: 11px; color: #999; text-transform: uppercase; letter-spacing: .05em;
              display: flex; align-items: center; gap: 6px; }
.sect-title i { font-style: normal; text-transform: none; letter-spacing: 0; color: #6c6c6c; }
.k { width: 16px; height: 16px; border-radius: 4px; text-align: center; color: #111;
     font: 700 11px/16px var(--mono); text-transform: none; letter-spacing: 0; }
.k-d { background: var(--c-d); }
.k-s { background: var(--c-s); }
.k-e { background: var(--c-e); }

.mode-tabs { display: flex; gap: 4px; }
.mode-tab { padding: 4px 7px; flex: none; border-radius: 14px; border: 1px solid #444; background: transparent;
            color: #aaa; font-size: 11.5px; white-space: nowrap; transition: all .15s; }
.mode-tab.active { background: var(--accent-bg); border-color: var(--accent); color: var(--accent-text); }
.mode-tab:hover:not(.active) { background: #2a2a2a; }

.date-inputs { display: flex; gap: 10px; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field label { font-size: 11px; color: #888; }
.field label i { font-style: normal; color: #666; }
.date-field { width: 126px; font: 15px var(--mono) !important; }
.nr-field { width: 126px; font: 15px var(--mono) !important; }
.date-field.invalid { border-color: #c0392b !important; }
.date-extra { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 24px; }
.date-hint { font-size: 11px; color: #777; }

.chips { display: flex; gap: 5px; flex-wrap: wrap; }
.chip { padding: 2px 9px; border-radius: 11px; border: 1px solid #555; background: transparent;
        color: #aaa; font-size: 11px; white-space: nowrap; transition: all .12s; }
.chip:hover { background: #2a2a2a; color: #ddd; }
.chip.on { background: var(--accent-bg); border-color: var(--accent); color: var(--accent-text); }
.chip.mono { font-family: var(--mono); font-size: 12px; }
.check-row { display: flex; align-items: center; gap: 7px; color: #bbb; cursor: pointer; white-space: nowrap; }
.check-row input { width: 15px; height: 15px; cursor: pointer; accent-color: var(--accent); }
#copy-row { align-self: flex-start; }
#copy-row .hint { font-size: 11px; color: #6c6c6c; }
#copy-row .hint code { font-family: var(--mono); color: #8a8a8a; }
#copy-row:has(input:checked) { color: var(--c-a); }

.person { display: flex; flex-direction: column; gap: 4px; }
.person-line { display: flex; align-items: center; gap: 8px; }
.person-field { width: 96px; flex: none; font: 15px var(--mono) !important; }
.names { font-size: 12px; color: #c9d3e6; min-height: 17px; line-height: 17px; }
.names .dim { color: #777; }
.names .warn { color: #d9a25e; }
#btn-swap { align-self: flex-start; margin: -1px 0 -1px 22px; padding: 2px 10px; background: transparent;
            border: 1px solid #444; border-radius: 11px; color: #aaa; font-size: 11px; }
#btn-swap:hover { background: var(--accent-bg); border-color: var(--accent); color: var(--accent-text); }

#preview-box { background: #191919; border: 1px solid #2f2f2f; border-radius: 8px; padding: 9px 11px; gap: 4px; }
#preview-name { font-family: var(--mono); font-size: 13.5px; line-height: 1.45; word-break: break-all;
                min-height: 39px; }
#preview-name span { display: inline-block; }      /* Umbruch nur zwischen den Bausteinen */
#preview-name.hint { font-family: inherit; font-size: 12px; color: #c9a25e; word-break: normal; }
.seg-d { color: var(--c-d); }
.seg-s { color: var(--c-s); }
.seg-e { color: var(--c-e); }
.seg-a { color: var(--c-a); }
.seg-name { color: #bbb; }
.seg-o { color: var(--c-a); }
.seg-p { color: var(--c-p); }
.seg-ext { color: #888; }

#btn-submit { width: 100%; padding: 10px; background: #2e6fd4; border: none; border-radius: 7px;
              color: #fff; font-size: 14px; font-weight: 600; transition: background .15s; }
#btn-submit:hover { background: #3a7de4; }
#btn-submit:disabled { background: #2a2a2a; color: #555; cursor: default; }
#btn-submit kbd { border-color: rgba(255,255,255,.45); color: #fff; margin-left: 6px; }
.actions { display: flex; flex-direction: column; gap: 8px; }
.btn-row { display: flex; gap: 8px; }
.btn2 { flex: 1; padding: 6px 10px; background: transparent; border: 1px solid #444; border-radius: 6px;
        color: #aaa; font-size: 12.5px; white-space: nowrap; transition: all .15s; }
.btn2:hover { background: #2a2a2a; color: #ddd; }
.btn2:disabled { color: #4a4a4a; border-color: #303030; cursor: default; background: transparent; }
#btn-original { border-color: #466; color: #9bb; }
#btn-original:hover { background: #1a2a2a; color: #bdd; }
#status-msg { font-size: 12px; color: #6a9; min-height: 34px; word-break: break-word; }
#status-msg.error { color: #e77; }
#keys { margin-top: auto; padding-top: 10px; border-top: 1px solid var(--line); font-size: 11px; color: #888;
        display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 5px 7px; align-items: center; }
#keys .wide { grid-column: span 3; }

/* ---- Dialoge ---- */
.overlay { display: none; position: fixed; inset: 0; z-index: 100; background: rgba(0,0,0,.75);
           align-items: center; justify-content: center; flex-direction: column; }
.overlay.open { display: flex; }
.dialog { width: min(640px, 92vw); max-height: 88vh; background: #242424; border: 1px solid #3a3a3a;
          border-radius: 12px; padding: 20px 22px; display: flex; flex-direction: column; gap: 12px;
          box-shadow: 0 20px 60px rgba(0,0,0,.6); }
.dialog h2 { font-size: 17px; font-weight: 600; }
.dialog p { color: #aaa; line-height: 1.5; }
.dialog code { font-family: var(--mono); color: #ccc; }
.ch-path-row { display: flex; gap: 8px; }
#ch-path { flex: 1; font-family: var(--mono); font-size: 12px; }
#ch-up { padding: 0 12px; background: transparent; border: 1px solid #444; border-radius: 6px; color: #bbb;
         white-space: nowrap; }
#ch-up:hover:not(:disabled) { background: #2e2e2e; color: #fff; }
#ch-up:disabled { opacity: .35; cursor: default; }
.ch-places { display: flex; flex-direction: column; gap: 6px; }
.ch-places > div { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ch-places .caps { font-size: 10px; margin-right: 2px; }
#ch-list { height: min(46vh, 430px); flex: none; overflow-y: auto; border: 1px solid #333; border-radius: 8px;
           background: #1c1c1c; }
.ch-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
           border-bottom: 1px solid #272727; cursor: pointer; }
.ch-item:hover { background: #2a2a2a; }
.ch-item svg { width: 16px; height: 16px; flex: none; fill: #6f8fbf; }
.ch-item span:nth-child(2) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ch-count { margin-left: auto; font-size: 11px; color: var(--c-s); white-space: nowrap; }
.ch-empty { padding: 20px; color: #777; text-align: center; }
.ch-foot { display: flex; align-items: center; gap: 10px; }
#ch-summary { flex: 1; font-size: 12px; color: #aaa; }
#ch-summary.error { color: #e77; }
.dlg-btn { padding: 8px 16px; border-radius: 6px; border: 1px solid #555; background: transparent; color: #bbb;
           max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: none; }
.dlg-btn:hover { background: #2e2e2e; color: #fff; }
.dlg-btn.primary { background: #2e6fd4; border-color: #2e6fd4; color: #fff; font-weight: 600; }
.dlg-btn.primary:hover { background: #3a7de4; }
.dlg-btn.primary:disabled { background: #2a2a2a; border-color: #333; color: #555; cursor: default; }

#orig-overlay { background: rgba(0,0,0,.93); }
#orig-overlay img { max-width: 88vw; max-height: 76vh; object-fit: contain; border: 1px solid #444; background: #111; }
#orig-title { color: #ddd; font-size: 14px; margin-bottom: 10px; }
#orig-loading { color: #aaa; font-size: 15px; }
.orig-actions { margin-top: 16px; display: flex; gap: 12px; }
</style>
</head>
<body>

<div id="sidebar" onclick="if (document.body.classList.contains('nosidebar')) toggleSidebar()">
  <div id="side-head">
    <span class="caps">Ordner</span>
    <button class="icon-btn" id="btn-side" onclick="event.stopPropagation(); toggleSidebar()"></button>
  </div>
  <button id="root-btn" onclick="openChooser()">
    <span id="root-name"></span><span>wechseln…</span>
  </button>
  <input id="search" type="search" placeholder="Ordner suchen…" oninput="filterList()">
  <div id="folder-list"></div>
  <div id="side-foot">
    <span id="count-info"></span>
    <button id="btn-shutdown" onclick="shutdown()" title="Tool beenden (passiert auch beim Schließen des Tabs)">Beenden</button>
  </div>
</div>

<div id="viewer">
  <div id="placeholder"></div>
  <img id="img-display" alt="">
</div>

<div id="panel" class="off">
  <div>
    <div id="folder-row">
      <div id="folder-title">Kein Ordner gewählt</div>
      <button id="btn-reveal" class="link-btn needs-folder" onclick="revealFolder()" tabindex="-1"></button>
    </div>
    <div id="image-row" class="needs-folder">
      <button class="tool" onclick="navigate(-1)" tabindex="-1" title="Vorheriges Bild (Taste ↑)"><svg viewBox="0 0 16 16"><path d="M10 3 5 8l5 5"/></svg></button>
      <span id="img-counter"></span>
      <button class="tool" onclick="navigate(1)" tabindex="-1" title="Nächstes Bild (Taste ↓)"><svg viewBox="0 0 16 16"><path d="m6 3 5 5-5 5"/></svg></button>
      <span class="spacer"></span>
      <button class="tool" onclick="zoomStep(1 / 1.5)" tabindex="-1" title="Verkleinern"><svg viewBox="0 0 16 16"><path d="M3 8h10"/></svg></button>
      <button class="tool" onclick="zoomStep(1.5)" tabindex="-1" title="Vergrößern (auch: Mausrad oder Doppelklick ins Bild)"><svg viewBox="0 0 16 16"><path d="M3 8h10M8 3v10"/></svg></button>
      <button class="tool" onclick="resetView(false)" tabindex="-1" title="Ganzes Bild zeigen"><svg viewBox="0 0 16 16"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/></svg></button>
      <button class="tool" onclick="rotateView()" tabindex="-1" title="Ansicht drehen (Taste R) – die Datei bleibt unverändert"><svg viewBox="0 0 16 16"><path d="M13 8a5 5 0 1 1-1.6-3.7"/><path d="M13.4 1.8v3h-3"/></svg></button>
    </div>
    <div id="file-info"></div>
    <div id="suggest" hidden>
      <span id="suggest-text"></span>
      <button class="link-btn" id="suggest-apply" onclick="applySuggestion()" tabindex="-1"
              title="Datum, Sender und Empfänger wieder aus dem Vorschlag übernehmen">wieder eintragen</button>
    </div>
  </div>

  <div class="sect needs-folder">
    <div class="sect-title"><span class="k k-d">d</span> Datum</div>
    <div class="mode-tabs">
      <button class="mode-tab active" tabindex="-1" onclick="setMode('when')" title="when – ein Tag, ein Monat oder ein Jahr">Datum</button>
      <button class="mode-tab" tabindex="-1" onclick="setMode('notBeforeAfter')" title="notBefore / notAfter">frühestens / spätestens</button>
      <button class="mode-tab" tabindex="-1" onclick="setMode('fromTo')" title="from / to">von / bis</button>
      <button class="mode-tab" tabindex="-1" onclick="setMode('undatiert')" title="Ohne Datum benennen: undatiert_s…_e…">undatiert</button>
    </div>
    <div class="date-inputs" id="date-inputs"></div>
    <div class="date-extra" id="date-extra">
      <div class="chips" id="quick-dates"></div>
      <label class="check-row" title="Hängt ein u ans Datum (Taste U)">
        <input type="checkbox" id="chk-uncertain"> unsicher
      </label>
    </div>
    <div class="date-hint" id="date-hint"></div>
  </div>

  <div class="sect needs-folder">
    <div class="person">
      <label class="sect-title" for="p-s"><span class="k k-s">s</span> Von <i>(Sender)</i></label>
      <div class="person-line">
        <input type="text" class="person-field" id="p-s" placeholder="Nr." oninput="onPersonInput()">
        <div class="chips" id="chips-s"></div>
      </div>
      <div class="names" id="names-s"></div>
    </div>
    <button id="btn-swap" onclick="swapPersons()" tabindex="-1" title="Sender und Empfänger tauschen (Taste S)">&#8593;&#8595; tauschen</button>
    <div class="person">
      <label class="sect-title" for="p-e"><span class="k k-e">e</span> An <i>(Empfänger)</i></label>
      <div class="person-line">
        <input type="text" class="person-field" id="p-e" placeholder="Nr." oninput="onPersonInput()">
        <div class="chips" id="chips-e"></div>
      </div>
      <div class="names" id="names-e"></div>
    </div>
  </div>

  <div class="sect needs-folder">
    <label class="check-row" id="copy-row">
      <input type="checkbox" id="chk-copy"> Abschrift <span class="hint"></span>
    </label>
  </div>

  <div class="sect needs-folder" id="preview-box">
    <div class="sect-title">Neuer Name</div>
    <div id="preview-name"></div>
  </div>

  <div class="actions">
    <button id="btn-submit" class="needs-folder" onclick="submitDate()" disabled>Umbenennen &amp; weiter <kbd>&#8629;</kbd></button>
    <div class="btn-row">
      <button class="btn2 needs-folder" id="btn-skip" onclick="skipImage()" title="Nächstes Bild, ohne umzubenennen (↓)">Überspringen</button>
      <button class="btn2 needs-folder" id="btn-other" onclick="markOther()">Sonstiges</button>
      <button class="btn2" id="btn-undo" onclick="undo()" disabled>Rückgängig</button>
    </div>
    <button class="btn2 needs-folder" id="btn-original" onclick="useOriginal()" hidden title="Ausschnitt durch das unbeschnittene Foto ersetzen">Unbeschnittenes Original verwenden</button>
  </div>
  <div id="status-msg"></div>

  <div id="keys">
    <kbd>&#8629;</kbd><span>umbenennen &amp; weiter</span>
    <kbd>S</kbd><span>Von/An tauschen</span>
    <kbd>&#8595;</kbd><span>nächstes Bild</span>
    <kbd>U</kbd><span>unsicher</span>
    <kbd>&#8593;</kbd><span>vorheriges Bild</span>
    <kbd>A</kbd><span>Abschrift</span>
    <kbd>O</kbd><span>sonstiges &amp; weiter</span>
    <kbd>R</kbd><span>Ansicht drehen</span>
  </div>
</div>

<div id="chooser" class="overlay">
  <div class="dialog">
    <h2>Wo liegen die Bilder?</h2>
    <p>In den Ordner mit den Bildern gehen – oder in den Ordner, der pro Korrespondenz einen Unterordner enthält.</p>
    <div class="ch-path-row">
      <button id="ch-up" onclick="chooserUp()" title="Eine Ebene höher">&#8593; höher</button>
      <input id="ch-path" type="text" spellcheck="false" title="Pfad – kann auch eingefügt werden (Enter)"
             onkeydown="if (event.key === 'Enter') browse(this.value)">
    </div>
    <div class="ch-places" id="ch-places"></div>
    <div id="ch-list"></div>
    <div class="ch-foot">
      <div id="ch-summary"></div>
      <button class="dlg-btn" id="ch-cancel" onclick="closeChooser()">Abbrechen</button>
      <button class="dlg-btn primary" id="ch-ok" onclick="chooseCurrent()"></button>
    </div>
  </div>
</div>

<div id="orig-overlay" class="overlay">
  <div id="orig-title"></div>
  <div id="orig-loading"></div>
  <img id="orig-img" alt="" hidden>
  <div class="orig-actions">
    <button class="dlg-btn primary" id="orig-yes" onclick="confirmOriginal()" hidden>Ja, Bild ersetzen</button>
    <button class="dlg-btn" onclick="closeOriginal()">Abbrechen</button>
  </div>
</div>

<div id="gone" class="overlay">
  <div class="dialog">
    <h2 id="gone-title">Das Tool läuft nicht mehr</h2>
    <p id="gone-text">Zum Weiterarbeiten das Tool neu starten (Doppelklick auf <code>start.command</code>
      bzw. <code>start.bat</code>) und diese Seite neu laden.</p>
    <div class="ch-foot"><div id="ch-spacer" style="flex:1"></div>
      <button class="dlg-btn" id="gone-reload" onclick="location.reload()">Seite neu laden</button></div>
  </div>
</div>

<script>
const ISO_RE = /^[\dX]{4}-[\dX]{2}-[\dX]{2}$/i;
const $ = id => document.getElementById(id);
const PAGE_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);

let cfg = { root: null, folders: [], yearMin: 0, yearMax: 9999, defaultDate: "", quickIds: {},
            quickDates: [], fileManager: "Dateimanager" };
let allFolders = [], currentFolder = null, images = [], imgIdx = 0, mode = "when";
let finished = null;        // { name, next } – der zuletzt abgeschlossene Ordner
let stopped = false;        // Tool wurde beendet
const undoStack = [];

// Zuletzt verwendete Werte pro Datumsfeld und Sender/Empfänger pro Ordner
const lastValues = {};
let personsByFolder = {};
let suggestion = null;      // Datierung aus einem Dateinamen im Ordner (z. B. DLA_d1926-12-08_s…_e….txt)
let lastFolder = null;      // zuletzt geöffneter Ordner – sein Vorschlag steht schon in den Feldern
let lastDateField = null;

// ---- Server ----
async function api(path, body) {
  const opts = { headers: { "X-Page": PAGE_ID } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { showGone(); throw new Error("Das Tool läuft nicht mehr."); }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// Schließt man den Tab, beendet sich das Tool von selbst.
window.addEventListener("pagehide", () => {
  if (stopped) return;
  navigator.sendBeacon("/api/bye", new Blob([JSON.stringify({ page: PAGE_ID })], { type: "application/json" }));
});
window.addEventListener("pageshow", e => { if (e.persisted) loadState().catch(() => {}); });

function showGone() {
  if (stopped) return;
  $("gone").classList.add("open");
}

function setStatus(msg, isError) {
  const el = $("status-msg");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
}

function imgUrl(folder, file) {
  return `/api/image?folder=${encodeURIComponent(folder)}&file=${encodeURIComponent(file)}`;
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- Ordnerliste ----
function applyState(state) {
  cfg = state;
  Object.assign(names, state.names || {});
  allFolders = state.folders;
  $("root-name").textContent = state.rootName || "Kein Ordner";
  $("root-btn").title = (state.root ? state.root + "\n" : "") + "Klick: anderen Ordner wählen";
  $("btn-original").hidden = !state.hasOriginals;
  $("btn-reveal").textContent = `im ${state.fileManager} zeigen`;
  document.querySelector("#copy-row .hint").replaceChildren("→ ", el("code", "", "_" + state.copyMark));
  $("copy-row").title = `Der Brief ist eine Abschrift: hängt _${state.copyMark} vor die Seitenzahl (Taste A)`;
  renderQuickDates();
  renderPlaceholder();
  filterList();
  updateCountInfo();
}

async function loadState() { applyState(await api("/api/state")); }

function renderPlaceholder() {
  const ph = $("placeholder");
  ph.replaceChildren();
  const button = (label, fn) => {
    const b = el("button", "big-btn", label);
    b.onclick = fn;
    ph.append(el("br"), b);
    return b;
  };
  if (!cfg.root) {
    ph.append("Wo liegen die Bilder?");
    button("Ordner wählen…", openChooser);
  } else if (finished) {
    ph.append("✓ ", el("b", "", finished.name), " ist fertig.");
    if (finished.next) {
      const next = finished.next;
      button(`Weiter mit »${next.name}«`, () => openFolder(next.key)).id = "btn-next-folder";
    } else {
      ph.append(el("br"), "Alle Bilder in diesem Ordner sind benannt.");
      button("Anderen Ordner wählen…", openChooser);
    }
  } else if (!allFolders.length) {
    ph.append("In ", el("b", "", cfg.rootName), " gibt es keine unbenannten Bilder (mehr).");
    button("Anderen Ordner wählen…", openChooser);
  } else {
    ph.append(document.body.classList.contains("nosidebar") ? "« Ordnerliste einblenden und einen Ordner auswählen"
                                                            : "← Ordner auswählen");
  }
}

function updateCountInfo() {
  const n = allFolders.reduce((sum, f) => sum + f.count, 0);
  $("count-info").textContent = allFolders.length
    ? `${n} ${n === 1 ? "Bild" : "Bilder"} offen · ${allFolders.length} Ordner`
    : "";
}

function renderList(list) {
  const box = $("folder-list");
  box.replaceChildren(...list.map(f => {
    const div = el("div", "folder-item" + (currentFolder === f.key ? " active" : ""));
    div.title = f.name;
    div.append(el("span", "", f.self ? f.name + " (direkt)" : f.name), el("span", "badge", f.count));
    div.onclick = () => openFolder(f.key);
    return div;
  }));
}

function filterList() {
  const q = $("search").value.toLowerCase();
  renderList(allFolders.filter(f => f.name.toLowerCase().includes(q)));
}

function folderInfo(key) { return allFolders.find(f => f.key === key); }

function setSidebar(hidden) {
  document.body.classList.toggle("nosidebar", hidden);
  $("btn-side").innerHTML = hidden ? "&raquo;" : "&laquo;";
  $("btn-side").title = hidden ? "Ordnerliste einblenden" : "Ordnerliste ausblenden – mehr Platz fürs Bild";
}

function toggleSidebar() {
  const hidden = !document.body.classList.contains("nosidebar");
  setSidebar(hidden);
  try { localStorage.setItem("nosidebar", hidden ? "1" : ""); } catch (e) {}
  if (!document.body.classList.contains("has-image")) renderPlaceholder();
  applyView();
}

function focusDate() {
  setTimeout(() => {
    if (document.querySelector(".overlay.open")) return;
    const f = document.querySelector("#date-inputs input");
    if (f) { f.focus(); f.select(); }
  }, 30);
}

async function openFolder(key, selectFile) {
  savePersons();
  let data;
  try { data = await api(`/api/images?folder=${encodeURIComponent(key)}`); }
  catch (e) { setStatus("Fehler: " + e.message, true); return; }
  if (!data.images.length) {       // inzwischen außerhalb des Tools geändert
    closeFolder();
    await loadState();
    return;
  }
  currentFolder = key; images = data.images; finished = null; suggestion = data.suggestion;
  imgIdx = Math.max(0, selectFile ? images.indexOf(selectFile) : 0);
  const info = folderInfo(key);
  $("folder-title").textContent = info ? info.name : key;
  $("folder-title").title = $("folder-title").textContent;
  $("panel").classList.remove("off");
  $("btn-submit").disabled = false;
  for (const name in MARKS) setMark(name, false);
  newImage();
  loadPersons();
  // Beim Wechsel in einen Ordner mit Vorschlag steht er in den Feldern; beim erneuten Öffnen
  // desselben Ordners (z. B. nach »Rückgängig«) bleibt, was man eingetragen hat.
  if (suggestion && key !== lastFolder) applySuggestion();
  lastFolder = key;
  filterList();
  showImage();
  updatePreview();
  focusDate();
}

function closeFolder() {
  savePersons();
  currentFolder = null; images = []; suggestion = null;
  renderSuggestion();
  $("folder-title").textContent = "Kein Ordner gewählt";
  $("panel").classList.add("off");
  $("btn-submit").disabled = true;
  $("preview-name").textContent = "";
  $("chips-s").replaceChildren(); $("chips-e").replaceChildren();
  renderNames(true);
  showImage();
  filterList();
}

function showImage() {
  const has = currentFolder !== null && images.length > 0;
  document.body.classList.toggle("has-image", has);
  if (!has) {
    $("img-display").removeAttribute("src");
    $("file-info").textContent = $("img-counter").textContent = "";
    return;
  }
  resetView(true);
  $("img-display").src = imgUrl(currentFolder, images[imgIdx]);
  $("img-counter").textContent = `Bild ${imgIdx + 1} von ${images.length}`;
  $("file-info").textContent = $("file-info").title = images[imgIdx];
  $("btn-other").title = `Kein Brief, nicht datieren: wird zu ${otherName(images[imgIdx])} (Taste O)`;
  // Das nächste Bild schon laden, damit es nach dem Umbenennen sofort da ist.
  if (images.length > 1) new Image().src = imgUrl(currentFolder, images[(imgIdx + 1) % images.length]);
}

function navigate(dir) {
  if (!images.length) return;
  imgIdx = (imgIdx + dir + images.length) % images.length;
  showImage();
  updatePreview();
}

async function revealFolder() {
  if (currentFolder === null) return;
  try { await api("/api/reveal", { folder: currentFolder }); }
  catch (e) { setStatus("Fehler: " + e.message, true); }
}

// ---- Ansicht: Zoom, Verschieben, Drehen ----
let zScale = 1, zTx = 0, zTy = 0, zRot = 0, dragging = false, dragX = 0, dragY = 0;

// Faktor, mit dem das gedrehte Bild wieder ganz ins Fenster passt.
function fitScale() {
  if (zRot % 180 === 0) return 1;
  const img = $("img-display"), v = $("viewer");
  if (!img.offsetWidth || !img.offsetHeight) return 1;
  return Math.min(v.clientWidth / img.offsetHeight, v.clientHeight / img.offsetWidth);
}

function applyView() {
  const img = $("img-display");
  img.style.transform = zScale === 1 && zRot === 0 ? ""
    : `translate(${zTx}px, ${zTy}px) rotate(${zRot}deg) scale(${zScale * fitScale()})`;
  img.classList.toggle("zoomed", zScale > 1);
}

function resetView(all) {
  zScale = 1; zTx = 0; zTy = 0;
  if (all) zRot = 0;
  $("img-display").classList.remove("dragging");
  applyView();
}

// Zoomt so, dass der Punkt (cx, cy) – gemessen ab Bildmitte – an seiner Stelle bleibt.
function zoomAt(factor, cx, cy) {
  const prev = zScale;
  zScale = Math.min(10, Math.max(1, zScale * factor));
  if (zScale === 1) { zTx = 0; zTy = 0; }
  else {
    const ratio = zScale / prev;
    zTx += cx * (1 - ratio); zTy += cy * (1 - ratio);
  }
  applyView();
}

function zoomStep(factor) { zoomAt(factor, -zTx, -zTy); }   // um die Fenstermitte

function rotateView() {
  if (!document.body.classList.contains("has-image")) return;
  zRot = (zRot + 90) % 360;
  zScale = 1; zTx = 0; zTy = 0;
  applyView();
}

(function initView() {
  const viewer = $("viewer"), img = $("img-display");
  const fromCentre = e => {
    const r = img.getBoundingClientRect();
    return [e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2)];
  };

  viewer.addEventListener("wheel", e => {
    if (!document.body.classList.contains("has-image")) return;
    e.preventDefault();
    // Mausrad liefert große, Trackpad viele kleine Schritte; Pinch meldet sich mit ctrlKey.
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    const factor = Math.min(1.6, Math.max(0.625, Math.exp(-dy * (e.ctrlKey ? 0.012 : 0.0022))));
    zoomAt(factor, ...fromCentre(e));
  }, { passive: false });

  viewer.addEventListener("mousedown", e => {
    if (zScale <= 1 || e.target !== img) return;
    dragging = true; dragX = e.clientX; dragY = e.clientY;
    img.classList.add("dragging");
  });
  window.addEventListener("mousemove", e => {
    if (!dragging) return;
    zTx += e.clientX - dragX; zTy += e.clientY - dragY;
    dragX = e.clientX; dragY = e.clientY;
    applyView();
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    img.classList.remove("dragging");
  });

  img.addEventListener("dblclick", e => {
    e.preventDefault();
    if (zScale > 1) { resetView(false); return; }
    zoomAt(3, ...fromCentre(e));
  });
  window.addEventListener("resize", applyView);
})();

// ---- Tastatur ----
function anyOverlay() { return document.querySelector(".overlay.open"); }

// Klicks auf Knöpfe und ins Bild nehmen dem Datumsfeld nicht den Fokus – man kann sofort weitertippen.
document.addEventListener("mousedown", e => {
  if (e.target.closest("#panel button, #viewer")) e.preventDefault();
});

document.addEventListener("keydown", e => {
  if (anyOverlay()) {
    if (e.key === "Escape") { closeOriginal(); if (cfg.root) closeChooser(); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey || currentFolder === null) return;
  const t = e.target;
  const inDate = t.classList.contains("date-field");
  const inPerson = t.classList.contains("person-field") || t.classList.contains("nr-field");
  if (t.tagName === "INPUT" && t.type !== "checkbox" && !inDate && !inPerson) return;   // Suche
  if (t.tagName !== "INPUT" && /^[0-9xX]$/.test(e.key)) {     // lostippen genügt: die Ziffer landet im Datum
    const f = document.querySelector("#date-inputs input");
    if (f) { f.focus(); f.select(); }
    return;
  }

  if (e.key === "Enter") {
    if (t.tagName === "BUTTON") return;          // ein Knopf mit Fokus tut, was draufsteht
    e.preventDefault();
    submitDate();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    navigate(e.key === "ArrowDown" ? 1 : -1);
  } else if (inPerson) {
    return;                                      // dort gehören Buchstaben und ←/→ zum Text
  } else if (!inDate && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
    e.preventDefault();
    navigate(e.key === "ArrowRight" ? 1 : -1);
  } else {
    // Buchstaben außer X sind keine Datumszeichen und deshalb als Kürzel frei.
    const k = e.key.toLowerCase();
    if (k === "s") { e.preventDefault(); swapPersons(); }
    else if (k === "u") { e.preventDefault(); toggleMark("uncertain"); }
    else if (k === "a") { e.preventDefault(); toggleMark("copy"); }
    else if (k === "o") { e.preventDefault(); markOther(); }
    else if (k === "r") { e.preventDefault(); rotateView(); }
  }
});

// ---- Sender / Empfänger ----
function parseIds(s) {
  return [...new Set(s.split(/[^0-9A-Za-z]+/).filter(Boolean))];
}

function savePersons() {
  if (currentFolder === null) return;
  personsByFolder[currentFolder] = { s: $("p-s").value, e: $("p-e").value };
}

function loadPersons() {
  let p = personsByFolder[currentFolder];
  if (!p) {
    // Standard: Ordner-Nummer schreibt an die erste Schnellauswahl (Schnitzler).
    const info = folderInfo(currentFolder);
    const quick = Object.keys(cfg.quickIds)[0] || "";
    const num = info ? info.number : "";
    p = { s: num, e: num === quick ? "" : quick };
  }
  $("p-s").value = p.s; $("p-e").value = p.e;
  renderChips();
  renderNames(true);
}

function chipList() {
  const list = Object.entries(cfg.quickIds).map(([id, name]) => ({ id, label: `${id} ${name}`, tip: name }));
  const info = folderInfo(currentFolder);
  if (info && info.number && !(info.number in cfg.quickIds)) {
    const known = names[info.number];
    list.push({ id: info.number, label: `${info.number} Ordner`,
                tip: (known && known.name ? known.name + " – " : "") + "Nummer aus dem Ordnernamen" });
  }
  return list;
}

function renderChips() {
  for (const side of ["s", "e"]) {
    const ids = currentFolder === null ? [] : parseIds($("p-" + side).value);
    $("chips-" + side).replaceChildren(...(currentFolder === null ? [] : chipList()).map(c => {
      const b = el("button", "chip" + (ids.includes(c.id) ? " on" : ""), c.label);
      b.tabIndex = -1;
      b.title = `${c.tip} – Klick trägt ${c.id} ein oder aus`;
      b.onclick = () => toggleId(side, c.id);
      return b;
    }));
  }
}

function toggleId(side, id) {
  const field = $("p-" + side);
  const ids = parseIds(field.value);
  const i = ids.indexOf(id);
  if (i === -1) ids.push(id); else ids.splice(i, 1);
  field.value = ids.join(" ");
  onPersonInput();
  focusDate();
}

function swapPersons() {
  const s = $("p-s"), e = $("p-e");
  [s.value, e.value] = [e.value, s.value];
  onPersonInput();
}

function onPersonInput() { renderChips(); renderNames(false); updatePreview(); }

// ---- Namen aus der PMB ----
// names[Nummer] = { name, dates } | { missing: true } | { failed: true } | { loading: true }
const names = {};
let namesTimer = null;

function personInfo(id) {
  if (id in cfg.quickIds) return { name: cfg.quickIds[id] };      // z. B. 2121: nie abfragen
  return names[id];
}

// Zeigt unter beiden Feldern, wer hinter den Nummern steckt; Unbekanntes wird nachgeschlagen –
// beim Tippen erst nach einer kurzen Pause, damit nicht jede Zwischenziffer abgefragt wird.
function renderNames(immediate) {
  const unknown = [];
  for (const side of ["s", "e"]) {
    const box = $("names-" + side);
    box.replaceChildren();
    if (currentFolder === null) continue;
    parseIds($("p-" + side).value).forEach((id, i) => {
      const info = personInfo(id);
      if (i) box.append(el("span", "dim", " · "));
      if (info && info.name) {
        box.append(info.name);
        if (info.dates) box.append(el("span", "dim", ` (${info.dates})`));
      } else if (info && info.missing) box.append(el("span", "warn", `${id}: nicht in der PMB`));
      else if (info && info.failed) box.append(el("span", "dim", `${id}: Name nicht abrufbar`));
      else if (cfg.pmbApi && /^\d+$/.test(id)) { box.append(el("span", "dim", `${id} …`)); if (!info) unknown.push(id); }
      else box.append(el("span", "dim", id));
    });
  }
  clearTimeout(namesTimer);
  if (unknown.length) namesTimer = setTimeout(() => unknown.forEach(lookupName), immediate ? 0 : 600);
}

async function lookupName(id) {
  if (names[id]) return;
  names[id] = { loading: true };
  try { names[id] = await api("/api/person?id=" + id); }
  catch (e) { names[id] = { failed: true }; }
  renderNames(true);
  renderChips();
}

// ---- Datum ----
const MODES = ["when", "notBeforeAfter", "fromTo", "undatiert"];
const FIELDS = {
  when: [["d-when", ""]],
  notBeforeAfter: [["d-notBefore", "frühestens <i>notBefore</i>"], ["d-notAfter", "spätestens <i>notAfter</i>"]],
  fromTo: [["d-from", "von <i>from</i>"], ["d-to", "bis <i>to</i>"]],
  undatiert: [],
};
const END_FIELDS = new Set(["d-notAfter", "d-to"]);

function setMode(m) {
  mode = m;
  document.querySelectorAll(".mode-tab").forEach((t, i) => t.classList.toggle("active", MODES[i] === m));
  renderInputs();
  updatePreview();
  focusDate();
}

function renderInputs() {
  const undated = mode === "undatiert";
  $("date-extra").hidden = undated;
  if (undated) {
    // Statt eines Datums: die Nummer, die die Seiten eines undatierten Briefs zusammenhält.
    $("date-inputs").innerHTML = `<div class="field"><label for="undatiert-label">Brief Nr. <i>optional</i></label>
      <input type="text" class="nr-field" id="undatiert-label" placeholder="z. B. 1"
             value="${lastValues["undatiert-label"] ?? ""}" autocomplete="off" spellcheck="false"
             oninput="onLabelInput(event)"></div>`;
    $("date-hint").textContent = "ergibt undatiert bzw. undatiert1 · gleiche Nr. = derselbe Brief";
    return;
  }
  $("date-inputs").innerHTML = FIELDS[mode].map(([id, label]) => {
    const val = lastValues[id] ?? (END_FIELDS.has(id) ? "" : cfg.defaultDate);
    return `<div class="field">${label ? `<label for="${id}">${label}</label>` : ""}
      <input type="text" class="date-field" id="${id}" placeholder="JJJJ-MM-TT" value="${val}"
             inputmode="numeric" autocomplete="off" spellcheck="false"
             oninput="onDateInput(event)" onfocus="lastDateField = this.id"></div>`;
  }).join("");
  $("date-hint").textContent = mode === "when"
    ? "auch nur 1899 oder 1899-12 · X = unlesbar"
    : "beide Daten vollständig · X = unlesbar";
  markQuickDates();
}

function onLabelInput(e) {
  e.target.value = e.target.value.replace(/[^0-9A-Za-z]/g, "");
  lastValues["undatiert-label"] = e.target.value;
  letterChanged();
  updatePreview();
}

// Schnellauswahl: trägt das Datum ins zuletzt benutzte Datumsfeld ein.
function renderQuickDates() {
  $("quick-dates").replaceChildren(...cfg.quickDates.map(d => {
    const b = el("button", "chip mono", d);
    b.tabIndex = -1;
    b.title = "Dieses Datum eintragen";
    b.onclick = () => setQuickDate(d);
    return b;
  }));
  markQuickDates();
}

function markQuickDates() {
  const values = [...document.querySelectorAll("#date-inputs .date-field")].map(f => f.value);
  document.querySelectorAll("#quick-dates .chip").forEach(c => c.classList.toggle("on", values.includes(c.textContent)));
}

function setQuickDate(d) {
  const field = (lastDateField && $(lastDateField)) || document.querySelector("#date-inputs input");
  if (!field) return;
  field.value = d;
  field.classList.remove("invalid");
  lastValues[field.id] = d;
  dateChanged();
  field.focus();
  field.select();
}

// »unsicher« und »Abschrift« gelten für einen Brief: Sie bleiben für die folgenden Seiten stehen
// und fallen weg, sobald man nach dem Umbenennen ein neues Datum (oder eine neue Brief-Nr.)
// eintippt. Was man für das neue Bild selbst an- oder abhakt, bleibt.
const MARKS = { uncertain: "chk-uncertain", copy: "chk-copy" };
let carried = new Set();        // Markierungen, die noch vom vorigen Brief stammen

function setMark(name, on) { $(MARKS[name]).checked = on; }
function isMarked(name) { return $(MARKS[name]).checked; }

function toggleMark(name) {
  setMark(name, !isMarked(name));
  carried.delete(name);
  updatePreview();
}

function newImage() { carried = new Set(Object.keys(MARKS)); }

function letterChanged() {
  carried.forEach(name => setMark(name, false));
  carried.clear();
}

function dateChanged() {
  letterChanged();
  markQuickDates();
  updatePreview();
}

for (const name in MARKS) {
  $(MARKS[name]).addEventListener("change", () => { carried.delete(name); updatePreview(); focusDate(); });
}

// Setzt die Bindestriche: von selbst, sobald Jahr oder Monat voll sind – und sofort, wenn man
// sie tippt. Ein getippter Bindestrich füllt einen einstelligen Monat auf (»1899-1-« → »1899-01-«).
// Beim Ändern mitten im Datum (hyphens = false) zählen nur die Ziffern.
function autoFormat(raw, hyphens) {
  const sizes = [4, 2, 2], groups = [""];
  if (!hyphens) raw = raw.replace(/-/g, "");
  for (const ch of raw.toUpperCase()) {
    const g = groups.length - 1;
    if (/[\dX]/.test(ch)) {
      if (groups[g].length < sizes[g]) groups[g] += ch;
      else if (g < 2) groups.push(ch);
    } else if (ch === "-" && g < 2 && groups[g]) {
      if (groups[g].length < sizes[g]) {
        if (g === 0) continue;                       // ein Jahr hat vier Stellen
        groups[g] = groups[g].padStart(sizes[g], "0");
      }
      groups.push("");
    }
  }
  return groups.join("-");
}

// Ein Bindestrich am Ende ist nur Tipphilfe und gehört nicht zum Datum.
function cleanDate(v) { return v.replace(/-$/, ""); }

function onDateInput(e) {
  const field = e.target;
  // Formatieren, ohne dass die Schreibmarke beim Ändern mitten im Datum ans Ende springt.
  const atEnd = field.selectionStart === field.value.length;
  const before = field.value.slice(0, field.selectionStart).replace(/[^\dXx]/g, "").length;
  const formatted = autoFormat(field.value, atEnd);
  field.value = formatted;
  let pos = formatted.length;
  if (!atEnd) {
    pos = 0;
    for (let seen = 0; pos < formatted.length && seen < before; pos++) if (formatted[pos] !== "-") seen++;
    if (formatted[pos] === "-" && (e.inputType || "").startsWith("insert")) pos++;
  }
  field.setSelectionRange(pos, pos);

  const value = cleanDate(formatted);
  const validator = mode === "when" ? isValidWhen : isValidISO;
  const complete = mode === "when" ? [4, 7, 10].includes(value.length) : value.length === 10;
  field.classList.toggle("invalid", complete && !validator(value));
  lastValues[field.id] = value;
  dateChanged();
}

// Der erlaubte Zeitraum fängt Tippfehler ab – für ein vorgeschlagenes Datum gilt er nicht.
function yearOk(y, s) {
  return (y >= cfg.yearMin && y <= cfg.yearMax) || suggestedDates().has(s.toUpperCase());
}

function isValidWhen(s) {
  if (/^\d{4}$/.test(s)) return yearOk(parseInt(s), s);
  if (/^\d{4}-\d{2}$/.test(s)) {
    const [y, m] = s.split("-").map(Number);
    return yearOk(y, s) && m >= 1 && m <= 12;
  }
  return isValidISO(s);
}

function isValidISO(s) {
  if (!ISO_RE.test(s)) return false;
  const u = s.toUpperCase();
  const yearPart = u.slice(0, 4), monthPart = u.slice(5, 7), dayPart = u.slice(8, 10);
  if (!/X/.test(yearPart) && !yearOk(parseInt(yearPart), s)) return false;
  if (!/X/.test(monthPart)) {
    const m = parseInt(monthPart);
    if (m < 1 || m > 12) return false;
  }
  if (!/X/.test(dayPart)) {
    const d = parseInt(dayPart);
    if (d < 1 || d > 31) return false;
  }
  if (!/X/.test(u)) {
    const [y, m, d] = u.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    return dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d;
  }
  return true;
}

function getDateValues() {
  return Object.fromEntries(FIELDS[mode].map(([id]) => [id.slice(2), cleanDate($(id)?.value || "")]));
}

// Was am Datum noch fehlt – oder null, wenn alles passt.
function dateProblem() {
  if (mode === "undatiert") return null;
  const vals = Object.values(getDateValues());
  const span = `${cfg.yearMin}–${cfg.yearMax}`;
  if (mode === "when") {
    const s = vals[0];
    if (!s) return "Datum eintippen, z. B. 18991231 oder 1899-12-31";
    if (isValidWhen(s)) return null;
    return [4, 7, 10].includes(s.length) ? `Kein gültiges Datum (erlaubt: ${span})` : "Datum unvollständig";
  }
  const [a, b] = vals;
  if (a.length < 10 || b.length < 10) return "Beide Daten vollständig eintragen";
  if (!isValidISO(a) || !isValidISO(b)) return `Kein gültiges Datum (erlaubt: ${span})`;
  if (!/X/i.test(a + b) && a > b) return "Das zweite Datum liegt vor dem ersten";
  return null;
}

function requestBody(extra) {
  return {
    folder: currentFolder, filename: images[imgIdx],
    senders: $("p-s").value, receivers: $("p-e").value, ...extra
  };
}

function dateBody() {
  const copy = isMarked("copy");
  if (mode === "undatiert") return requestBody({ undatiert: true, copy, label: $("undatiert-label")?.value || "" });
  return requestBody({ mode, copy, uncertain: isMarked("uncertain"), ...getDateValues() });
}

// Bild ohne Datum ablegen: IMG_0001.jpg -> IMG_0001_sonstiges.jpg
function otherName(file) {
  const dot = file.lastIndexOf(".");
  return dot > 0 ? `${file.slice(0, dot)}_${cfg.otherMark}${file.slice(dot)}` : `${file}_${cfg.otherMark}`;
}

// Der Server kennt die schon vergebenen Namen und damit die Seitenzahl.
let previewSeq = 0;
async function updatePreview() {
  const pv = $("preview-name");
  const seq = ++previewSeq;
  const hint = text => { pv.className = "hint"; pv.textContent = text; };
  renderSuggestion();
  if (currentFolder === null || !images.length) { pv.className = ""; pv.textContent = ""; return; }
  const problem = dateProblem();
  if (problem) { hint(problem); return; }
  let data;
  try { data = await api("/api/preview", dateBody()); }
  catch (e) { if (seq === previewSeq) hint(e.message); return; }
  if (seq !== previewSeq) return;
  pv.className = "";
  pv.replaceChildren(...data.parts.map(([kind, text]) => el("span", "seg-" + kind, text)));
}

// ---- Vorschlag aus einem Dateinamen im Ordner ----
function suggestedDates() {
  if (!suggestion) return new Set();
  return new Set(["when", "notBefore", "notAfter"].map(k => suggestion[k]).filter(Boolean));
}

function suggestionHasPersons() { return !!(suggestion.senders || suggestion.receivers); }

// Trägt Datum, Sender, Empfänger und Markierungen aus dem Vorschlag ein.
function applySuggestion() {
  const sg = suggestion;
  if (!sg || currentFolder === null) return;
  FIELDS[sg.mode].forEach(([id]) => { lastValues[id] = sg[id.slice(2)]; });
  if (suggestionHasPersons()) { $("p-s").value = sg.senders; $("p-e").value = sg.receivers; }
  setMark("uncertain", sg.uncertain);
  setMark("copy", sg.copy);
  newImage();                  // wie ein Brief: ein neues Datum nimmt die Markierungen wieder weg
  renderChips();
  renderNames(true);
  setMode(sg.mode);            // zeichnet die Felder neu und aktualisiert die Vorschau
}

function suggestionMatches() {
  const sg = suggestion;
  if (!sg || mode !== sg.mode) return false;
  const vals = getDateValues();
  if (!FIELDS[sg.mode].every(([id]) => vals[id.slice(2)].toUpperCase() === sg[id.slice(2)])) return false;
  const same = (a, b) => parseIds(a).join(" ") === parseIds(b).join(" ");
  if (suggestionHasPersons() && !(same($("p-s").value, sg.senders) && same($("p-e").value, sg.receivers))) return false;
  return isMarked("uncertain") === sg.uncertain && isMarked("copy") === sg.copy;
}

function renderSuggestion() {
  const box = $("suggest"), sg = suggestion;
  box.hidden = !sg || currentFolder === null;
  if (box.hidden) return;
  const matches = suggestionMatches();
  box.classList.toggle("changed", !matches);
  $("suggest-apply").hidden = matches;
  const more = sg.sources.length - 1;
  $("suggest-text").replaceChildren(matches ? "✓ Vorschlag aus " : "Vorschlag aus ",
                                    el("code", "", sg.sources[0]), more ? ` (+${more})` : "");
  box.title = (more ? `${sg.sources.length} Dateien mit derselben Datierung:\n` : "Datierung aus dem Dateinamen:\n")
    + sg.sources.join("\n") + (matches ? "\n\n↵ übernimmt den Vorschlag." : "");
}

// ---- Umbenennen ----
let busy = false;
async function rename(body) {
  if (busy || currentFolder === null || !images.length) return;
  busy = true;
  let data;
  try { data = await api("/api/rename", body); }
  catch (e) { setStatus("Fehler: " + e.message, true); return; }
  finally { busy = false; }
  undoStack.push({ folder: currentFolder, old: data.old, new: data.new });
  updateUndoButton();
  setStatus(`✓ ${data.old} → ${data.new}`);
  afterMove();
}

function submitDate() {
  if (currentFolder === null || !images.length) return;
  const problem = dateProblem();
  if (problem) { setStatus(problem + ".", true); return; }
  rename(dateBody());
}

function markOther() {
  if (currentFolder === null || !images.length) return;
  rename(requestBody({ other: true }));
}

function updateUndoButton() {
  const last = undoStack[undoStack.length - 1];
  $("btn-undo").disabled = !last;
  $("btn-undo").title = last ? `${last.new} heißt dann wieder ${last.old}` : "";
}

async function undo() {
  const a = undoStack[undoStack.length - 1];
  if (!a) return;
  try { await api("/api/undo", a); }
  catch (e) { setStatus("Fehler: " + e.message, true); return; }
  undoStack.pop();
  updateUndoButton();
  setStatus(`↩ ${a.new} heißt wieder ${a.old}`);
  finished = null;
  await loadState();
  if (folderInfo(a.folder)) await openFolder(a.folder, a.old);
}

function afterMove() {
  const fi = allFolders.findIndex(f => f.key === currentFolder);
  images.splice(imgIdx, 1);
  newImage();
  if (fi !== -1) allFolders[fi].count = images.length;
  if (!images.length) {
    const name = fi !== -1 ? allFolders[fi].name : currentFolder;
    if (fi !== -1) allFolders.splice(fi, 1);
    closeFolder();
    finished = { name, next: allFolders[fi] || allFolders[0] || null };
    renderPlaceholder();
    const next = $("btn-next-folder");
    if (next) next.focus();
  } else {
    if (imgIdx >= images.length) imgIdx = 0;     // am Ende: zurück zu den übersprungenen
    showImage();
    updatePreview();
    focusDate();
  }
  updateCountInfo();
  filterList();
}

function skipImage() {
  if (!images.length) return;
  setStatus("");
  navigate(1);
  focusDate();
}

// ---- Ordner wählen ----
let chooser = null;
const FOLDER_ICON = '<svg viewBox="0 0 16 16"><path d="M1.5 3.5a1 1 0 0 1 1-1h3.2l1.5 1.6h6.3a1 1 0 0 1 1 1v7.4a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"/></svg>';

async function openChooser() {
  $("chooser").classList.add("open");
  $("ch-cancel").hidden = !cfg.root;
  await browse("");
}

function closeChooser() {
  $("chooser").classList.remove("open");
  focusDate();
}

async function browse(path) {
  const summary = $("ch-summary");
  let d;
  try { d = await api("/api/browse?path=" + encodeURIComponent(path)); }
  catch (e) { summary.className = "error"; summary.textContent = e.message; return; }
  chooser = d;
  const pathField = $("ch-path");
  pathField.value = d.path;
  pathField.scrollLeft = pathField.scrollWidth;          // das Ende des Pfads zeigen
  $("ch-up").disabled = !d.parent;
  const name = d.path.split(/[\\/]/).filter(Boolean).pop() || d.path;
  $("ch-ok").textContent = `»${name}« verwenden`;

  const row = (title, items) => {
    const div = el("div");
    div.append(el("span", "caps", title));
    items.forEach(([label, p]) => {
      const b = el("button", "chip" + (p === d.path ? " on" : ""), label);
      b.title = p;
      b.onclick = () => browse(p);
      div.append(b);
    });
    return div;
  };
  const rows = [row("Orte", d.places.map(p => [p.label, p.path]))];
  if (d.recent.length) rows.push(row("Zuletzt", d.recent.map(p => [p.split(/[\\/]/).filter(Boolean).pop() || p, p])));
  $("ch-places").replaceChildren(...rows);

  const list = $("ch-list");
  list.scrollTop = 0;
  if (!d.dirs.length) {
    list.replaceChildren(el("div", "ch-empty", d.error || "Keine Unterordner."));
  } else {
    list.replaceChildren(...d.dirs.map(sub => {
      const item = el("div", "ch-item");
      item.innerHTML = FOLDER_ICON;
      item.append(el("span", "", sub.name));
      if (sub.images) item.append(el("span", "ch-count", `${sub.images} ${sub.images === 1 ? "Bild" : "Bilder"}`));
      item.onclick = () => browse(sub.path);
      return item;
    }));
  }

  const withImages = d.dirs.filter(sub => sub.images).length;
  const unknown = d.dirs.some(sub => sub.images === null);
  const bits = [];
  if (d.images) bits.push(`${d.images} ${d.images === 1 ? "Bild" : "Bilder"} direkt`);
  if (withImages) bits.push(`${withImages} Unterordner mit Bildern`);
  summary.className = "";
  summary.textContent = !d.usable ? "Bitte in den Ordner mit den Bildern hineingehen."
    : bits.length ? "Hier: " + bits.join(" und ") + "."
    : unknown ? "" : "Hier liegen keine Bilder – weiter hineingehen.";
  $("ch-ok").disabled = !d.usable || (!bits.length && !unknown);
}

function chooserUp() { if (chooser && chooser.parent) browse(chooser.parent); }

async function chooseCurrent() {
  if (!chooser) return;
  let state;
  try { state = await api("/api/root", { path: chooser.path }); }
  catch (e) { $("ch-summary").className = "error"; $("ch-summary").textContent = e.message; return; }
  closeFolder();
  undoStack.length = 0; updateUndoButton();     // gehören zum alten Ordner
  personsByFolder = {};
  lastFolder = null;
  finished = null;
  setStatus("");
  applyState(state);
  $("chooser").classList.remove("open");
  if (allFolders.length === 1) openFolder(allFolders[0].key);
}

// ---- Unbeschnittenes Original ----
let pendingOrig = null;

async function useOriginal() {
  if (currentFolder === null || !images.length) return;
  const filename = images[imgIdx];
  const loading = $("orig-loading"), oimg = $("orig-img"), title = $("orig-title"), yes = $("orig-yes");

  $("orig-overlay").classList.add("open");
  oimg.hidden = true; yes.hidden = true; loading.hidden = false;
  loading.textContent = "Suche Original…";
  title.textContent = filename;
  pendingOrig = null;

  let data;
  try { data = await api(`/api/original-info?file=${encodeURIComponent(filename)}`); }
  catch (e) { loading.textContent = "Fehler: " + e.message; return; }
  if (!data.found) {
    loading.textContent = data.error || "Original nicht gefunden.";
    return;
  }
  pendingOrig = { filename };
  title.textContent = `${filename}  →  ${data.orig_name}`;
  oimg.onload = () => { loading.hidden = true; oimg.hidden = false; yes.hidden = false; };
  oimg.onerror = () => { loading.textContent = "Vorschau konnte nicht geladen werden."; };
  oimg.src = `/api/original-preview?num=${data.orig_num}`;
}

function closeOriginal() {
  if (!$("orig-overlay").classList.contains("open")) return;
  $("orig-overlay").classList.remove("open");
  pendingOrig = null;
  focusDate();
}

async function confirmOriginal() {
  if (!pendingOrig) return;
  const { filename } = pendingOrig;
  let data;
  try { data = await api("/api/use-original", { folder: currentFolder, filename }); }
  catch (e) {
    $("orig-loading").hidden = false;
    $("orig-loading").textContent = "Fehler: " + e.message;
    $("orig-img").hidden = true; $("orig-yes").hidden = true;
    return;
  }
  // Ausschnitt an Ort und Stelle durch das Original ersetzen.
  const i = images.indexOf(filename);
  if (i !== -1) images[i] = data.new_filename;
  closeOriginal();
  setStatus(`✓ Original übernommen: ${data.new_filename}`);
  showImage();
  updatePreview();
}

async function shutdown() {
  if (!confirm("Tool wirklich beenden?")) return;
  try { await api("/api/shutdown", {}); } catch (e) { return; }
  stopped = true;
  $("gone-title").textContent = "Beendet";
  $("gone-text").textContent = "Dieser Tab kann geschlossen werden.";
  $("gone-reload").hidden = true;
  $("gone").classList.add("open");
}

// ---- Start ----
(async function init() {
  let hidden = false;
  try { hidden = !!localStorage.getItem("nosidebar"); } catch (e) {}
  setSidebar(hidden);
  await loadState();
  renderInputs();
  if (!cfg.root) openChooser();
  else if (allFolders.length === 1) openFolder(allFolders[0].key);
})();
</script>
</body>
</html>
"""


def make_server(port: int) -> ThreadingHTTPServer:
    """Nimmt den nächsten freien Port ab `port`."""
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            continue
    raise SystemExit(f"Kein freier Port zwischen {port} und {port + 19}.")


def main() -> None:
    global SERVER
    parser = argparse.ArgumentParser(description="Korrespondenz-Scans im Dateinamen datieren.")
    parser.add_argument("ordner", nargs="?", help="Ordner mit Bildern oder mit Korrespondenz-Ordnern")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht öffnen")
    args = parser.parse_args()

    start = args.ordner or load_config().get("root")
    if start:
        try:
            set_root(start)
        except ApiError as e:
            if args.ordner:
                raise SystemExit(str(e))

    SERVER = make_server(args.port)
    url = f"http://localhost:{SERVER.server_address[1]}"
    print(f"Datierungstool läuft auf {url}")
    print("Beenden: Browser-Tab schließen, »Beenden« klicken oder hier Strg+C drücken.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    SERVER.server_close()


if __name__ == "__main__":
    main()
