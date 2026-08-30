"""First run: pair this laptop and pick the strap, without touching a file.

Everything here used to be typed at a prompt or edited into config.toml. Two
of those steps are the ones most likely to defeat someone -- copying a pairing
code into a terminal, and pasting a Bluetooth address into the right line of a
TOML file -- so they are a window with two fields and a list instead.

Tkinter deliberately: it ships with Python, so this adds nothing to the
download and nothing to go wrong at install time.
"""

from __future__ import annotations

import asyncio
import queue
import socket
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

from whoop_bridge.setup_config import (claim_pairing, existing_server,
                                       needs_setup, normalise_code,
                                       normalise_server, set_config_value,
                                       write_pairing)

__all__ = ["SetupWindow", "needs_setup"]

BG = "#141416"
CARD = "#1e1e21"
INK = "#f5f5f7"
MUTED = "#9a9aa0"
GOOD = "#00c26a"
BAD = "#ff453a"
LINE = "#2e2e33"


class SetupWindow:
    """Pair, then choose a strap. Returns True if the config is now usable."""

    def __init__(self, config_path: str | Path = "config.toml",
                 pair_fn: Callable[..., dict] | None = None,
                 scan_fn: Callable[[float], list] | None = None):
        self.config_path = Path(config_path)
        self._pair_fn = pair_fn
        self._scan_fn = scan_fn
        self.result: dict[str, Any] = {"paired": False, "address": ""}
        self._events: queue.Queue = queue.Queue()
        self._found: list[tuple[str, str]] = []

    # --- the work, off the UI thread ---------------------------------------
    def _pair(self, server: str, code: str) -> None:
        def worker() -> None:
            try:
                got = (self._pair_fn or claim_pairing)(server, code, socket.gethostname())
                write_pairing(self.config_path, server, got)
                self._events.put(("paired", got))
            except Exception as exc:                      # noqa: BLE001
                self._events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _scan(self) -> None:
        def worker() -> None:
            try:
                if self._scan_fn:
                    found = self._scan_fn(12.0)
                else:
                    from whoop_bridge.connection import scan
                    found = asyncio.run(scan(12.0))
                self._events.put(("found", found))
            except Exception as exc:                      # noqa: BLE001
                self._events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _save_address(self, address: str) -> None:
        set_config_value(self.config_path, "device", "address", address)
        self.result["address"] = address

    # --- the window ---------------------------------------------------------
    def run(self) -> dict[str, Any]:
        root = self._root = tk.Tk()
        root.title("Set up Strap")
        root.configure(bg=BG)
        root.geometry("460x430")
        root.minsize(420, 400)
        self._style(root)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=22, pady=20)

        tk.Label(wrap, text="Connect this laptop", bg=BG, fg=INK,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(wrap, text="Open Strap on your phone or browser, then\n"
                            "Settings → Connect a laptop.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9), justify="left").pack(anchor="w", pady=(2, 14))

        server_var = tk.StringVar(value=existing_server(self.config_path))
        code_var = tk.StringVar()
        self._field(wrap, "Server address", server_var, "https://strap.example.com")
        self._field(wrap, "Pairing code", code_var, "XXXX-XXXX")

        status = tk.Label(wrap, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9),
                          wraplength=400, justify="left")
        status.pack(anchor="w", pady=(8, 0))

        strap_frame = tk.Frame(wrap, bg=BG)
        listbox = tk.Listbox(strap_frame, bg=CARD, fg=INK, height=4, bd=0,
                             highlightthickness=1, highlightbackground=LINE,
                             selectbackground=GOOD, selectforeground="#000",
                             font=("Segoe UI", 9), activestyle="none")

        buttons = tk.Frame(wrap, bg=BG)
        buttons.pack(side="bottom", fill="x", pady=(14, 0))
        primary = ttk.Button(buttons, text="Pair")
        primary.pack(side="right")
        skip = ttk.Button(buttons, text="Later", command=root.destroy)
        skip.pack(side="right", padx=(0, 8))

        def say(text: str, colour: str = MUTED) -> None:
            status.configure(text=text, fg=colour)

        def do_pair() -> None:
            server, code = server_var.get().strip(), code_var.get().strip()
            if not server:
                say("Enter your server's address.", BAD); return
            if not code:
                say("Enter the code the app is showing.", BAD); return
            primary.state(["disabled"])
            say("Pairing…")
            self._pair(normalise_server(server), normalise_code(code))

        def do_scan() -> None:
            primary.state(["disabled"])
            say("Looking for your strap — about twelve seconds.\n"
                "Un-pair it from the WHOOP phone app first; it only talks to "
                "one device at a time.")
            self._scan()

        def choose() -> None:
            if not listbox.curselection():
                say("Pick your strap from the list.", BAD); return
            address = self._found[listbox.curselection()[0]][0]
            self._save_address(address)
            say(f"Saved {address}. You are set up.", GOOD)
            root.after(900, root.destroy)

        primary.configure(command=do_pair)

        def pump() -> None:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                root.after(120, pump); return

            if kind == "error":
                say(str(payload), BAD)
                primary.state(["!disabled"])
            elif kind == "paired":
                self.result["paired"] = True
                account = payload.get("account", "your account")
                say(f"Paired with {account}. Now find your strap.", GOOD)
                primary.configure(text="Find my strap", command=do_scan)
                primary.state(["!disabled"])
            elif kind == "found":
                self._found = list(payload)
                if not self._found:
                    say("No strap found. Un-pair it from the WHOOP phone app, "
                        "make sure it is charged, and try again.", BAD)
                    primary.state(["!disabled"])
                else:
                    strap_frame.pack(fill="x", pady=(10, 0))
                    listbox.pack(fill="x")
                    listbox.delete(0, tk.END)
                    for address, name in self._found:
                        listbox.insert(tk.END, f"  {name or 'WHOOP'}   {address}")
                    listbox.selection_set(0)
                    say("Choose your strap.", INK)
                    primary.configure(text="Use this one", command=choose)
                    primary.state(["!disabled"])
            root.after(120, pump)

        root.after(120, pump)
        root.mainloop()
        return self.result

    # --- chrome -------------------------------------------------------------
    @staticmethod
    def _style(root: tk.Tk) -> None:
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", background=GOOD, foreground="#000",
                        borderwidth=0, focuscolor=BG, padding=(16, 7),
                        font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("disabled", LINE), ("active", "#00e07a")],
                  foreground=[("disabled", MUTED)])

    @staticmethod
    def _field(parent: tk.Widget, label: str, var: tk.StringVar, hint: str) -> tk.Entry:
        tk.Label(parent, text=label.upper(), bg=BG, fg=MUTED,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", pady=(8, 3))
        entry = tk.Entry(parent, textvariable=var, bg=CARD, fg=INK, bd=0,
                         insertbackground=INK, font=("Segoe UI", 10),
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=GOOD)
        entry.pack(fill="x", ipady=6)
        if hint:
            tk.Label(parent, text=hint, bg=BG, fg=LINE,
                     font=("Segoe UI", 8)).pack(anchor="w")
        return entry
