from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import time
from zoneinfo import ZoneInfo
import sys
import tkinter as tk
from tkinter import messagebox

import requests
import urllib3


NYSE_TZ = ZoneInfo("America/New_York")
NYSE_SYMBOLS_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
NASDAQ_SYMBOLS_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
SYMBOL_CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class ShareMetrics:
    symbol: str
    opening_price: float
    close_price_for_card: float
    daily_change_dollar: float
    daily_change_percent: float
    market_open: bool
    history: list[float]
    history_dates: list[str]


def is_nyse_open(now: datetime | None = None) -> bool:
    current = now.astimezone(NYSE_TZ) if now else datetime.now(NYSE_TZ)
    if current.weekday() >= 5:
        return False

    open_time = current.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = current.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= current < close_time


def get_runtime_proxy() -> str | None:
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def get_tls_verify_setting() -> bool | str:
    insecure = os.environ.get("SHARE_CHECKER_INSECURE_SSL", "").strip().lower()
    if insecure in {"1", "true", "yes", "on"}:
        return False

    invalid_keys: list[str] = []
    for key in ("SHARE_CHECKER_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        value = os.environ.get(key)
        if value:
            if Path(value).expanduser().is_file():
                return value
            invalid_keys.append(key)

    for key in invalid_keys:
        os.environ.pop(key, None)

    try:
        import certifi

        certifi_path = Path(certifi.where())
        if certifi_path.is_file():
            return str(certifi_path)
    except Exception:
        pass

    return True


def fetch_yahoo_ohlc(symbol: str, proxy: str | None, verify: bool | str) -> list[tuple[str, float, float]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "range": "7d",
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    response = requests.get(url, params=params, headers=headers, timeout=20, proxies=proxies, verify=verify)
    response.raise_for_status()
    payload = response.json()

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise ValueError(f"Yahoo API returned error: {chart['error']}")

    results = chart.get("result") or []
    if not results:
        raise ValueError("Yahoo API returned no results.")

    result = results[0]
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    timestamps = result.get("timestamp") or []

    rows: list[tuple[str, float, float]] = []
    for ts_value, open_value, close_value in zip(timestamps, opens, closes):
        if open_value is None or close_value is None:
            continue
        date_label = datetime.fromtimestamp(ts_value, NYSE_TZ).strftime("%b-%d")
        rows.append((date_label, float(open_value), float(close_value)))

    if len(rows) < 2:
        raise ValueError("Could not load enough valid OHLC rows from Yahoo data.")

    return rows


def fetch_supported_symbols() -> dict[str, str]:
    proxy = get_runtime_proxy()
    verify = get_tls_verify_setting()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    symbols: dict[str, str] = {}

    other_response = requests.get(NYSE_SYMBOLS_URL, timeout=20, proxies=proxies, verify=verify)
    other_response.raise_for_status()
    other_lines = [line.strip() for line in other_response.text.splitlines() if line.strip()]
    if len(other_lines) < 2:
        raise ValueError("US symbols source (otherlisted) returned no data.")

    other_header = other_lines[0].split("|")
    try:
        other_symbol_idx = other_header.index("ACT Symbol")
        other_name_idx = other_header.index("Security Name")
    except ValueError as exc:
        raise ValueError("Unexpected otherlisted symbols format.") from exc

    for line in other_lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= max(other_symbol_idx, other_name_idx):
            continue
        symbol = parts[other_symbol_idx].strip().upper()
        name = parts[other_name_idx].strip()
        if symbol and symbol != "ACT SYMBOL":
            symbols[symbol] = name

    nasdaq_response = requests.get(NASDAQ_SYMBOLS_URL, timeout=20, proxies=proxies, verify=verify)
    nasdaq_response.raise_for_status()
    nasdaq_lines = [line.strip() for line in nasdaq_response.text.splitlines() if line.strip()]
    if len(nasdaq_lines) < 2:
        raise ValueError("US symbols source (nasdaqlisted) returned no data.")

    nasdaq_header = nasdaq_lines[0].split("|")
    try:
        nasdaq_symbol_idx = nasdaq_header.index("Symbol")
        nasdaq_name_idx = nasdaq_header.index("Security Name")
    except ValueError as exc:
        raise ValueError("Unexpected nasdaqlisted symbols format.") from exc

    for line in nasdaq_lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= max(nasdaq_symbol_idx, nasdaq_name_idx):
            continue
        symbol = parts[nasdaq_symbol_idx].strip().upper()
        name = parts[nasdaq_name_idx].strip()
        if symbol and symbol != "SYMBOL":
            symbols.setdefault(symbol, name)

    if not symbols:
        raise ValueError("Symbol sources parsed but no symbols were found.")

    return symbols


def fetch_metrics(symbol: str) -> ShareMetrics:
    proxy = get_runtime_proxy()
    verify = get_tls_verify_setting()

    try:
        rows = fetch_yahoo_ohlc(symbol, proxy, verify)
    except Exception as exc:
        network_hint = (
            "Set HTTPS_PROXY / HTTP_PROXY in the same terminal. "
            "If your company inspects TLS, set REQUESTS_CA_BUNDLE to your corporate root CA PEM file "
            "or run with --ca-bundle <path>."
        )
        raise ValueError(f"Unable to download share data for '{symbol}'. {network_hint} Details: {exc}") from exc

    nyse_open = is_nyse_open()

    _, latest_open, latest_close = rows[-1]
    _, _, previous_close = rows[-2]
    history = [close for _date, _open, close in rows]
    history_dates = [date for date, _open, _close in rows]

    today_open = latest_open

    if nyse_open:
        close_for_card = previous_close
    else:
        close_for_card = latest_close

    daily_change_dollar = close_for_card - today_open
    daily_change_percent = (daily_change_dollar / today_open * 100.0) if today_open else 0.0

    return ShareMetrics(
        symbol=symbol.upper(),
        opening_price=today_open,
        close_price_for_card=close_for_card,
        daily_change_dollar=daily_change_dollar,
        daily_change_percent=daily_change_percent,
        market_open=nyse_open,
        history=history,
        history_dates=history_dates,
    )


class ShareCardApp:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.nyse_symbols: set[str] | None = None
        self.symbol_names: dict[str, str] = {}
        self.sorted_symbols: list[str] = []
        self.symbol_catalog_error: str | None = None
        self.symbol_catalog_loading = False
        self.symbol_cache_timestamp = 0.0
        self.symbol_validation_after_id: str | None = None
        self.suggestion_frame: tk.Toplevel | None = None
        self.suggestion_scrollbar: tk.Scrollbar | None = None
        self.suggestion_list: tk.Listbox | None = None
        self.suppress_suggestions_once = False
        self.chart_canvas: tk.Canvas | None = None
        self.last_history: list[float] = []
        self.last_history_dates: list[str] = []
        self.symbol_catalog_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"{self.symbol}")
        self.root.geometry("440x470")
        self.root.minsize(380, 420)
        self.root.resizable(True, True)
        icon_loaded = False
        png_icon_path = Path(__file__).with_name("Icon.png")
        if png_icon_path.exists():
            try:
                icon_image = tk.PhotoImage(file=str(png_icon_path))
                self.root.iconphoto(True, icon_image)
                self._icon_image = icon_image
                icon_loaded = True
            except tk.TclError:
                pass
        if not icon_loaded and sys.platform == "darwin":
            icns_path = Path(__file__).with_name("AppIcon.icns")
            if icns_path.exists():
                try:
                    self.root.iconbitmap(str(icns_path))
                    icon_loaded = True
                except tk.TclError:
                    pass
        if not icon_loaded:
            icon_path = Path(__file__).with_name("app.ico")
            if icon_path.exists():
                try:
                    self.root.iconbitmap(str(icon_path))
                except tk.TclError:
                    pass

        self._init_colors()
        self.root.configure(bg=self.bg_color)

        self.positive_color = "#0f766e"
        self.negative_color = "#b91c1c"

        card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief="solid", padx=16, pady=14)
        card.pack(fill="both", expand=True, padx=20, pady=18)

        self.title_label = tk.Label(
            card,
            text=f"{self.symbol}",
            font=("Helvetica Neue", 16, "bold"),
            bg=self.card_bg,
            fg=self.text_primary,
            anchor="w",
        )
        self.title_label.pack(fill="x", pady=(0, 4))

        self.status_label = tk.Label(
            card,
            text="Fetching latest quote...",
            font=("Helvetica Neue", 10),
            bg=self.card_bg,
            fg=self.text_muted,
            anchor="w",
        )
        self.status_label.pack(fill="x", pady=(0, 10))

        symbol_row = tk.Frame(card, bg=self.card_bg)
        symbol_row.pack(fill="x", pady=(0, 10))

        symbol_label = tk.Label(
            symbol_row,
            text="Symbol",
            font=("Helvetica Neue", 10, "bold"),
            bg=self.card_bg,
            fg=self.text_primary,
        )
        symbol_label.pack(side="left")

        self.symbol_var = tk.StringVar(value=self.symbol)
        self.symbol_entry = tk.Entry(symbol_row, textvariable=self.symbol_var, width=12, relief="solid")
        self.symbol_entry.pack(side="left", padx=(8, 8), ipadx=2, ipady=1)
        self.symbol_entry.bind("<Return>", self.load_symbol)
        self.symbol_entry.bind("<KeyRelease>", self.schedule_symbol_validation)
        self.symbol_entry.bind("<Down>", self.focus_suggestion_list)
        self.symbol_entry.bind("<FocusOut>", self.hide_symbol_suggestions)

        load_button = tk.Button(
            symbol_row,
            text="Load",
            command=self.load_symbol,
            takefocus=0,
            relief="solid",
            bd=1,
            highlightthickness=0,
            bg=self.button_bg,
            activebackground=self.button_active_bg,
            fg=self.button_fg,
            activeforeground=self.button_fg,
            highlightbackground=self.shadow_color,
            highlightcolor=self.shadow_color,
            padx=8,
            pady=2,
        )
        load_button.pack(side="left")

        self.suggestion_frame = tk.Toplevel(self.root)
        self.suggestion_frame.withdraw()
        self.suggestion_frame.overrideredirect(True)
        self.suggestion_frame.attributes("-topmost", True)
        self.suggestion_frame.configure(bg=self.card_bg, bd=1, relief="solid")

        self.suggestion_scrollbar = tk.Scrollbar(self.suggestion_frame, orient="vertical")
        self.suggestion_list = tk.Listbox(
            self.suggestion_frame,
            height=8,
            font=("Segoe UI", 9),
            yscrollcommand=self.suggestion_scrollbar.set,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=self.shadow_color,
        )
        self.suggestion_scrollbar.config(command=self.suggestion_list.yview)
        self.suggestion_list.pack(side="left", fill="both", expand=True)
        self.suggestion_scrollbar.pack(side="right", fill="y")
        self.suggestion_list.bind("<<ListboxSelect>>", self.apply_selected_suggestion)
        self.suggestion_list.bind("<ButtonRelease-1>", self.apply_and_load_suggestion)
        self.suggestion_list.bind("<Double-Button-1>", self.apply_and_load_suggestion)
        self.suggestion_list.bind("<Return>", self.apply_and_load_suggestion)
        self.suggestion_frame.withdraw()

        self.root.bind("<Button-1>", self.on_global_click, add=True)

        self.validation_label = tk.Label(
            card,
            text="",
            font=("Helvetica Neue", 9),
            bg=self.card_bg,
            fg=self.text_muted,
            anchor="w",
        )
        self.validation_label.pack(fill="x", pady=(0, 8))

        info_frame = tk.Frame(card, bg=self.card_bg)
        info_frame.pack(fill="x")
        info_frame.columnconfigure(1, weight=1)

        label_style = {
            "font": ("Helvetica Neue", 10),
            "bg": self.card_bg,
            "fg": self.text_muted,
            "anchor": "w",
        }
        value_style = {
            "font": ("Helvetica Neue", 11, "bold"),
            "bg": self.card_bg,
            "fg": self.text_primary,
            "anchor": "e",
        }

        tk.Label(info_frame, text="Open", **label_style).grid(row=0, column=0, sticky="w", pady=2)
        self.opening_label = tk.Label(info_frame, text="--", **value_style)
        self.opening_label.grid(row=0, column=1, sticky="e", pady=2)

        tk.Label(info_frame, text="Close", **label_style).grid(row=1, column=0, sticky="w", pady=2)
        self.close_label = tk.Label(info_frame, text="--", **value_style)
        self.close_label.grid(row=1, column=1, sticky="e", pady=2)

        tk.Label(info_frame, text="Daily Change %", **label_style).grid(row=2, column=0, sticky="w", pady=2)
        self.change_pct_label = tk.Label(info_frame, text="--", **value_style)
        self.change_pct_label.grid(row=2, column=1, sticky="e", pady=2)

        tk.Label(info_frame, text="Daily Change $", **label_style).grid(row=3, column=0, sticky="w", pady=2)
        self.change_dollar_label = tk.Label(info_frame, text="--", **value_style)
        self.change_dollar_label.grid(row=3, column=1, sticky="e", pady=2)

        trend_label = tk.Label(
            card,
            text="7-day trend",
            font=("Helvetica Neue", 9),
            bg=self.card_bg,
            fg=self.text_muted,
            anchor="w",
        )
        trend_label.pack(fill="x", pady=(10, 4))

        self.chart_canvas = tk.Canvas(
            card,
            height=110,
            bg=self.card_bg,
            highlightthickness=1,
            highlightbackground=self.shadow_color,
        )
        self.chart_canvas.pack(fill="x")
        self.chart_canvas.bind("<Configure>", self.on_chart_resize)

        refresh_button = tk.Button(
            card,
            text="Refresh",
            command=self.refresh,
            takefocus=0,
            relief="solid",
            bd=1,
            highlightthickness=0,
            bg=self.button_bg,
            activebackground=self.button_active_bg,
            fg=self.button_fg,
            activeforeground=self.button_fg,
            highlightbackground=self.shadow_color,
            highlightcolor=self.shadow_color,
            padx=10,
            pady=2,
        )
        refresh_button.pack(anchor="e", pady=(12, 0))

        self.start_symbol_catalog_refresh(force=True)
        self.root.after(100, self.process_symbol_catalog_queue)
        self.refresh()

    def _resolve_color(self, name: str, fallback: str) -> str:
        try:
            self.root.winfo_rgb(name)
            return name
        except tk.TclError:
            return fallback

    def _is_light_color(self, color: str) -> bool:
        try:
            r, g, b = self.root.winfo_rgb(color)
        except tk.TclError:
            return True
        # Normalize 16-bit RGB to 0-255 and compute relative luminance.
        r8 = r / 257
        g8 = g / 257
        b8 = b / 257
        luminance = 0.2126 * r8 + 0.7152 * g8 + 0.0722 * b8
        return luminance >= 140

    def _init_colors(self) -> None:
        self.bg_color = self._resolve_color("SystemButtonFace", "#f0f0f0")
        self.card_bg = self._resolve_color("SystemWindow", "#ffffff")
        self.text_primary = self._resolve_color("SystemWindowText", "#111111")
        self.text_muted = self._resolve_color("SystemGrayText", "#6b7280")
        self.button_bg = self._resolve_color("SystemButtonFace", "#e5e7eb")
        self.button_active_bg = self._resolve_color("SystemButtonFace", "#d1d5db")
        self.shadow_color = self._resolve_color("SystemButtonShadow", "#c7c7c7")
        self.button_fg = self._resolve_color("SystemButtonText", self.text_primary)
        if self._is_light_color(self.button_bg):
            self.button_fg = "#111111"
        elif self._is_light_color(self.text_primary):
            self.button_fg = "#f9fafb"

    def update_title(self, symbol: str) -> None:
        self.root.title(f"{symbol}")
        self.title_label.config(text=f"{symbol}")

    def load_symbol(self, _event: object | None = None) -> None:
        candidate = self.symbol_var.get().strip().upper()
        if not candidate:
            messagebox.showwarning("Symbol Required", "Please enter a ticker symbol.")
            return

        if self.nyse_symbols is None:
            self.start_symbol_catalog_refresh(force=False)
        elif candidate not in self.nyse_symbols:
            messagebox.showwarning("Invalid Symbol", f"'{candidate}' is not in the supported US symbol list.")
            self.validation_label.config(text="Symbol not found in US symbol list.", fg="red")
            return

        previous_symbol = self.symbol
        self.symbol = candidate
        if not self.refresh():
            self.symbol = previous_symbol
            self.symbol_var.set(previous_symbol)
            self.update_title(previous_symbol)
        else:
            company = self.symbol_names.get(candidate, "").strip()
            label_text = company or "Valid US-listed symbol."
            self.validation_label.config(text=label_text, fg="green")

    def schedule_symbol_validation(self, _event: object | None = None) -> None:
        if self.symbol_validation_after_id:
            self.root.after_cancel(self.symbol_validation_after_id)
        self.symbol_validation_after_id = self.root.after(250, self.update_symbol_input_state)

    def update_symbol_input_state(self) -> None:
        self.symbol_validation_after_id = None
        self.validate_symbol_realtime()
        self.update_symbol_suggestions()

    def validate_symbol_realtime(self) -> None:
        candidate = self.symbol_var.get().strip().upper()
        if not candidate:
            self.validation_label.config(text="", fg="black")
            return

        if self.nyse_symbols is None:
            if self.symbol_catalog_loading:
                self.validation_label.config(text="Checking symbol list...", fg="gray")
            elif self.symbol_catalog_error:
                self.validation_label.config(text="Symbol list unavailable right now.", fg="gray")
            else:
                self.validation_label.config(text="Loading symbol list...", fg="gray")
            return

        if candidate in self.nyse_symbols:
            company = self.symbol_names.get(candidate, "").strip()
            label_text = company or "Valid US-listed symbol."
            self.validation_label.config(text=label_text, fg="green")
        else:
            self.validation_label.config(text="Symbol not found in US symbol list.", fg="red")

    def update_symbol_suggestions(self) -> None:
        if self.suppress_suggestions_once:
            self.suppress_suggestions_once = False
            self.hide_symbol_suggestions()
            return
        candidate = self.symbol_var.get().strip().upper()
        if not candidate or not self.sorted_symbols:
            self.hide_symbol_suggestions()
            return

        matches: list[str] = []
        for item in self.sorted_symbols:
            if item.startswith(candidate):
                matches.append(item)

        if matches and not (len(matches) == 1 and matches[0] == candidate):
            self.show_symbol_suggestions(matches)
        else:
            self.hide_symbol_suggestions()

    def show_symbol_suggestions(self, matches: list[str]) -> None:
        if self.suggestion_list is None or self.suggestion_frame is None:
            return

        self.root.update_idletasks()
        entry_x = self.symbol_entry.winfo_rootx()
        entry_y = self.symbol_entry.winfo_rooty()
        entry_w = self.symbol_entry.winfo_width()
        entry_h = self.symbol_entry.winfo_height()
        row_height = 20
        screen_h = self.root.winfo_screenheight()
        margin = 8
        space_below = screen_h - (entry_y + entry_h) - margin
        space_above = entry_y - margin
        place_above = space_below < row_height * 3 and space_above > space_below
        available_px = space_above if place_above else space_below
        visible_rows = max(1, min(10, len(matches), max(1, available_px // row_height)))
        popup_h = visible_rows * row_height + 2

        self.suggestion_list.delete(0, tk.END)
        for item in matches:
            self.suggestion_list.insert(tk.END, item)
        self.suggestion_list.config(height=visible_rows)
        if place_above:
            popup_y = max(margin, entry_y - popup_h - 2)
        else:
            popup_y = entry_y + entry_h + 2
        self.suggestion_frame.geometry(f"{entry_w + 18}x{popup_h}+{entry_x}+{popup_y}")
        self.suggestion_frame.deiconify()
        self.suggestion_frame.lift()

    def hide_symbol_suggestions(self, _event: object | None = None) -> None:
        if self.suggestion_frame is not None:
            self.suggestion_frame.withdraw()

    def on_global_click(self, event: tk.Event) -> None:
        if self.suggestion_frame is None or self.suggestion_list is None:
            return
        if not self.suggestion_frame.winfo_ismapped():
            return
        target = event.widget
        if target in (self.symbol_entry, self.suggestion_list, self.suggestion_scrollbar):
            return
        if self.suggestion_frame.winfo_containing(event.x_root, event.y_root) is not None:
            return
        self.hide_symbol_suggestions()

    def focus_suggestion_list(self, _event: object | None = None) -> str | None:
        if self.suggestion_list is not None and self.suggestion_list.winfo_ismapped() and self.suggestion_list.size() > 0:
            self.suggestion_list.focus_set()
            self.suggestion_list.selection_clear(0, tk.END)
            self.suggestion_list.selection_set(0)
            return "break"
        return None

    def apply_selected_suggestion(self, _event: object | None = None) -> None:
        if self.suggestion_list is None:
            return
        selection = self.suggestion_list.curselection()
        if not selection:
            return
        chosen = self.suggestion_list.get(selection[0])
        self.symbol_var.set(chosen)
        self.validate_symbol_realtime()

    def apply_and_load_suggestion(self, _event: object | None = None) -> str:
        self.apply_selected_suggestion()
        self.hide_symbol_suggestions()
        self.symbol_entry.focus_set()
        self.suppress_suggestions_once = True
        self.load_symbol()
        return "break"

    def start_symbol_catalog_refresh(self, force: bool) -> None:
        if self.symbol_catalog_loading:
            return

        if not force and self.nyse_symbols is not None:
            age = time.time() - self.symbol_cache_timestamp
            if age < SYMBOL_CACHE_TTL_SECONDS:
                return

        self.symbol_catalog_loading = True
        self.validation_label.config(text="Loading symbol list...", fg="gray")
        thread = threading.Thread(target=self._refresh_symbol_catalog_worker, daemon=True)
        thread.start()

    def _refresh_symbol_catalog_worker(self) -> None:
        try:
            symbols = fetch_supported_symbols()
            self.symbol_catalog_queue.put(("ok", symbols))
        except Exception as exc:
            self.symbol_catalog_queue.put(("err", str(exc)))

    def process_symbol_catalog_queue(self) -> None:
        while not self.symbol_catalog_queue.empty():
            status, payload = self.symbol_catalog_queue.get()
            if status == "ok":
                self._on_symbol_catalog_loaded(payload)
            else:
                self._on_symbol_catalog_failed(payload)
        if self.root.winfo_exists():
            self.root.after(100, self.process_symbol_catalog_queue)

    def _on_symbol_catalog_loaded(self, symbols: dict[str, str]) -> None:
        self.symbol_names = symbols
        self.nyse_symbols = set(symbols.keys())
        self.sorted_symbols = sorted(self.nyse_symbols)
        self.symbol_catalog_error = None
        self.symbol_catalog_loading = False
        self.symbol_cache_timestamp = time.time()
        self.update_symbol_input_state()

    def _on_symbol_catalog_failed(self, error: str) -> None:
        self.symbol_catalog_error = error
        self.symbol_catalog_loading = False
        self.update_symbol_input_state()

    def refresh(self) -> bool:
        self.status_label.config(text="Fetching latest quote...", fg=self.text_muted)
        self.root.update_idletasks()

        try:
            data = fetch_metrics(self.symbol)
        except Exception as exc:
            self.status_label.config(text="Unable to load quote. Check network and try again.", fg=self.negative_color)
            self.opening_label.config(text="--", fg=self.text_primary)
            self.close_label.config(text="--", fg=self.text_primary)
            self.change_pct_label.config(text="--", fg=self.text_primary)
            self.change_dollar_label.config(text="--", fg=self.text_primary)
            messagebox.showerror("Data Error", str(exc))
            return False

        close_suffix = "(Prev Day, Market Open)" if data.market_open else ""

        self.symbol = data.symbol
        self.symbol_var.set(data.symbol)
        self.update_title(data.symbol)

        change_color = self.positive_color if data.daily_change_dollar >= 0 else self.negative_color

        self.opening_label.config(text=f"${data.opening_price:,.2f}")
        self.close_label.config(text=f"${data.close_price_for_card:,.2f} {close_suffix}".rstrip())
        self.change_pct_label.config(text=f"{data.daily_change_percent:+.2f}%", fg=change_color)
        self.change_dollar_label.config(text=f"${data.daily_change_dollar:+,.2f}", fg=change_color)
        self.status_label.config(text="Quote updated.", fg=self.text_muted)
        self.update_symbol_input_state()
        self.draw_trend(data.history, data.history_dates)
        return True

    def run(self) -> None:
        self.root.mainloop()

    def on_chart_resize(self, _event: tk.Event) -> None:
        if self.last_history:
            self.draw_trend(self.last_history, self.last_history_dates)

    def draw_trend(self, history: list[float], history_dates: list[str]) -> None:
        if self.chart_canvas is None:
            return
        if not history:
            self.chart_canvas.delete("trend")
            return

        self.last_history = history
        self.last_history_dates = history_dates
        self.chart_canvas.delete("trend")

        self.root.update_idletasks()
        width = max(1, self.chart_canvas.winfo_width())
        height = max(1, self.chart_canvas.winfo_height())
        left_pad = 54
        right_pad = 10
        top_pad = 8
        bottom_pad = 26
        inner_w = max(1, width - left_pad - right_pad)
        inner_h = max(1, height - top_pad - bottom_pad)

        min_val = min(history)
        max_val = max(history)
        if max_val == min_val:
            max_val = min_val + 1.0
        span = max_val - min_val
        min_val -= span * 0.02
        max_val += span * 0.02
        span = max_val - min_val

        step_x = inner_w / max(1, len(history) - 1)
        points: list[float] = []
        for idx, value in enumerate(history):
            x = left_pad + idx * step_x
            ratio = (value - min_val) / span
            y = top_pad + (1.0 - ratio) * inner_h
            points.extend([x, y])

        grid_color = self.shadow_color
        label_color = self.text_muted

        for i in range(5):
            y = top_pad + i * (inner_h / 4)
            self.chart_canvas.create_line(
                left_pad,
                y,
                left_pad + inner_w,
                y,
                fill=grid_color,
                width=1,
                tags="trend",
            )
            value = max_val - (span * i / 4)
            self.chart_canvas.create_text(
                left_pad - 6,
                y,
                text=f"{value:,.2f}",
                anchor="e",
                fill=label_color,
                font=("Segoe UI", 8),
                tags="trend",
            )

        if len(history) > 1:
            for idx in range(len(history)):
                x = left_pad + idx * step_x
                self.chart_canvas.create_line(
                    x,
                    top_pad,
                    x,
                    top_pad + inner_h,
                    fill=grid_color,
                    width=1,
                    tags="trend",
                )

        self.chart_canvas.create_line(
            left_pad,
            top_pad,
            left_pad,
            top_pad + inner_h,
            fill=grid_color,
            width=1,
            tags="trend",
        )
        self.chart_canvas.create_line(
            left_pad,
            top_pad + inner_h,
            left_pad + inner_w,
            top_pad + inner_h,
            fill=grid_color,
            width=1,
            tags="trend",
        )

        if len(history) >= 2:
            start_label = history_dates[0] if history_dates else f"D-{len(history) - 1}"
            end_label = history_dates[-1] if history_dates else "D0"
            self.chart_canvas.create_text(
                left_pad,
                top_pad + inner_h + 12,
                text=start_label,
                anchor="w",
                fill=label_color,
                font=("Segoe UI", 8),
                tags="trend",
            )
            self.chart_canvas.create_text(
                left_pad + inner_w,
                top_pad + inner_h + 12,
                text=end_label,
                anchor="e",
                fill=label_color,
                font=("Segoe UI", 8),
                tags="trend",
            )

        line_color = self.positive_color if history[-1] >= history[0] else self.negative_color
        self.chart_canvas.create_line(*points, fill=line_color, width=2, smooth=True, tags="trend")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small Windows share card app")
    parser.add_argument("--symbol", default="DOX", help="Ticker symbol (default: DOX)")
    parser.add_argument("--ca-bundle", default=None, help="Path to corporate CA bundle PEM file")
    parser.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="Disable TLS verification (temporary troubleshooting only)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.ca_bundle:
        ca_path = Path(args.ca_bundle)
        if not ca_path.exists():
            raise FileNotFoundError(f"CA bundle file not found: {ca_path}")
        os.environ["SHARE_CHECKER_CA_BUNDLE"] = str(ca_path)

    if args.insecure_ssl:
        os.environ["SHARE_CHECKER_INSECURE_SSL"] = "1"
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    app = ShareCardApp(symbol=args.symbol)
    app.run()


if __name__ == "__main__":
    main()
