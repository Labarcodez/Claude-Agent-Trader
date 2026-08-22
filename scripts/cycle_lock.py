"""CycleLock: a dependency-free mutual-exclusion lock guarding a trading
cycle's whole read-modify-write sequence against a second concurrent process
(or, for the live trade-cycle skill, a second concurrent Claude session)
doing the same thing at the same time.

Originally lived only in paper_trading/run_paper_cycle.py, added after
mining journal/paper_trades.jsonl found two near-simultaneous buy/sell pairs
on the same symbol (MET, RIZO), seconds apart, identical size and
return_pct -- the signature of this session's cron loop and a separate
local terminal loop both racing on the same state/paper_portfolio.json:
each loaded state before either saved, both independently decided the same
trade, and whichever save landed second silently clobbered the first's,
while the append-only journal kept both entries -- a trade the portfolio
state never actually reflected.

Extracted here so the live trade-cycle skill can guard
state/starting_capital.json, state/circuit_breaker.json, and
journal/trades.jsonl the same way -- that pathway has the exact same
exposure (this session's cron loop plus a possible local terminal session
both running trade-cycle) and is more consequential now that it's real
money, not paper.

os.O_CREAT | os.O_EXCL is an atomic exclusive-create on both POSIX and
Windows, so this needs no extra dependency (the project stays
dependency-free). A stale lock (left behind by a crashed/killed process, or
an LLM session that never reached its release step) is broken after
`timeout` seconds rather than deadlocking the system forever -- an
unattended cron loop has no one around to clear it by hand."""
import os
import time
from pathlib import Path


class CycleLock:
    def __init__(self, path: Path, timeout: float = 280.0, poll: float = 0.5):
        self.path = path
        self.timeout = timeout
        self.poll = poll

    def acquire(self) -> "CycleLock":
        start = time.time()
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - start > self.timeout:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                time.sleep(self.poll)

    def release(self) -> None:
        try:
            os.remove(str(self.path))
        except OSError:
            pass

    def __enter__(self) -> "CycleLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.release()
        return False
