#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
War Thunder — Old Build Downloader
==================================

Удобная графическая обёртка для скачивания исторических сборок War Thunder
из Steam через DepotDownloader или steamctl.

Ключевая идея:
  * приложение хранит каталог «депо + manifest id + дата» (manifests.json);
  * manifest id берутся со SteamDB (depot/<id>/manifests/) и вставляются
    через диалог «Импорт из SteamDB»;
  * скачивание запускается одной кнопкой, лог и QR-код видны прямо в окне.

Зависимости: только стандартная библиотека Python (tkinter).
Внешние инструменты (любой на выбор):
  * DepotDownloader.exe  — https://github.com/SteamRE/DepotDownloader/releases
  * steamctl             — pip install steamctl

Запуск:  python wt_old_build.py     (или run.bat)
Проверка: python wt_old_build.py --selftest
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------

APP_ID = 236390
APP_NAME = "War Thunder"
STEAMDB_APP_URL = f"https://steamdb.info/app/{APP_ID}/depots/"
STEAMDB_DEPOT_URL = "https://steamdb.info/depot/{depot}/manifests/"

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "manifests.json"
SETTINGS_PATH = HERE / "settings.json"

# Известные депо War Thunder. Названия — подсказка, id можно добавлять любые.
DEPOT_NAMES = {
    "236394": "War Thunder Win — launcher / binaries",
    "236391": "War Thunder Content — aces, content",
    "236393": "War Thunder Linux",
    "236392": "War Thunder macOS",
    "228990": "DirectX Jun 2010 Redist (shared)",
}

# Стартовый каталог: единственный подтверждённый пользователем манифест.
DEFAULT_CATALOG = {
    "version": 1,
    "entries": [
        {
            "depot": "236391",
            "manifest": "3264770838696250786",
            "date": "2013-08-15 12:13:36",
            "note": "Steam release — content",
        }
    ],
}

DEFAULT_SETTINGS = {
    "output_dir": r"C:\WT_old",
    "backend": "depotdownloader",
    "dd_path": "",
    "steamctl_path": "",
    "threads": 16,
    "use_qr": True,
    "remember_password": True,
    "app_id": APP_ID,
    "steamctl_user": "",
    "auto_open_steamdb": True,
}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}

MANIFEST_RE = re.compile(r"\b(\d{15,20})\b")
LONG_DATE_RE = re.compile(
    r"(\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4}"
    r"\s*[–\-—]?\s*\d{1,2}:\d{2}:\d{2}\s*UTC)",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)")


# --------------------------------------------------------------------------
# Утилиты
# --------------------------------------------------------------------------

def deep_default(value):
    """Глубокая копия значения по умолчанию (без внешних зависимостей)."""
    return json.loads(json.dumps(value))


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return deep_default(default)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings() -> dict:
    data = load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return deep_default(DEFAULT_SETTINGS)
    result = deep_default(DEFAULT_SETTINGS)
    result.update({k: v for k, v in data.items() if k in result or k == "auto_open_steamdb"})
    return result


def load_catalog() -> list:
    """Загружает каталог, отбрасывает мусор и дубликаты."""
    data = load_json(CATALOG_PATH, {"version": 1, "entries": []})
    if isinstance(data, dict):
        raw = data.get("entries", [])
    elif isinstance(data, list):
        raw = data
    else:
        raw = []

    entries = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        depot = str(item.get("depot", "")).strip()
        manifest = str(item.get("manifest", "")).strip()
        if not depot.isdigit() or not re.fullmatch(r"\d{15,20}", manifest):
            continue
        key = (depot, manifest)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "depot": depot,
                "manifest": manifest,
                "date": normalize_date(str(item.get("date", ""))),
                "note": str(item.get("note", "")),
            }
        )

    if not entries:
        entries = [dict(item) for item in DEFAULT_CATALOG["entries"]]
    return entries


def extract_manifest(text: str) -> str:
    matches = MANIFEST_RE.findall(text or "")
    return matches[0] if matches else ""


