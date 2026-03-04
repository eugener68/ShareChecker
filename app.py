from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo
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

    for key in ("SHARE_CHECKER_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        value = os.environ.get(key)
        if value:
            return value

    return True


def fetch_yahoo_ohlc(symbol: str, proxy: str | None, verify: bool | str) -> list[tuple[float, float]]:
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

    quote = (((results[0].get("indicators") or {}).get("quote") or [{}])[0])
    opens = quote.get("open") or []
    closes = quote.get("close") or []

    rows: list[tuple[float, float]] = []
    for open_value, close_value in zip(opens, closes):
        if open_value is None or close_value is None:
            continue
        rows.append((float(open_value), float(close_value)))

    if len(rows) < 2:
        raise ValueError("Could not load enough valid OHLC rows from Yahoo data.")

    return rows


def fetch_supported_symbols() -> set[str]:
    proxy = get_runtime_proxy()
    verify = get_tls_verify_setting()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    symbols: set[str] = set()

    other_response = requests.get(NYSE_SYMBOLS_URL, timeout=20, proxies=proxies, verify=verify)
    other_response.raise_for_status()
    other_lines = [line.strip() for line in other_response.text.splitlines() if line.strip()]
    if len(other_lines) < 2:
        raise ValueError("US symbols source (otherlisted) returned no data.")

    other_header = other_lines[0].split("|")
    try:
        other_symbol_idx = other_header.index("ACT Symbol")
    except ValueError as exc:
        raise ValueError("Unexpected otherlisted symbols format.") from exc

    for line in other_lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= other_symbol_idx:
            continue
        symbol = parts[other_symbol_idx].strip().upper()
        if symbol and symbol != "ACT SYMBOL":
            symbols.add(symbol)

    nasdaq_response = requests.get(NASDAQ_SYMBOLS_URL, timeout=20, proxies=proxies, verify=verify)
    nasdaq_response.raise_for_status()
    nasdaq_lines = [line.strip() for line in nasdaq_response.text.splitlines() if line.strip()]
    if len(nasdaq_lines) < 2:
        raise ValueError("US symbols source (nasdaqlisted) returned no data.")

    nasdaq_header = nasdaq_lines[0].split("|")
    try:
        nasdaq_symbol_idx = nasdaq_header.index("Symbol")
    except ValueError as exc:
        raise ValueError("Unexpected nasdaqlisted symbols format.") from exc

    for line in nasdaq_lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= nasdaq_symbol_idx:
            continue
        symbol = parts[nasdaq_symbol_idx].strip().upper()
        if symbol and symbol != "SYMBOL":
            symbols.add(symbol)

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

    latest_open, latest_close = rows[-1]
    _, previous_close = rows[-2]

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
    )


class ShareCardApp:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.nyse_symbols: set[str] | None = None
        self.sorted_symbols: list[str] = []
        self.symbol_catalog_error: str | None = None
        self.symbol_catalog_loading = False
        self.symbol_cache_timestamp = 0.0
        self.symbol_validation_after_id: str | None = None
        self.suggestion_frame: tk.Toplevel | None = None
        self.suggestion_scrollbar: tk.Scrollbar | None = None
        self.suggestion_list: tk.Listbox | None = None

        self.bg_color = "#eef1f6"
        self.card_bg = "#ffffff"
        self.text_primary = "#1b1f24"
        self.text_muted = "#6b7280"
        self.positive_color = "#0f766e"
        self.negative_color = "#b91c1c"

        self.root = tk.Tk()
        self.root.title(f"{self.symbol}")
        self.root.geometry("420x320")
        self.root.minsize(360, 280)
        self.root.resizable(True, True)
        self.root.configure(bg=self.bg_color)

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

        load_button = tk.Button(
            symbol_row,
            text="Load",
            command=self.load_symbol,
            takefocus=0,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg="#e5e7eb",
            activebackground="#d1d5db",
            fg=self.text_primary,
        )
        load_button.pack(side="left")

        self.suggestion_frame = tk.Toplevel(self.root)
        self.suggestion_frame.withdraw()
        self.suggestion_frame.overrideredirect(True)
        self.suggestion_frame.attributes("-topmost", True)
        self.suggestion_frame.configure(bg="white")

        self.suggestion_scrollbar = tk.Scrollbar(self.suggestion_frame, orient="vertical")
        self.suggestion_list = tk.Listbox(
            self.suggestion_frame,
            height=8,
            font=("Segoe UI", 9),
            yscrollcommand=self.suggestion_scrollbar.set,
            borderwidth=0,
            highlightthickness=0,
        )
        self.suggestion_scrollbar.config(command=self.suggestion_list.yview)
        self.suggestion_list.pack(side="left", fill="both", expand=True)
        self.suggestion_scrollbar.pack(side="right", fill="y")
        self.suggestion_list.bind("<<ListboxSelect>>", self.apply_selected_suggestion)
        self.suggestion_list.bind("<Double-Button-1>", self.apply_and_load_suggestion)
        self.suggestion_list.bind("<Return>", self.apply_and_load_suggestion)
        self.suggestion_frame.withdraw()

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

        refresh_button = tk.Button(
            card,
            text="Refresh",
            command=self.refresh,
            takefocus=0,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg="#e5e7eb",
            activebackground="#d1d5db",
            fg=self.text_primary,
        )
        refresh_button.pack(anchor="e", pady=(12, 0))

        self.start_symbol_catalog_refresh(force=True)
        self.refresh()

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
            self.validation_label.config(text="Valid US-listed symbol.", fg="green")

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
            self.validation_label.config(text="Valid US-listed symbol.", fg="green")
        else:
            self.validation_label.config(text="Symbol not found in US symbol list.", fg="red")

    def update_symbol_suggestions(self) -> None:
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
        visible_rows = min(10, len(matches))
        popup_h = visible_rows * 20 + 2

        self.suggestion_list.delete(0, tk.END)
        for item in matches:
            self.suggestion_list.insert(tk.END, item)
        self.suggestion_list.config(height=visible_rows)
        self.suggestion_frame.geometry(f"{entry_w + 18}x{popup_h}+{entry_x}+{entry_y + entry_h + 2}")
        self.suggestion_frame.deiconify()
        self.suggestion_frame.lift()

    def hide_symbol_suggestions(self) -> None:
        if self.suggestion_frame is not None:
            self.suggestion_frame.withdraw()

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
            self.root.after(0, lambda: self._on_symbol_catalog_loaded(symbols))
        except Exception as exc:
            self.root.after(0, lambda: self._on_symbol_catalog_failed(str(exc)))

    def _on_symbol_catalog_loaded(self, symbols: set[str]) -> None:
        self.nyse_symbols = symbols
        self.sorted_symbols = sorted(symbols)
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

        close_suffix = "(Prev Day, NYSE Open)" if data.market_open else ""

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
        return True

    def run(self) -> None:
        self.root.mainloop()


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
