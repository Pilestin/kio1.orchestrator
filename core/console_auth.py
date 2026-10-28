"""One password in front of everything: the first lock for a KIO1 on a server.

A KIO1 reachable from the internet is found by scanners within hours, and
without a lock anyone who finds it can run workflows — every one a paid model
call, and some a command on the server — and read every conversation. Until
there are accounts (docs/plans/accounts.md), one password closes that:

- The password is never stored, only its scrypt hash, in
  `KIO1_CONSOLE_PASSWORD_HASH` (`python start.py --set-password` writes it).
  With no hash set, the lock is off — the default on 127.0.0.1, where only
  this machine can connect; `start.py` refuses any other address without one.
- Signing in gives the browser an HttpOnly session cookie, which `fetch` and
  the live view's `EventSource` both send on their own. Sessions live in
  memory: restarting KIO1 signs everyone out.
- The administration token still works as a bearer token, for scripts.
- Failed attempts are limited per address and overall, so a password cannot
  be guessed at the speed of a script.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

PASSWORD_HASH_ENV = "KIO1_CONSOLE_PASSWORD_HASH"
COOKIE = "kio1_session"

# scrypt's interactive-login cost: about 50 ms and 16 MiB per attempt.
_N, _R, _P = 2**14, 8, 1
SESSION_SECONDS = 7 * 24 * 3600
IDLE_SECONDS = 24 * 3600

# Per address: this many failures in the window, then a wait.
FAILURES_PER_ADDRESS = 5
# Across every address, so many addresses cannot share the guessing.
FAILURES_OVERALL = 50
FAILURE_WINDOW_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    """`scrypt$N$r$p$salt$hash`, all that is needed to check it later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32
    )
    encode = base64.urlsafe_b64encode
    return f"scrypt${_N}${_R}${_P}${encode(salt).decode()}${encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` is the one `stored` was made from."""
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.urlsafe_b64decode(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))


def configured_hash() -> str:
    return os.environ.get(PASSWORD_HASH_ENV, "").strip()


def enabled() -> bool:
    """Whether the console asks for a password."""
    return bool(configured_hash())


@dataclass
class _Session:
    created: float
    seen: float


@dataclass
class Gate:
    """The signed-in sessions and the failed attempts, held in memory."""

    sessions: dict[str, _Session] = field(default_factory=dict)
    failures: dict[str, list[float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def open_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.sessions[token] = _Session(created=now, seen=now)
        return token

    def check_session(self, token: str) -> bool:
        """Whether `token` is a live session; touching it keeps it alive."""
        if not token:
            return False
        now = time.time()
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return False
            if (
                now - session.created > SESSION_SECONDS
                or now - session.seen > IDLE_SECONDS
            ):
                del self.sessions[token]
                return False
            session.seen = now
            return True

    def close_session(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def wait_before_trying(self, address: str) -> int:
        """Seconds `address` must wait before another attempt; 0 to go ahead."""
        now = time.time()
        with self.lock:
            for key in list(self.failures):
                self.failures[key] = [
                    t for t in self.failures[key] if now - t < FAILURE_WINDOW_SECONDS
                ]
                if not self.failures[key]:
                    del self.failures[key]
            # An address of "" is a visitor whose address is not known.
            mine = self.failures.get(address, []) if address else []
            overall = sum(len(times) for times in self.failures.values())
            if len(mine) >= FAILURES_PER_ADDRESS:
                return int(FAILURE_WINDOW_SECONDS - (now - mine[0])) + 1
            if overall >= FAILURES_OVERALL:
                return 60
        return 0

    def failed(self, address: str) -> None:
        with self.lock:
            self.failures.setdefault(address, []).append(time.time())

    def succeeded(self, address: str) -> None:
        with self.lock:
            self.failures.pop(address, None)


GATE = Gate()

LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · KIO1</title>
<style>
  :root { --bg: #f5f6fa; --card: #fff; --ink: #0f1222; --muted: #5c6178; --line: #dfe2ec;
          --accent: #5b5bf0; --bad: #c0283b; color-scheme: light dark; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0b0d14; --card: #141722; --ink: #e8eaf4; --muted: #9aa0b8; --line: #262a3a; --bad: #ff6b7d; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg);
         color: var(--ink); font: 14px/1.5 "Inter", "Segoe UI", system-ui, sans-serif; }
  form { width: min(340px, calc(100vw - 32px)); padding: 28px; background: var(--card);
         border: 1px solid var(--line); border-radius: 16px; display: grid; gap: 14px; }
  .mark { width: 38px; height: 38px; border-radius: 10px; display: grid; place-items: center; color: #fff;
          font: 700 13px ui-monospace, Consolas, monospace; background: linear-gradient(135deg, #5b5bf0, #8b5cf6); }
  h1 { margin: 0; font-size: 18px; font-weight: 600; }
  input { width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px;
          background: transparent; color: inherit; font: inherit; }
  input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  button { padding: 10px; border: 0; border-radius: 10px; background: var(--accent); color: #fff;
           font: 600 14px/1 inherit; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: default; }
  .error { min-height: 1.5em; margin: 0; color: var(--bad); font-size: 13px; }
</style>
</head>
<body>
<form id="login">
  <div class="mark">K1</div>
  <h1>Sign in to KIO1</h1>
  <input id="password" type="password" autocomplete="current-password" placeholder="Password" required autofocus>
  <button id="go" type="submit">Sign in</button>
  <p class="error" id="error" role="alert"></p>
</form>
<script>
  document.getElementById("login").addEventListener("submit", async function (event) {
    event.preventDefault();
    const go = document.getElementById("go");
    const error = document.getElementById("error");
    go.disabled = true;
    error.textContent = "";
    try {
      const response = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: document.getElementById("password").value }),
      });
      if (response.ok) { window.location.replace("/" + window.location.hash); return; }
      const payload = await response.json().catch(function () { return {}; });
      error.textContent = payload.detail || "That did not work.";
    } catch (failure) {
      error.textContent = "KIO1 did not answer.";
    }
    go.disabled = false;
  });
</script>
</body>
</html>
"""