def extract_depot_from_text(text: str) -> str:
    match = re.search(r"steamdb\.info/depot/(\d+)", text or "")
    if not match:
        match = re.search(r"\bdepot[^0-9]{0,6}(\d{5,8})\b", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def load_steamdb_page_text(path) -> str:
    """Читает сохранённую страницу SteamDB: HTML или HAR-дамп запросов."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() != ".har":
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return ""
    best = ""
    for entry in data.get("log", {}).get("entries", []):
        response = entry.get("response", {})
        mime = response.get("content", {}).get("mimeType", "")
        body = response.get("content", {}).get("text", "")
        if not body or "html" not in mime or "changeid=M:" not in body:
            continue
        if len(body) > len(best):
            best = body
    return best


def parse_steamdb_html(text: str):
    """Разбирает сохранённую страницу SteamDB depot/<id>/manifests/."""
    results = []
    seen = set()
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text or "", re.I | re.S):
        m_time = re.search(r'data-time="([^"]+)"', row, re.I)
        m_id = re.search(r"changeid=M:(\d{15,20})", row, re.I)
        if not (m_time and m_id):
            continue
        manifest = m_id.group(1)
        if manifest in seen:
            continue
        seen.add(manifest)
        m_depot = re.search(r"/depot/(\d+)/history/", row, re.I)
        results.append(
            {
                "depot": m_depot.group(1) if m_depot else "",
                "manifest": manifest,
                "date": normalize_date(m_time.group(1)[:19].replace("T", " ")),
                "note": "",
            }
        )
    return results


def parse_steamctl_info(text: str) -> str:
    """Достаёт дату создания манифеста из вывода `steamctl depot info`."""
    match = re.search(r"Created\s+On:\s*(.+)", text or "", re.IGNORECASE)
    if not match:
        return ""
    return normalize_date(match.group(1).strip())


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def decode_console(raw: bytes) -> str:
    # .NET может писать в OEM-кодировке, python-инструменты — в UTF-8.
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def normalize_date(value: str) -> str:
    """Приводит дату SteamDB к виду 'YYYY-MM-DD HH:MM:SS' (или '')."""
    if not value:
        return ""
    value = value.strip()

    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*[–\-—]?\s*(\d{1,2}:\d{2}:\d{2})?",
        value,
    )
    if m:
        day, month, year, time_part = m.groups()
        number = MONTH_NUM.get(month.lower())
        if number:
            return f"{year}-{number:02d}-{int(day):02d} {time_part or '00:00:00'}"

    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?", value)
    if m:
        return f"{m.group(1)} {m.group(2) or '00:00:00'}"

    return value


def parse_steamdb_text(text: str):
    """
    Разбирает вставленный текст со страницы SteamDB
    (таблица 'Previously seen manifests') и возвращает список
    {'manifest', 'date', 'note'}.
    """
    results = []
    seen = set()
    last_date = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        date_match = LONG_DATE_RE.search(line) or ISO_DATE_RE.search(line)
        if date_match:
            last_date = normalize_date(date_match.group(1))

        # В таблице SteamDB manifest идёт после даты в той же строке.
        for manifest in MANIFEST_RE.findall(line):
            if manifest in seen:
                continue
            seen.add(manifest)
            results.append(
                {
                    "manifest": manifest,
                    "date": last_date,
                    "note": "",
                }
            )

    return results


def build_depotdownloader_cmd(
    exe: str,
    app_id: int,
    depot: str,
    manifest: str,
    out_dir: str,
    threads: int = 16,
    qr: bool = True,
    remember_password: bool = True,
    user: str = "",
):
    cmd = [
        exe,
        "-app", str(app_id),
        "-depot", str(depot),
        "-manifest", str(manifest),
        "-dir", str(out_dir),
        "-max-downloads", str(threads),
    ]
    user = (user or "").strip()
    if user:
        # после успешного QR-входа DepotDownloader сохраняет refresh token —
        # вход по -username + -remember-password надёжнее повторного QR
        cmd += ["-username", user]
    elif qr:
        cmd.append("-qr")
    if remember_password:
        cmd.append("-remember-password")
    return cmd


def build_steamctl_cmd(
    exe: str,
    app_id: int,
    depot: str,
    manifest: str,
    out_dir: str,
    user: str = "",
):
    cmd = command_prefix(exe)
    if user.strip():
        cmd += ["--user", user.strip()]
    cmd += [
        "depot", "download",
        "--app", str(app_id),
        "--depot", str(depot),
        "--manifest", str(manifest),
        "-o", str(out_dir),
        "-np",
    ]
    return cmd


def depot_label(depot: str) -> str:
    return DEPOT_NAMES.get(str(depot), "неизвестный депо")


def depot_choices():
    ordered = sorted(DEPOT_NAMES.keys(), key=lambda x: int(x))
    return [f"{d} — {DEPOT_NAMES[d]}" for d in ordered]


def depot_from_choice(value: str) -> str:
    return value.strip().split()[0] if value.strip() else ""


def find_steamctl() -> str:
    local = HERE / "tools" / "steamctl" / "steamctl.cmd"
    if local.is_file():
        return str(local)
    found = shutil.which("steamctl") or shutil.which("steamctl.exe")
    if found:
        return found
    scripts = Path(sys.executable).parent / "Scripts" / "steamctl.exe"
    if scripts.is_file():
        return str(scripts)
    return ""


def command_prefix(exe: str) -> list:
    """Для .cmd-обёрток нужен cmd.exe, иначе Windows не запустит файл."""
    if os.name == "nt" and str(exe).lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", str(exe)]
    return [str(exe)]


def find_depotdownloader() -> str:
    user = os.environ.get("USERNAME", "")
    candidates = [
        HERE / "DepotDownloader.exe",
        HERE / "tools" / "DepotDownloader" / "DepotDownloader.exe",
        Path(r"C:\Users") / user / r"AppData\Local\Temp\dd\DepotDownloader.exe",
        Path(r"C:\Users") / user / r"Downloads\DepotDownloader.exe",
        Path(r"C:\Tools") / "DepotDownloader.exe",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            pass
    return shutil.which("DepotDownloader") or ""


# --------------------------------------------------------------------------
# Диалог добавления манифеста
# --------------------------------------------------------------------------

class ManifestDialog(tk.Toplevel):
    """Диалог «укажи ID версии»: объясняет, где взять manifest на SteamDB,
    открывает страницу и принимает как чистый ID, так и строку из таблицы."""

    def __init__(
        self,
        master,
        depot_default: str = "236391",
        auto_open: bool = True,
        app_id: int = APP_ID,
        steamctl_exe: str = "",
        steamctl_user: str = "",
    ):
        super().__init__(master)
        self.title("Указать ID версии со SteamDB")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.app_id = app_id
        self.steamctl_exe = steamctl_exe
        self.steamctl_user = steamctl_user
        self._lookup_result = None
        self._lookup_running = False

        self.depot_var = tk.StringVar(
            value=f"{depot_default} — {depot_label(depot_default)}"
        )
        self.manifest_var = tk.StringVar()
        self.date_var = tk.StringVar(value="")
        self.note_var = tk.StringVar()
        self.auto_open_var = tk.BooleanVar(value=auto_open)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Как получить ID версии:",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        steps = ttk.Label(
            frame,
            justify="left",
            foreground="#333",
            text=(
                "1. Нажмите «Открыть SteamDB» — откроется страница манифестов выбранного депо.\n"
                "2. Если SteamDB просит — войдите в Steam (логин и пароль идут напрямую на сайт).\n"
                "3. Прокрутите до таблицы «Previously seen manifests» и найдите нужную дату.\n"
                "4. Скопируйте строку или сам ManifestID (15–20 цифр) и вставьте в поле ниже."
            ),
        )
        steps.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 10))

        ttk.Label(frame, text="Депо:").grid(row=2, column=0, sticky="w", pady=4)
        depot_row = ttk.Frame(frame)
        depot_row.grid(row=2, column=1, sticky="ew", pady=4)
        depot_row.columnconfigure(0, weight=1)
        self.depot_box = ttk.Combobox(
            depot_row, textvariable=self.depot_var, values=depot_choices()
        )
        self.depot_box.grid(row=0, column=0, sticky="ew")
        ttk.Button(depot_row, text="Открыть SteamDB", command=self._open_steamdb).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Label(
            frame,
            text="Можно вписать свой Depot ID, например 236394 (launcher)",
            foreground="#666",
        ).grid(row=3, column=1, sticky="w")

        ttk.Label(frame, text="Manifest ID:").grid(row=4, column=0, sticky="nw", pady=(8, 4))
        manifest_row = ttk.Frame(frame)
        manifest_row.grid(row=4, column=1, sticky="ew", pady=(8, 4))
        manifest_row.columnconfigure(0, weight=1)
        self.manifest_entry = ttk.Entry(manifest_row, textvariable=self.manifest_var)
        self.manifest_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            manifest_row, text="Вставить из буфера", command=self._paste_clipboard
        ).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(
            frame,
            text="Можно вставить всю строку из таблицы — ID и дата определятся сами.",
            foreground="#666",
        ).grid(row=5, column=1, sticky="w")

        ttk.Label(frame, text="Дата (необязательно):").grid(
            row=6, column=0, sticky="nw", pady=4
        )
        date_wrap = ttk.Frame(frame)
        date_wrap.grid(row=6, column=1, sticky="ew", pady=4)
        date_wrap.columnconfigure(0, weight=1)
        ttk.Entry(date_wrap, textvariable=self.date_var).grid(
            row=0, column=0, sticky="ew"
        )
        self.lookup_button = ttk.Button(
            date_wrap, text="Узнать дату по ID", command=self._lookup_date
        )
        self.lookup_button.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(
            date_wrap,
            text=(
                "необязательно; можно узнать у Steam по Manifest ID "
                "(нужен вход в steamctl)"
            ),
            foreground="#666",
        ).grid(row=1, column=0, columnspan=2, sticky="w")

        ttk.Label(frame, text="Заметка:").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.note_var).grid(
            row=7, column=1, sticky="ew", pady=4
        )

        ttk.Checkbutton(
            frame,
            text="Открывать SteamDB сразу при открытии этого окна",
            variable=self.auto_open_var,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.status = ttk.Label(
            frame, text="", foreground="#1b5e20", wraplength=720, justify="left"
        )
        self.status.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="Добавить", command=self._ok).pack(side="right")

        self.bind("<Escape>", lambda _e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.grab_set()
        self._center(master)
        try:
            self.manifest_entry.focus_set()
        except tk.TclError:
            pass

        if auto_open:
            self.after(200, self._open_steamdb)

    def _center(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _on_close(self):
        if getattr(self, "_lookup_running", False):
            self.stop_lookup()
        master = self.master
        if getattr(master, "_active_dialog", None) is self:
            master._active_dialog = None
            try:
                master._update_cancel_state()
            except AttributeError:
                pass
        self.destroy()

    def _open_steamdb(self):
        depot = depot_from_choice(self.depot_var.get())
        if not depot.isdigit():
            messagebox.showerror("Ошибка", "Сначала укажите Depot ID.", parent=self)
            return
        webbrowser.open(STEAMDB_DEPOT_URL.format(depot=depot))

    def _paste_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        if not text:
            self.status.configure(text="Буфер обмена пуст.", foreground="#b00020")
            return
        self._apply_pasted(text)

    def _apply_pasted(self, text: str):
        depot = extract_depot_from_text(text)
        if depot:
            self.depot_var.set(f"{depot} — {depot_label(depot)}")

        parsed = parse_steamdb_text(text)
        manifest = extract_manifest(text)
        if not manifest:
            self.status.configure(
                text="ID не найден во вставленном тексте.", foreground="#b00020"
            )
            return
        self.manifest_var.set(manifest)
        date = parsed[0]["date"] if parsed else ""
        if date:
            self.date_var.set(date)
        extra = ""
        if len(parsed) > 1:
            extra = f" (в тексте ещё {len(parsed) - 1} ID — для пачки используйте «Импорт со SteamDB»)"
        self.status.configure(
            text=f"Распознан manifest {manifest}" + (f", дата {date}" if date else "") + extra,
            foreground="#1b5e20",
        )

    def _lookup_date(self):
        """Спрашиваем дату создания напрямую у Steam через steamctl depot info."""
        if self._lookup_running:
            return
        depot = depot_from_choice(self.depot_var.get())
        manifest = extract_manifest(self.manifest_var.get().strip())
        if not depot.isdigit() or not re.fullmatch(r"\d{15,20}", manifest):
            messagebox.showerror(
                "Ошибка", "Сначала укажите корректные Depot ID и Manifest ID.", parent=self
            )
            return

        exe = self.steamctl_exe or find_steamctl()
        if not exe:
            messagebox.showerror(
                "steamctl не найден",
                "Установите steamctl (pip install steamctl) или укажите путь в настройках.",
                parent=self,
            )
            return

        self._lookup_running = True
        self._lookup_result = None
        self._lookup_proc = None
        master = self.master
        master._active_dialog = self
        master._update_cancel_state()
        self.lookup_button.state(["disabled"])
        self.status.configure(text="Запрашиваю дату у Steam…", foreground="#666")
        threading.Thread(
            target=self._lookup_worker,
            args=(exe, depot, manifest),
            daemon=True,
        ).start()
        self.after(150, self._check_lookup)

    def _lookup_worker(self, exe: str, depot: str, manifest: str):
        cmd = command_prefix(exe)
        if self.steamctl_user:
            cmd += ["--user", self.steamctl_user]
        cmd += [
            "depot", "info",
            "--app", str(self.app_id),
            "--depot", depot,
            "--manifest", manifest,
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=flags,
            )
            self._lookup_proc = proc
            try:
                out_bytes, _ = proc.communicate(timeout=90)
                output = decode_console(out_bytes or b"")
            except subprocess.TimeoutExpired:
                self._kill_lookup_proc()
                output = "TIMEOUT: Steam не ответил за 90 секунд."
            finally:
                self._lookup_proc = None
        except FileNotFoundError:
            output = f"Файл не найден: {exe}"
        except OSError as exc:
            output = str(exc)

        date = parse_steamctl_info(output)
        if date:
            self._lookup_result = ("ok", date)
        else:
            lines = []
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                if any(
                    marker in line
                    for marker in (
                        "SyntaxWarning", "DeprecationWarning", "RuntimeWarning",
                        "warnings.warn", "invalid escape sequence",
                    )
                ):
                    continue
                lines.append(line)
            if any(
                marker in output
                for marker in ("Enter Steam login", "Username:", "EOFError")
            ):
                message = (
                    "Нужен вход в steamctl: нажмите «Вход…» справа в настройках "
                    "и введите логин/пароль Steam один раз."
                )
            else:
                message = " | ".join(lines[-3:])[:300] or "пустой ответ от steamctl"
            self._lookup_result = ("error", message)

    def _kill_lookup_proc(self):
        proc = getattr(self, "_lookup_proc", None)
        if proc is None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop_lookup(self):
        self._kill_lookup_proc()
        self._lookup_result = ("stopped", "")

    def _check_lookup(self):
        if self._lookup_result is None:
            self.after(150, self._check_lookup)
            return
        status, value = self._lookup_result
        self._lookup_running = False
        self._lookup_proc = None
        master = self.master
        if getattr(master, "_active_dialog", None) is self:
            master._active_dialog = None
        try:
            master._update_cancel_state()
        except AttributeError:
            pass
        try:
            self.lookup_button.state(["!disabled"])
        except tk.TclError:
            pass
        if status == "ok":
            self.date_var.set(value)
            self.status.configure(text=f"Дата получена из Steam: {value}", foreground="#1b5e20")
        elif status == "stopped":
            self.status.configure(text="Запрос даты остановлен.", foreground="#666")
        else:
            self.status.configure(
                text=f"Не удалось узнать дату: {value}", foreground="#b00020"
            )

    def _ok(self):
        depot = depot_from_choice(self.depot_var.get())
        raw = self.manifest_var.get().strip()
        manifest = extract_manifest(raw) or raw
        if not depot.isdigit():
            messagebox.showerror("Ошибка", "Depot ID должен состоять из цифр.", parent=self)
            return
        if not re.fullmatch(r"\d{15,20}", manifest):
            messagebox.showerror(
                "Ошибка",
                "Manifest ID должен состоять из 15–20 цифр (или вставьте строку со SteamDB).",
                parent=self,
            )
            return
        self.result = {
            "depot": depot,
            "manifest": manifest,
            "date": normalize_date(self.date_var.get().strip()),
            "note": self.note_var.get().strip(),
            "auto_open": bool(self.auto_open_var.get()),
        }
        self._on_close()


# --------------------------------------------------------------------------
# Диалог импорта со SteamDB
# --------------------------------------------------------------------------

class ImportDialog(tk.Toplevel):
    def __init__(self, master, depot_default: str = "236391", auto_open: bool = True):
        super().__init__(master)
        self.title("Импорт манифестов со SteamDB")
        self.geometry("760x580")
        self.transient(master)
        self.result = None

        self.depot_var = tk.StringVar(
            value=f"{depot_default} — {depot_label(depot_default)}"
        )
        self.auto_open_var = tk.BooleanVar(value=auto_open)
        self._parsed = []

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(
            outer,
            text=(
                "1. Откройте страницу депо на SteamDB. Если попросят — войдите в Steam.\n"
                "   Затем прокрутите вниз до «Previously seen manifests» — там даты и ID версий."
            ),
            justify="left",
            foreground="#333",
        ).grid(row=0, column=0, sticky="w")

        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        ttk.Label(top, text="Депо:").pack(side="left")
        ttk.Combobox(
            top, textvariable=self.depot_var, values=depot_choices(), width=48
        ).pack(side="left", padx=(6, 6))
        ttk.Button(top, text="Открыть SteamDB", command=self._open_steamdb).pack(
            side="left"
        )

        ttk.Checkbutton(
            outer,
            text="Открывать SteamDB сразу при открытии этого окна",
            variable=self.auto_open_var,
        ).grid(row=2, column=0, sticky="w", pady=(0, 8))

        ttk.Label(
            outer,
            text="2. Скопируйте таблицу манифестов (или просто строки с ID) и вставьте сюда:",
            foreground="#333",
        ).grid(row=3, column=0, sticky="w")

        text_frame = ttk.Frame(outer)
        text_frame.grid(row=4, column=0, sticky="nsew", pady=(4, 8))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, wrap="none", height=10, undo=True)
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        bottom = ttk.Frame(outer)
        bottom.grid(row=5, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self.status = ttk.Label(bottom, text="Пока ничего не разобрано.", foreground="#666")
        self.status.grid(row=0, column=0, sticky="w")

        ttk.Button(bottom, text="Из HTML-файла…", command=self._load_html).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(bottom, text="Разобрать", command=self._parse).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Button(bottom, text="Отмена", command=self.destroy).grid(
            row=0, column=3, padx=(6, 0)
        )
        self.add_button = ttk.Button(bottom, text="Добавить", command=self._add)
        self.add_button.grid(row=0, column=4, padx=(6, 0))
        self.add_button.state(["disabled"])

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self._center(master)
        self.text.focus_set()

        if auto_open:
            self.after(200, self._open_steamdb)

    def _center(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _open_steamdb(self):
        depot = depot_from_choice(self.depot_var.get())
        webbrowser.open(STEAMDB_DEPOT_URL.format(depot=depot))

    def _load_html(self):
        path = filedialog.askopenfilename(
            title="Сохранённая страница SteamDB",
            filetypes=[
                ("SteamDB HTML / HAR", "*.html;*.htm;*.txt;*.har"),
                ("All files", "*.*"),
            ],
            initialdir=str(HERE),
        )
        if not path:
            return
        try:
            text = load_steamdb_page_text(path)
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return
        parsed = parse_steamdb_html(text)
        if not parsed:
            parsed = parse_steamdb_text(text)
        if not parsed:
            self.status.configure(
                text="В файле не найдены манифесты SteamDB.", foreground="#b00020"
            )
            return
        self._parsed = parsed
        depots = sorted({item["depot"] for item in parsed if item.get("depot")})
        if depots:
            self.depot_var.set(f"{depots[0]} — {depot_label(depots[0])}")
        self.status.configure(
            text=f"Из файла: {len(parsed)} манифестов"
            + (f", депо {', '.join(depots)}" if depots else ""),
            foreground="#1b5e20",
        )
        self.add_button.state(["!disabled"])

    def _parse(self):
        self._parsed = parse_steamdb_text(self.text.get("1.0", "end"))
        if not self._parsed:
            self.status.configure(
                text="Манифесты не найдены. Вставьте таблицу или строки с ID.",
                foreground="#b00020",
            )
            self.add_button.state(["disabled"])
            return
        preview = ", ".join(item["manifest"] for item in self._parsed[:4])
        if len(self._parsed) > 4:
            preview += ", …"
        self.status.configure(
            text=f"Найдено: {len(self._parsed)} — {preview}",
            foreground="#1b5e20",
        )
        self.add_button.state(["!disabled"])

    def _add(self):
        depot = depot_from_choice(self.depot_var.get())
        if not depot.isdigit():
            messagebox.showerror("Ошибка", "Некорректный Depot ID.", parent=self)
            return
        if not self._parsed:
            self._parse()
            if not self._parsed:
                return
        self.result = []
        for item in self._parsed:
            self.result.append(
                {
                    "depot": item.get("depot") or depot,
                    "manifest": item["manifest"],
                    "date": item["date"],
                    "note": item.get("note", ""),
                }
            )
        self.auto_open_value = bool(self.auto_open_var.get())
        self.destroy()


# --------------------------------------------------------------------------
# Просмотр команд
# --------------------------------------------------------------------------

class CommandsDialog(tk.Toplevel):
    def __init__(self, master, commands):
        super().__init__(master)
        self.title("Команды, которые будут выполнены")
        self.geometry("900x480")
        self.transient(master)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.text = tk.Text(frame, wrap="none", font=("Consolas", 9))
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        for index, (cmd, label) in enumerate(commands, 1):
            self.text.insert(
                "end",
                f"# [{index}] {label}\n" + " ".join(_quote(x) for x in cmd) + "\n\n",
            )
        self.text.configure(state="disabled")

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Копировать", command=self._copy).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="Закрыть", command=self.destroy).pack(side="right")

        self.grab_set()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end").strip())
        self.destroy()


def _quote(value: str) -> str:
    value = str(value)
    if not value or re.search(r"\s", value):
        return f'"{value}"'
    return value


# --------------------------------------------------------------------------
# Главное окно
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("War Thunder — Old Build Downloader")
        self.geometry("1220x840")
        self.minsize(980, 660)

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        # данные
        self.entries = load_catalog()
        self.settings = load_settings()
        self.rows = []  # соответствие iid -> entry

        # состояние выполнения
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._cancel_requested = False
        self._log_path: Optional[Path] = None
        self._job_total = 0
        self._job_done = 0
        self._last_output = []
        self._extra_env = {}
        self._login_proc = None
        self._active_dialog = None

        self._build_menu()
        self._build_ui()
        self._apply_settings_to_ui()
        self._refresh_tree()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)

    # ---------------- данные ----------------

    def _save_catalog(self):
        save_json(CATALOG_PATH, {"version": 1, "entries": self.entries})

    def _save_settings(self):
        save_json(SETTINGS_PATH, self.settings)

    def _all_entries_sorted(self):
        def key(entry):
            date = entry.get("date") or ""
            return (str(entry.get("depot", "")).rjust(8, "0"), _neg_date(date))

        return sorted(self.entries, key=key)

    # ---------------- интерфейс ----------------

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Открыть папку с программой", command=self._open_program_dir)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self._on_close)
        menubar.add_cascade(label="Файл", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="SteamDB — депо War Thunder", command=lambda: webbrowser.open(STEAMDB_APP_URL))
        tools_menu.add_command(
            label="Импорт сохранённой страницы SteamDB (HTML)…",
            command=self._import_html_file,
        )
        tools_menu.add_command(label="Проверить пути инструментов", command=self._autodetect_tools)
        menubar.add_cascade(label="Инструменты", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="О программе", command=self._about)
        menubar.add_cascade(label="Справка", menu=help_menu)

        self.configure(menu=menubar)

    def _build_ui(self):
        header = ttk.Frame(self, padding=(14, 10, 14, 6))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="War Thunder — Old Build Downloader",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Скачивайте исторические сборки по manifest id со SteamDB. "
                "Нужен аккаунт Steam с доступом к игре (War Thunder — free-to-play)."
            ),
            foreground="#555",
        ).pack(anchor="w", pady=(2, 0))

        vpaned = ttk.PanedWindow(self, orient="vertical")
        vpaned.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        top = ttk.Frame(vpaned)
        vpaned.add(top, weight=3)

        hpaned = ttk.PanedWindow(top, orient="horizontal")
        hpaned.pack(fill="both", expand=True)
        hpaned.add(self._build_left(hpaned), weight=3)
        hpaned.add(self._build_right(hpaned), weight=2)

        vpaned.add(self._build_log(vpaned), weight=2)

        self._vpaned = vpaned
        self._hpaned = hpaned
        self.after(150, self._set_sashes)

    def _set_sashes(self):
        try:
            self._vpaned.sashpos(0, max(420, self.winfo_height() - 330))
            self._hpaned.sashpos(0, 700)
        except tk.TclError:
            pass

    def _build_left(self, parent):
        frame = ttk.LabelFrame(parent, text="Каталог манифестов", padding=8)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(
            toolbar, text="Указать ID версии…", command=self._add_manifest
        ).pack(side="left")
        ttk.Button(toolbar, text="Импорт со SteamDB…", command=self._import_steamdb).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="HTML SteamDB…", command=self._import_html_file).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="Удалить", command=self._remove_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="Открыть SteamDB", command=self._open_steamdb_selected).pack(
            side="left", padx=(6, 0)
        )

        columns = ("depot", "name", "manifest", "date", "note")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        self.tree.heading("depot", text="Depot")
        self.tree.heading("name", text="Название")
        self.tree.heading("manifest", text="Manifest ID")
        self.tree.heading("date", text="Дата")
        self.tree.heading("note", text="Заметка")

        self.tree.column("depot", width=80, anchor="center", stretch=False)
        self.tree.column("name", width=300, anchor="w", stretch=True)
        self.tree.column("manifest", width=190, anchor="w", stretch=False)
        self.tree.column("date", width=150, anchor="w", stretch=False)
        self.tree.column("note", width=220, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.tag_configure("old", background="#fff6dc")
        self.tree.tag_configure("release", background="#e8f5e9")
        self.tree.bind("<Double-1>", lambda _e: self._open_steamdb_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_selection_label())

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        self.selection_label = ttk.Label(footer, text="Выбрано: 0", foreground="#555")
        self.selection_label.pack(side="left")
        ttk.Label(
            footer,
            text="Ctrl/Shift — выбрать несколько депо за один раз",
            foreground="#888",
        ).pack(side="right")

        return frame

    def _build_right(self, parent):
        frame = ttk.LabelFrame(parent, text="Параметры загрузки", padding=10)

        frame.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frame, text="Папка назначения:").grid(
            row=row, column=0, sticky="w", pady=4
        )
        out_wrap = ttk.Frame(frame)
        out_wrap.grid(row=row, column=1, sticky="ew", pady=4)
        out_wrap.columnconfigure(0, weight=1)
        self.out_var = tk.StringVar()
        ttk.Entry(out_wrap, textvariable=self.out_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(out_wrap, text="Обзор…", command=self._browse_output).grid(
            row=0, column=1, padx=(6, 0)
        )

        row += 1
        ttk.Label(frame, text="Инструмент:").grid(
            row=row, column=0, sticky="nw", pady=4
        )
        backend_wrap = ttk.Frame(frame)
        backend_wrap.grid(row=row, column=1, sticky="w", pady=4)
        self.backend_var = tk.StringVar(value="depotdownloader")
        ttk.Radiobutton(
            backend_wrap,
            text="DepotDownloader (QR-вход)",
            value="depotdownloader",
            variable=self.backend_var,
            command=self._on_backend_change,
        ).pack(anchor="w")
        ttk.Radiobutton(
            backend_wrap,
            text="steamctl (логин/пароль)",
            value="steamctl",
            variable=self.backend_var,
            command=self._on_backend_change,
        ).pack(anchor="w")

        row += 1
        ttk.Label(frame, text="Путь к инструменту:").grid(
            row=row, column=0, sticky="w", pady=4
        )
        tool_wrap = ttk.Frame(frame)
        tool_wrap.grid(row=row, column=1, sticky="ew", pady=4)
        tool_wrap.columnconfigure(0, weight=1)
        self.tool_var = tk.StringVar()
        ttk.Entry(tool_wrap, textvariable=self.tool_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(tool_wrap, text="Обзор…", command=self._browse_tool).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(tool_wrap, text="Найти", command=self._autodetect_tools).grid(
            row=0, column=2, padx=(6, 0)
        )

        row += 1
        options = ttk.Frame(frame)
        options.grid(row=row, column=1, sticky="ew", pady=(2, 4))
        self.qr_var = tk.BooleanVar(value=True)
        self.remember_var = tk.BooleanVar(value=True)
        self.threads_var = tk.IntVar(value=16)
        self.qr_check = ttk.Checkbutton(
            options, text="QR-вход", variable=self.qr_var
        )
        self.qr_check.pack(side="left")
        self.remember_check = ttk.Checkbutton(
            options, text="Запомнить пароль", variable=self.remember_var
        )
        self.remember_check.pack(side="left", padx=(10, 0))
        ttk.Label(options, text="Потоков:").pack(side="left", padx=(14, 4))
        self.threads_spin = ttk.Spinbox(
            options, from_=1, to=64, width=5, textvariable=self.threads_var
        )
        self.threads_spin.pack(side="left")

        row += 1
        self.user_label = ttk.Label(frame, text="Имя пользователя Steam:")
        self.user_label.grid(row=row, column=0, sticky="w", pady=4)
        user_wrap = ttk.Frame(frame)
        user_wrap.grid(row=row, column=1, sticky="ew", pady=4)
        user_wrap.columnconfigure(0, weight=1)
        self.user_var = tk.StringVar()
        self.user_entry = ttk.Entry(user_wrap, textvariable=self.user_var)
        self.user_entry.grid(row=0, column=0, sticky="ew")
        self.login_button = ttk.Button(
            user_wrap, text="Вход…", command=self._steamctl_login
        )
        self.login_button.grid(row=0, column=1, padx=(6, 0))

        row += 1
        ttk.Label(frame, text="App ID:").grid(row=row, column=0, sticky="w", pady=4)
        app_wrap = ttk.Frame(frame)
        app_wrap.grid(row=row, column=1, sticky="ew", pady=4)
        self.app_var = tk.StringVar(value=str(APP_ID))
        ttk.Entry(app_wrap, textvariable=self.app_var, width=16).pack(side="left")
        ttk.Label(
            app_wrap,
            text="War Thunder = 236390",
            foreground="#888",
        ).pack(side="left", padx=(8, 0))

        row += 1
        ttk.Button(
            frame,
            text="Указать ID версии со SteamDB…",
            command=self._add_manifest,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        row += 1
        actions = ttk.Frame(frame)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.download_button = ttk.Button(
            actions, text="Скачать выбранное", command=self._start_download
        )
        self.download_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.preview_button = ttk.Button(
            actions, text="Показать команды", command=self._preview_commands
        )
        self.preview_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        row += 1
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 2))

        row += 1
        self.status_label = ttk.Label(frame, text="Готово к работе.", foreground="#333")
        self.status_label.grid(row=row, column=0, columnspan=2, sticky="w")

        row += 1
        self.cancel_button = ttk.Button(
            frame, text="Остановить всё", command=self._cancel, state="disabled"
        )
        self.cancel_button.grid(row=row, column=0, columnspan=2, sticky="e", pady=(6, 0))

        row += 1
        tip = ttk.Label(
            frame,
            text=(
                "Совет: для 2013 года добавьте manifest id для депо 236394 (launcher)\n"
                "и 236391 (content) — их можно импортировать со SteamDB."
            ),
            foreground="#777",
            justify="left",
        )
        tip.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))

        row += 1
        self.input_label = ttk.Label(frame, text="Ввод (пароль/код):")
        self.input_label.grid(row=row, column=0, sticky="w", pady=(8, 0))
        input_wrap = ttk.Frame(frame)
        input_wrap.grid(row=row, column=1, sticky="ew", pady=(8, 0))
        input_wrap.columnconfigure(0, weight=1)
        self.stdin_var = tk.StringVar()
        self.stdin_entry = ttk.Entry(
            input_wrap, textvariable=self.stdin_var, state="disabled"
        )
        self.stdin_entry.grid(row=0, column=0, sticky="ew")
        self.stdin_button = ttk.Button(
            input_wrap, text="↵", command=self._send_input, state="disabled", width=4
        )
        self.stdin_button.grid(row=0, column=1, padx=(4, 0))
        self.stdin_entry.bind("<Return>", lambda _e: self._send_input())

        return frame

    def _build_log(self, parent):
        frame = ttk.LabelFrame(parent, text="Лог / QR-код", padding=8)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 6))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar, text="Автопрокрутка", variable=self.autoscroll_var
        ).pack(side="left")
        ttk.Button(toolbar, text="Очистить", command=self._clear_log).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(toolbar, text="Сохранить лог…", command=self._save_log_as).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="Открыть папку", command=self._open_output_dir).pack(
            side="left", padx=(6, 0)
        )

        text_frame = ttk.Frame(frame)
        text_frame.pack(fill="both", expand=True)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            text_frame,
            wrap="none",
            font=("Consolas", 9),
            background="#ffffff",
            foreground="#111111",
            insertbackground="#111111",
        )
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(state="disabled")

        return frame

    # ---------------- настройки / каталог ----------------

    def _apply_settings_to_ui(self):
        settings = self.settings
        self.out_var.set(settings.get("output_dir", DEFAULT_SETTINGS["output_dir"]))
        self.backend_var.set(settings.get("backend", "depotdownloader"))
        self.threads_var.set(int(settings.get("threads", 16) or 16))
        self.qr_var.set(bool(settings.get("use_qr", True)))
        self.remember_var.set(bool(settings.get("remember_password", True)))
        self.app_var.set(str(settings.get("app_id", APP_ID)))
        self.user_var.set(settings.get("steamctl_user", ""))

        backend = self.backend_var.get()
        if backend == "steamctl":
            self.tool_var.set(settings.get("steamctl_path", "") or find_steamctl())
        else:
            self.tool_var.set(settings.get("dd_path", "") or find_depotdownloader())
        self._on_backend_change()

    def _collect_settings(self):
        backend = self.backend_var.get()
        self.settings.update(
            {
                "output_dir": self.out_var.get().strip(),
                "backend": backend,
                "threads": _safe_int(self.threads_var.get(), 16),
                "use_qr": bool(self.qr_var.get()),
                "remember_password": bool(self.remember_var.get()),
                "app_id": _safe_int(self.app_var.get(), APP_ID),
                "steamctl_user": self.user_var.get().strip(),
            }
        )
        if backend == "steamctl":
            self.settings["steamctl_path"] = self.tool_var.get().strip()
        else:
            self.settings["dd_path"] = self.tool_var.get().strip()
        self._save_settings()

    def _refresh_tree(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        for entry in self._all_entries_sorted():
            depot = str(entry.get("depot", ""))
            date = entry.get("date", "")
            tags = []
            if date.startswith("2013"):
                tags.append("old")
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    depot,
                    depot_label(depot),
                    entry.get("manifest", ""),
                    date,
                    entry.get("note", ""),
                ),
                tags=tuple(tags),
            )
            self.rows.append((iid, entry))
        # восстановить выделение по (depot, manifest)
        by_key = {}
        for iid, entry in self.rows:
            by_key[(entry.get("depot"), entry.get("manifest"))] = iid
        restore = [
            iid for iid in selected if iid in self.tree.get_children()
        ]
        if restore:
            self.tree.selection_set(restore)
        self._update_selection_label()

    def _selected_entries(self):
        selected = []
        for iid in self.tree.selection():
            for row_iid, entry in self.rows:
                if row_iid == iid:
                    selected.append(dict(entry))
                    break
        return selected

    def _update_selection_label(self):
        count = len(self.tree.selection())
        self.selection_label.configure(text=f"Выбрано: {count}")

    # ---------------- действия с каталогом ----------------

    def _add_manifest(self):
        depot_default = "236391"
        selected = self._selected_entries()
        if selected:
            depot_default = selected[0]["depot"]
        dialog = ManifestDialog(
            self,
            depot_default=depot_default,
            auto_open=bool(self.settings.get("auto_open_steamdb", True)),
            app_id=_safe_int(self.app_var.get(), APP_ID),
            steamctl_exe=self._steamctl_executable(),
            steamctl_user=self.user_var.get().strip(),
        )
        self.wait_window(dialog)
        if not dialog.result:
            return
        result = dict(dialog.result)
        self.settings["auto_open_steamdb"] = bool(result.pop("auto_open", True))
        self._save_settings()
        if self._entry_exists(result["depot"], result["manifest"]):
            messagebox.showinfo("Уже есть", "Такая пара депо + manifest уже в каталоге.")
            return
        self.entries.append(result)
        self._save_catalog()
        self._refresh_tree()

    def _import_steamdb(self):
        depot_default = "236391"
        selected = self._selected_entries()
        if selected:
            depot_default = selected[0]["depot"]
        dialog = ImportDialog(
            self,
            depot_default,
            auto_open=bool(self.settings.get("auto_open_steamdb", True)),
        )
        self.wait_window(dialog)
        if dialog.result:
            self.settings["auto_open_steamdb"] = bool(
                getattr(dialog, "auto_open_value", True)
            )
            self._save_settings()
        if not dialog.result:
            return
        added = 0
        skipped = 0
        for entry in dialog.result:
            if self._entry_exists(entry["depot"], entry["manifest"]):
                skipped += 1
                continue
            self.entries.append(entry)
            added += 1
        if added:
            self._save_catalog()
            self._refresh_tree()
        messagebox.showinfo(
            "Импорт завершён",
            f"Добавлено: {added}\nПропущено дубликатов: {skipped}",
        )

    def _import_html_file(self):
        path = filedialog.askopenfilename(
            title="Сохранённая страница SteamDB",
            filetypes=[
                ("SteamDB HTML / HAR", "*.html;*.htm;*.txt;*.har"),
                ("All files", "*.*"),
            ],
            initialdir=str(HERE),
        )
        if not path:
            return
        try:
            text = load_steamdb_page_text(path)
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        parsed = parse_steamdb_html(text)
        if not parsed:
            messagebox.showwarning(
                "Ничего не найдено",
                "В файле не найдены строки манифестов SteamDB.\n"
                "Нужна сохранённая страница depot/<id>/manifests/.",
            )
            return

        depots = sorted({item["depot"] for item in parsed if item.get("depot")})
        depot = depots[0] if len(depots) == 1 else ""
        if not depot:
            depot = simpledialog.askstring(
                "Depot ID",
                "Укажите Depot ID для загруженных версий:",
                initialvalue="236391",
                parent=self,
            )
            if not depot or not str(depot).isdigit():
                return

        added = skipped = 0
        for item in parsed:
            entry = {
                "depot": item.get("depot") or str(depot),
                "manifest": item["manifest"],
                "date": item["date"],
                "note": item.get("note", ""),
            }
            if self._entry_exists(entry["depot"], entry["manifest"]):
                skipped += 1
                continue
            self.entries.append(entry)
            added += 1
        if added:
            self._save_catalog()
            self._refresh_tree()
        messagebox.showinfo(
            "Импорт из HTML завершён",
            f"Файл: {path}\nДобавлено: {added}\nПропущено дубликатов: {skipped}",
        )

    def _remove_selected(self):
        selected = self._selected_entries()
        if not selected:
            messagebox.showinfo("Ничего не выбрано", "Выделите строки для удаления.")
            return
        if not messagebox.askyesno(
            "Удалить", f"Удалить из каталога строк: {len(selected)}?"
        ):
            return
        keys = {(e["depot"], e["manifest"]) for e in selected}
        self.entries = [
            e for e in self.entries if (e.get("depot"), e.get("manifest")) not in keys
        ]
        self._save_catalog()
        self._refresh_tree()

    def _entry_exists(self, depot, manifest):
        return any(
            str(e.get("depot")) == str(depot)
            and str(e.get("manifest")) == str(manifest)
            for e in self.entries
        )

    def _open_steamdb_selected(self):
        selected = self._selected_entries()
        if selected:
            webbrowser.open(STEAMDB_DEPOT_URL.format(depot=selected[0]["depot"]))
        else:
            webbrowser.open(STEAMDB_APP_URL)

    # ---------------- пути / инструменты ----------------

    def _browse_output(self):
        initial = self.out_var.get().strip() or str(HERE)
        chosen = filedialog.askdirectory(initialdir=initial, title="Папка назначения")
        if chosen:
            self.out_var.set(chosen)

    def _browse_tool(self):
        filename = filedialog.askopenfilename(
            title="Выберите исполняемый файл",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if filename:
            self.tool_var.set(filename)

    def _autodetect_tools(self):
        backend = self.backend_var.get()
        if backend == "steamctl":
            found = find_steamctl()
            name = "steamctl"
        else:
            found = find_depotdownloader()
            name = "DepotDownloader"
        if found:
            self.tool_var.set(found)
            messagebox.showinfo("Найдено", f"{name}:\n{found}")
        else:
            messagebox.showwarning(
                "Не найдено",
                f"{name} не найден автоматически.\nУкажите путь вручную.",
            )

    def _on_backend_change(self):
        backend = self.backend_var.get()
        is_dd = backend == "depotdownloader"
        for widget in (self.qr_check, self.remember_check, self.threads_spin):
            try:
                widget.state(["!disabled"] if is_dd else ["disabled"])
            except tk.TclError:
                pass
        try:
            self.login_button.state(["!disabled"] if not is_dd else ["disabled"])
        except tk.TclError:
            pass
        self.user_label.configure(
            text="steamctl user:" if backend == "steamctl" else "Имя пользователя Steam:"
        )
        # всегда подставляем путь, соответствующий выбранному бэкенду
        if is_dd:
            current = self.settings.get("dd_path", "") or find_depotdownloader()
            if not current or "steamctl" in str(current).lower():
                current = find_depotdownloader()
        else:
            current = self.settings.get("steamctl_path", "") or find_steamctl()
            if not current or "DepotDownloader" in str(current):
                current = find_steamctl()
        if current:
            self.tool_var.set(current)

    def _steamctl_executable(self) -> str:
        exe = self.tool_var.get().strip()
        if self.backend_var.get() == "steamctl" and exe and "DepotDownloader" not in exe:
            return exe
        return self.settings.get("steamctl_path", "") or find_steamctl()

    def _steamctl_login(self):
        exe = self.tool_var.get().strip() or find_steamctl()
        if not exe:
            messagebox.showerror(
                "steamctl не найден",
                "Установите steamctl:  pip install steamctl\n"
                "или укажите путь к steamctl.exe.",
            )
            return
        cmd = command_prefix(exe)
        user = self.user_var.get().strip()
        if user:
            cmd += ["--user", user]
        cmd += ["depot", "info", "--app", str(self.app_var.get().strip() or APP_ID)]
        entry = self.entries[0] if self.entries else None
        if entry:
            cmd += ["--depot", str(entry["depot"]), "--manifest", str(entry["manifest"])]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["CLICOLOR"] = "0"
        env["TERM"] = "dumb"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                cwd=str(HERE),
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            messagebox.showerror("Ошибка запуска", str(exc))
            return

        self._login_proc = proc
        self._update_cancel_state()
        self.status_label.configure(text="Идёт вход в steamctl…")
        self._append_log(
            "\n=== Вход steamctl ===\n"
            f"$ {' '.join(_quote(part) for part in cmd)}\n"
        )
        threading.Thread(
            target=self._read_login_output, args=(proc,), daemon=True
        ).start()

    def _read_login_output(self, proc: subprocess.Popen):
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = strip_ansi(decode_console(raw)).rstrip("\n")
            if text.strip():
                self._queue.put(("log", text + "\n"))
        rc = proc.wait()
        self._queue.put(("login_finished", rc))

    # ---------------- сборка заданий ----------------

    def _collect_jobs(self):
        entries = self._selected_entries()
        if not entries:
            messagebox.showinfo(
                "Ничего не выбрано",
                "Выделите в каталоге одну или несколько строк (Ctrl/Shift).",
            )
            return None

        try:
            app_id = int(self.app_var.get().strip())
            threads = int(self.threads_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("Ошибка", "App ID и число потоков должны быть числами.")
            return None
        if not 1 <= threads <= 64:
            messagebox.showerror("Ошибка", "Число потоков должно быть от 1 до 64.")
            return None

        out_dir = self.out_var.get().strip()
        if not out_dir:
            messagebox.showerror("Ошибка", "Укажите папку назначения.")
            return None
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось создать папку:\n{exc}")
            return None

        backend = self.backend_var.get()
        exe = self.tool_var.get().strip()
        if not exe:
            exe = find_steamctl() if backend == "steamctl" else find_depotdownloader()
        # защита от «гибридной» команды: путь обязан соответствовать бэкенду
        if backend == "steamctl" and ("DepotDownloader" in exe or "depotdownloader" in exe.lower()):
            exe = find_steamctl()
        elif backend == "depotdownloader" and "steamctl" in exe.lower():
            exe = find_depotdownloader()
        if exe:
            self.tool_var.set(exe)
        if not exe:
            messagebox.showerror("Ошибка", "Не найден исполняемый файл инструмента.")
            return None
        if not (Path(exe).is_file() or shutil.which(exe)):
            messagebox.showerror("Ошибка", f"Файл не найден:\n{exe}")
            return None

        jobs = []
        for entry in entries:
            label = f"depot {entry['depot']} / manifest {entry['manifest']}"
            if backend == "steamctl":
                cmd = build_steamctl_cmd(
                    exe,
                    app_id,
                    entry["depot"],
                    entry["manifest"],
                    out_dir,
                    user=self.user_var.get().strip(),
                )
            else:
                cmd = build_depotdownloader_cmd(
                    exe,
                    app_id,
                    entry["depot"],
                    entry["manifest"],
                    out_dir,
                    threads=threads,
                    qr=bool(self.qr_var.get()),
                    remember_password=bool(self.remember_var.get()),
                    user=self.user_var.get().strip(),
                )
            jobs.append((cmd, label))

        self._extra_env = {}
        self._collect_settings()
        return jobs, out_dir

    def _preview_commands(self):
        collected = self._collect_jobs()
        if not collected:
            return
        jobs, _out_dir = collected
        CommandsDialog(self, jobs)

    # ---------------- запуск / процесс ----------------

    def _start_download(self):
        if self._running:
            return
        collected = self._collect_jobs()
        if not collected:
            return
        jobs, out_dir = collected

        backend_name = (
            "steamctl" if self.backend_var.get() == "steamctl" else "DepotDownloader"
        )
        summary = "\n".join(f"• {label}" for _cmd, label in jobs)
        if not messagebox.askyesno(
            "Начать загрузку",
            f"Инструмент: {backend_name}\nПапка: {out_dir}\n\n{summary}\n\nПродолжить?",
        ):
            return

        self._running = True
        self._cancel_requested = False
        self._job_total = len(jobs)
        self._job_done = 0
        self.progress.configure(maximum=max(len(jobs), 1), value=0)
        self.status_label.configure(text="Запуск…")
        self.download_button.state(["disabled"])
        self.preview_button.state(["disabled"])
        self.cancel_button.state(["!disabled"])

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_path = Path(out_dir) / f"wt-download-{timestamp}.log"

        self._append_log(
            "\n" + "=" * 78 + "\n"
            f"War Thunder old build download — {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Backend: {backend_name}   Output: {out_dir}\n"
            f"Log: {self._log_path}\n"
            + "=" * 78 + "\n"
        )
        if self.backend_var.get() == "steamctl":
            self._append_log(
                "[INFO] Если steamctl запросит пароль или код Guard — "
                "введите его в поле «Ввод» под логом.\n"
            )

        self._worker = threading.Thread(
            target=self._run_jobs, args=(jobs, str(self._log_path)), daemon=True
        )
        self._worker.start()

    def _run_jobs(self, jobs, log_path):
        last_rc = None
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                for index, (cmd, label) in enumerate(jobs, 1):
                    if self._cancel_requested:
                        break
                    header = (
                        f"\n--- [{index}/{len(jobs)}] {label} ---\n"
                        f"$ {' '.join(_quote(part) for part in cmd)}\n"
                    )
                    self._queue.put(("log", header))
                    log_file.write(header)
                    log_file.flush()
                    self._queue.put(("status", f"Загрузка {index}/{len(jobs)}: {label}"))
                    self._queue.put(("progress", (index - 1, len(jobs))))

                    self._last_output = []
                    rc = self._run_one(cmd, log_file)
                    last_rc = rc
                    self._queue.put(("log", f"--- exit code: {rc} ---\n"))
                    log_file.write(f"--- exit code: {rc} ---\n")
                    log_file.flush()
                    self._queue.put(("progress", (index, len(jobs))))
                    self._job_done = index

                    # как в исходном .bat — пишем код возврата в exit.txt
                    try:
                        (Path(log_path).parent / "exit.txt").write_text(
                            str(rc), encoding="utf-8"
                        )
                    except OSError:
                        pass

                    if self._cancel_requested:
                        break
                    if rc != 0:
                        # 0 — успех; остальные коды не прерывают очередь,
                        # чтобы можно было скачать остальные депо.
                        self._queue.put(
                            ("log", f"ВНИМАНИЕ: код {rc}. Смотрите сообщения выше.\n")
                        )
                        output = "\n".join(self._last_output)
                        if any(
                            marker in output
                            for marker in (
                                "Failed to poll status", "FileNotFound",
                                "Lost connection", "TryAnotherCM",
                            )
                        ):
                            self._queue.put(
                                (
                                    "log",
                                    "СОВЕТ: соединение со Steam прервалось во время QR-входа.\n"
                                    "После успешного сканирования DepotDownloader сохранил токен — "
                                    "укажите имя пользователя в поле «Имя пользователя Steam» и "
                                    "снимите галочку «QR-вход», чтобы войти по сохранённому токену "
                                    "(-username ... -remember-password).\n",
                                )
                            )

        except Exception:
            self._queue.put(("log", "ОШИБКА обработчика:\n" + traceback.format_exc()))
        finally:
            self._proc = None
            self._queue.put(("finished", last_rc, log_path))

    def _run_one(self, cmd, log_file) -> int:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["CLICOLOR"] = "0"
        env["TERM"] = "dumb"
        env.update(self._extra_env)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                cwd=str(HERE),
                env=env,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            self._queue.put(("log", f"ОШИБКА: файл не найден: {cmd[0]}\n"))
            return 127
        except OSError as exc:
            self._queue.put(("log", f"ОШИБКА запуска: {exc}\n"))
            return 126

        self._proc = proc
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = strip_ansi(decode_console(raw))
            if "\r" in text:
                parts = [piece for piece in text.replace("\r", "\n").split("\n") if piece.strip()]
                text = parts[-1] if parts else ""
            text = text.rstrip("\n")
            if text.strip():
                self._last_output.append(text)
                self._queue.put(("log", text + "\n"))
                try:
                    log_file.write(text + "\n")
                    log_file.flush()
                except OSError:
                    pass
            if self._cancel_requested:
                self._terminate_proc(proc)
                break

        rc = proc.wait()
        self._proc = None
        return rc

    def _terminate_proc(self, proc: subprocess.Popen):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _send_input(self):
        text = self.stdin_var.get()
        if not text:
            return
        proc = self._proc
        if proc is None or proc.poll() is not None:
            proc = self._login_proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            messagebox.showinfo("Нет процесса", "Сейчас нет запущенного процесса.")
            return
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            proc.stdin.flush()
            # само значение пароля/кода НЕ пишем в лог — только длину
            self._append_log(f"[INFO] отправлено символов: {len(text)}\n")
            self.stdin_var.set("")
            self._set_input_enabled(False)
        except (OSError, ValueError) as exc:
            self._append_log(f"Не удалось отправить ввод: {exc}\n")

    def _focus_input(self):
        self._set_input_enabled(True)
        try:
            self.stdin_entry.focus_set()
            self.stdin_entry.selection_range(0, "end")
        except tk.TclError:
            pass
        self.status_label.configure(
            text="Нужен ввод: наберите пароль/код в поле «Ввод» и нажмите Enter."
        )

    def _set_input_enabled(self, enabled: bool):
        state = "!disabled" if enabled else "disabled"
        try:
            self.stdin_entry.state([state])
            self.stdin_button.state([state])
        except tk.TclError:
            pass

    def _any_operation_active(self) -> bool:
        dialog = getattr(self, "_active_dialog", None)
        return bool(
            self._running
            or (self._login_proc is not None and self._login_proc.poll() is None)
            or (dialog is not None and getattr(dialog, "_lookup_running", False))
        )

    def _update_cancel_state(self):
        state = "!disabled" if self._any_operation_active() else "disabled"
        try:
            self.cancel_button.state([state])
        except tk.TclError:
            pass

    def _cancel(self):
        stopped = []
        if self._running:
            self._cancel_requested = True
            if self._proc is not None:
                self._terminate_proc(self._proc)
            stopped.append("загрузка")

        if self._login_proc is not None and self._login_proc.poll() is None:
            self._terminate_proc(self._login_proc)
            self._append_log("\n=== Вход steamctl остановлен ===\n")
            stopped.append("вход steamctl")

        dialog = getattr(self, "_active_dialog", None)
        if dialog is not None and getattr(dialog, "_lookup_running", False):
            try:
                dialog.stop_lookup()
                stopped.append("запрос даты")
            except Exception:
                pass

        self.status_label.configure(text="Останавливаю…")
        if stopped:
            self._append_log(f"\n=== Остановлено: {', '.join(stopped)} ===\n")

    # ---------------- очередь сообщений ----------------

    def _poll_queue(self):
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._append_log(message[1])
                    if re.search(
                        r"Password:|Enter password|Enter 2FA|Guard|auth code|код\s+Guard|введите.*код",
                        message[1],
                        re.IGNORECASE,
                    ):
                        self._focus_input()
                elif kind == "status":
                    self.status_label.configure(text=message[1])
                elif kind == "progress":
                    done, total = message[1]
                    self.progress.configure(maximum=max(total, 1), value=done)
                elif kind == "finished":
                    self._on_finished(message[1], message[2])
                elif kind == "login_finished":
                    rc = message[1]
                    self._login_proc = None
                    self._append_log(f"\n=== Вход steamctl завершён, exit {rc} ===\n")
                    if rc == 0:
                        self.status_label.configure(text="Вход выполнен, можно скачивать.")
                    else:
                        self.status_label.configure(
                            text=f"Вход не удался (exit {rc}). Подробности в логе."
                        )
                    if not self._running:
                        self._set_input_enabled(False)
                    self._update_cancel_state()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _on_finished(self, rc, log_path):
        self._running = False
        self.download_button.state(["!disabled"])
        self.preview_button.state(["!disabled"])
        self._update_cancel_state()
        if self._login_proc is None:
            self._set_input_enabled(False)
        if self._cancel_requested:
            self.status_label.configure(text="Остановлено пользователем.")
            self._append_log("\n=== Остановлено пользователем ===\n")
        elif rc == 0:
            self.status_label.configure(text="Готово. Код возврата 0 (успех).")
            self._append_log("\n=== Готово, exit code 0 ===\n")
        else:
            self.status_label.configure(
                text=f"Завершено с кодом {rc}. Подробности в логе."
            )
            self._append_log(f"\n=== Завершено, exit code {rc} ===\n")
        self._append_log(f"Лог сохранён: {log_path}\n")

    # ---------------- лог ----------------

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        # ограничение, чтобы виджет не разросся бесконечно
        try:
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 6000:
                self.log_text.delete("1.0", f"{line_count - 5000}.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.configure(state="disabled")
        if self.autoscroll_var.get():
            self.log_text.see("end")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_log_as(self):
        content = self.log_text.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo("Лог пуст", "Нечего сохранять.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt")],
            initialfile=f"wt-download-{datetime.now():%Y%m%d-%H%M%S}.log",
        )
        if path:
            Path(path).write_text(content + "\n", encoding="utf-8")
            messagebox.showinfo("Сохранено", path)

    # ---------------- прочее ----------------

    def _open_program_dir(self):
        _open_path(HERE)

    def _open_output_dir(self):
        out = self.out_var.get().strip()
        if out and Path(out).is_dir():
            _open_path(Path(out))
        else:
            messagebox.showinfo("Папка не найдена", "Сначала укажите существующую папку.")

    def _about(self):
        messagebox.showinfo(
            "О программе",
            "War Thunder — Old Build Downloader\n\n"
            "Скачивание исторических депо Steam по manifest id.\n"
            "Manifest id берутся на SteamDB:\n"
            f"{STEAMDB_APP_URL}\n\n"
            "Инструменты: DepotDownloader или steamctl.\n"
            "Код: Python + tkinter, без внешних зависимостей.",
        )

    def _on_close(self):
        if self._any_operation_active():
            if not messagebox.askyesno(
                "Работа идёт",
                "Остановить текущие операции и закрыть программу?",
            ):
                return
            self._cancel()
            self._save_settings()
            self.after(400, self.destroy)
        else:
            self._save_settings()
            self.destroy()


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _neg_date(value: str) -> str:
    # для сортировки по убыванию даты: инвертируем цифры
    value = value or ""
    return "".join(str(9 - int(ch)) if ch.isdigit() else ch for ch in value)


def _open_path(path: Path):
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import subprocess as _sp

            _sp.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def check_environment():
    """Возвращает список предупреждений (для --selftest / диагностики)."""
    warnings = []
    if not find_depotdownloader():
        warnings.append("DepotDownloader не найден (можно указать путь вручную).")
    if not find_steamctl():
        warnings.append("steamctl не найден (pip install steamctl).")
    return warnings


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def selftest() -> int:
    sample = """
    | Seen Date | Relative Date | ManifestID |
    | 14 October 2026 – 15:25:52 UTC | | 3914674954621805258 `release_candidate` |
    | 13 October 2026 – 12:32:16 UTC | | 6526336152778705309 |
    Displaying manifest 2399792843388606621 dated 13 November 2026 – 11:31:16 UTC
    3264770838696250786
    """
    parsed = parse_steamdb_text(sample)
    manifests = {item["manifest"] for item in parsed}
    assert "3264770838696250786" in manifests, "не найден одиночный manifest"
    assert "3914674954621805258" in manifests, "не найден manifest из таблицы"
    assert len(parsed) == len(manifests), "дубликаты не отфильтрованы"

    cmd = build_depotdownloader_cmd(
        r"C:\dd\DepotDownloader.exe", APP_ID, "236391", "3264770838696250786",
        r"C:\WT_old", threads=16, qr=True, remember_password=True,
    )
    assert cmd[0].endswith("DepotDownloader.exe")
    assert "-qr" in cmd and "-remember-password" in cmd
    assert cmd[cmd.index("-manifest") + 1] == "3264770838696250786"

    cmd3 = build_depotdownloader_cmd(
        "dd.exe", APP_ID, "236391", "3264770838696250786", r"C:\WT_old",
        qr=True, user="example_user",
    )
    assert "-username" in cmd3 and "-qr" not in cmd3

    cmd2 = build_steamctl_cmd(
        "steamctl", APP_ID, "236391", "3264770838696250786", r"C:\WT_old",
        user="testuser",
    )
    assert cmd2[:3] == ["steamctl", "--user", "testuser"]
    assert cmd2[:4][0] == "steamctl" and "depot" in cmd2

    assert normalize_date("15 August 2013 – 12:13:36 UTC") == "2013-08-15 12:13:36"
    assert normalize_date("2013-08-15 12:13:36") == "2013-08-15 12:13:36"
    assert extract_manifest("14 October 2026 – 15:25:52 UTC | 3914674954621805258") == "3914674954621805258"
    assert extract_depot_from_text("https://steamdb.info/depot/236391/manifests/") == "236391"
    assert parse_steamctl_info("App ID: 236390\nCreated On: 2013-08-15 12:13:36\nSize: 1 GiB") == "2013-08-15 12:13:36"
    html = (
        '<tr><td class="timeago" data-time="2013-08-15T12:13:36+00:00">x</td>'
        '<td class="tabular-nums"><a href="/depot/236391/history/?changeid=M:3264770838696250786">'
        "3264770838696250786</a></td></tr>"
    )
    html_parsed = parse_steamdb_html(html)
    assert len(html_parsed) == 1
    assert html_parsed[0]["manifest"] == "3264770838696250786"
    assert html_parsed[0]["date"] == "2013-08-15 12:13:36"

    print("selftest OK")
    for warning in check_environment():
        print("WARN:", warning)
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    app = App()
    # первичная загрузка/создание файлов каталога и настроек
    if not CATALOG_PATH.exists():
        save_json(CATALOG_PATH, {"version": 1, "entries": DEFAULT_CATALOG["entries"]})
    if not SETTINGS_PATH.exists():
        save_json(SETTINGS_PATH, DEFAULT_SETTINGS)

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
