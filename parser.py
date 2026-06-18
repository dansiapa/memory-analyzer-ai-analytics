"""
parser.py
Core parsing logic for Java thread dump (.txt) files.
Kept independent of any UI so it can be tested / reused on its own.

Supports thread dumps produced by:
  - jstack
  - jcmd Thread.print
  - kill -3 (stdout dumps)
  - VisualVM / most JVMs (HotSpot-style "Full thread dump" sections)

A single file may contain MULTIPLE dumps (e.g. captured every few seconds),
in which case each is parsed as a separate "snapshot".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from typing import List, Optional


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

@dataclass
class ThreadInfo:
    name: str
    state: str                      # RUNNABLE, WAITING, TIMED_WAITING, BLOCKED, NEW, TERMINATED, UNKNOWN
    daemon: bool = False
    priority: Optional[str] = None
    tid: Optional[str] = None
    nid: Optional[str] = None
    stack: List[str] = field(default_factory=list)
    locked_monitors: List[str] = field(default_factory=list)   # "locked <0x...> (a java.lang...)"
    waiting_on: Optional[str] = None                            # monitor this thread is waiting/blocked on
    raw_header: str = ""

    @property
    def top_frame(self) -> str:
        return self.stack[0] if self.stack else "(no stack)"

    @property
    def is_blocked(self) -> bool:
        return self.state == "BLOCKED"

    @property
    def is_waiting(self) -> bool:
        return self.state in ("WAITING", "TIMED_WAITING")


@dataclass
class Deadlock:
    raw_text: str
    threads_involved: List[str] = field(default_factory=list)


@dataclass
class Snapshot:
    """One full thread dump (a file can contain several, taken over time)."""
    index: int
    timestamp_line: str = ""        # e.g. "2026-06-18 14:32:01" if present
    threads: List[ThreadInfo] = field(default_factory=list)
    deadlocks: List[Deadlock] = field(default_factory=list)

    @property
    def state_counts(self) -> Counter:
        return Counter(t.state for t in self.threads)

    @property
    def daemon_count(self) -> int:
        return sum(1 for t in self.threads if t.daemon)


# ----------------------------------------------------------------------
# Regex patterns
# ----------------------------------------------------------------------

# Thread header, e.g.:
# "http-nio-8080-exec-3" #45 daemon prio=5 os_prio=0 cpu=12.34ms elapsed=120.00s tid=0x00007f... nid=0x1a03 waiting on condition [0x...]
#
# Rather than one fragile catch-all regex, the name is pulled out first, then
# the remainder of the line is scanned independently for each known field.
# This is far more robust against field reordering/omission across JVM vendors.
THREAD_NAME_RE = re.compile(r'^"(?P<name>.*)"')
PRIO_RE = re.compile(r'\bprio=(?P<prio>\d+)')
TID_RE = re.compile(r'\btid=(?P<tid>0x[0-9a-fA-F]+)')
NID_RE = re.compile(r'\bnid=(?P<nid>0x[0-9a-fA-F]+)')
DAEMON_RE = re.compile(r'\bdaemon\b')

# State line, e.g.: "   java.lang.Thread.State: BLOCKED (on object monitor)"
STATE_RE = re.compile(r'java\.lang\.Thread\.State:\s+(?P<state>\w+)')

# Stack frame, e.g.: "	at com.example.Foo.bar(Foo.java:123)"
FRAME_RE = re.compile(r'^\s*at\s+(?P<frame>.+)$')

# Lock lines
LOCKED_RE = re.compile(r'^\s*-\s+locked\s+(?P<lock>.+)$')
WAITING_LOCK_RE = re.compile(r'^\s*-\s+waiting (?:to lock|on)\s+(?P<lock>.+)$')
PARKING_RE = re.compile(r'^\s*-\s+parking to wait for\s+(?P<lock>.+)$')

DUMP_START_RE = re.compile(r'^Full thread dump|^"main"|^"VM Thread"|^\d{4}-\d{2}-\d{2}.*\d{2}:\d{2}:\d{2}')
TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}')

DEADLOCK_START_RE = re.compile(r'Found one Java-level deadlock')
DEADLOCK_THREAD_RE = re.compile(r'^"(?P<name>.*?)"')


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

def split_into_snapshots(text: str) -> List[str]:
    """
    Split a file that may contain multiple concatenated thread dumps into
    separate chunks. Uses 'Full thread dump' as the anchor; if that marker
    is absent (some tools omit it) the whole file is treated as one snapshot.

    Any timestamp line that precedes the anchor (jstack often prints one
    immediately before "Full thread dump") is pulled into the following
    chunk so it isn't lost during the split.
    """
    anchor = "Full thread dump"
    if anchor not in text:
        return [text]

    indices = [m.start() for m in re.finditer(re.escape(anchor), text)]
    parts = []
    for i, start in enumerate(indices):
        end = indices[i + 1] if i + 1 < len(indices) else len(text)
        chunk_start = start
        # Look at the line(s) immediately before this anchor for a timestamp,
        # and prepend it to the chunk so parse_snapshot can see it.
        preceding = text[:start]
        prev_lines = preceding.splitlines()
        if prev_lines:
            last_line = prev_lines[-1].strip()
            if TIMESTAMP_RE.match(last_line):
                chunk_start = preceding.rfind(last_line)
        parts.append(text[chunk_start:end])
    return parts


def _finalize_thread(t: Optional[ThreadInfo], threads: List[ThreadInfo]):
    if t is not None and t.name:
        threads.append(t)


def parse_snapshot(chunk: str, index: int) -> Snapshot:
    snap = Snapshot(index=index)

    # Try to find a timestamp line near the top (jstack prints one before "Full thread dump")
    for line in chunk.splitlines()[:5]:
        if TIMESTAMP_RE.match(line.strip()):
            snap.timestamp_line = line.strip()
            break

    lines = chunk.splitlines()
    current: Optional[ThreadInfo] = None
    in_deadlock_block = False
    deadlock_buffer: List[str] = []
    deadlock_threads: List[str] = []

    for raw_line in lines:
        line = raw_line.rstrip()

        # --- Deadlock detection ---
        if DEADLOCK_START_RE.search(line):
            in_deadlock_block = True
            deadlock_buffer = [line]
            deadlock_threads = []
            continue

        if in_deadlock_block:
            deadlock_buffer.append(line)
            dm = DEADLOCK_THREAD_RE.match(line.strip())
            if dm:
                deadlock_threads.append(dm.group("name"))
            # heuristic end-of-block marker used by HotSpot deadlock reports
            if line.strip().startswith("Found") and len(deadlock_buffer) > 1:
                pass
            if line.strip() == "" and len(deadlock_buffer) > 4:
                snap.deadlocks.append(
                    Deadlock(raw_text="\n".join(deadlock_buffer), threads_involved=deadlock_threads)
                )
                in_deadlock_block = False
            continue

        # --- Thread header ---
        if line.startswith('"'):
            _finalize_thread(current, snap.threads)
            name_m = THREAD_NAME_RE.match(line)
            prio_m = PRIO_RE.search(line)
            tid_m = TID_RE.search(line)
            nid_m = NID_RE.search(line)
            current = ThreadInfo(
                name=name_m.group("name") if name_m else line.strip(),
                state="UNKNOWN",
                daemon=bool(DAEMON_RE.search(line)),
                priority=prio_m.group("prio") if prio_m else None,
                tid=tid_m.group("tid") if tid_m else None,
                nid=nid_m.group("nid") if nid_m else None,
                raw_header=line.strip(),
            )
            continue

        if current is None:
            continue

        # --- State ---
        sm = STATE_RE.search(line)
        if sm:
            current.state = sm.group("state")
            continue

        # --- Stack frame ---
        fm = FRAME_RE.match(line)
        if fm:
            current.stack.append(fm.group("frame"))
            continue

        # --- Lock info ---
        lm = LOCKED_RE.match(line)
        if lm:
            current.locked_monitors.append(lm.group("lock").strip())
            continue

        wm = WAITING_LOCK_RE.match(line)
        if wm:
            current.waiting_on = wm.group("lock").strip()
            continue

        pm = PARKING_RE.match(line)
        if pm:
            current.waiting_on = pm.group("lock").strip()
            continue

    _finalize_thread(current, snap.threads)

    # Flush any unterminated deadlock block
    if in_deadlock_block and deadlock_buffer:
        snap.deadlocks.append(
            Deadlock(raw_text="\n".join(deadlock_buffer), threads_involved=deadlock_threads)
        )

    return snap


def parse_thread_dump_file(text: str) -> List[Snapshot]:
    """Entry point: parse raw file text into a list of Snapshot objects."""
    chunks = split_into_snapshots(text)
    snapshots = [parse_snapshot(chunk, i) for i, chunk in enumerate(chunks)]
    # Drop snapshots that yielded zero threads (e.g. stray preamble text)
    return [s for s in snapshots if s.threads]


# ----------------------------------------------------------------------
# Analysis helpers (used by the UI layer)
# ----------------------------------------------------------------------

def top_blocking_frames(snapshot: Snapshot, limit: int = 10) -> List[tuple]:
    """Most common top-of-stack frames among BLOCKED/WAITING threads -> likely contention points."""
    counter = Counter()
    for t in snapshot.threads:
        if t.is_blocked or t.is_waiting:
            counter[t.top_frame] += 1
    return counter.most_common(limit)


def group_by_stack_signature(snapshot: Snapshot, depth: int = 5) -> dict:
    """
    Groups threads that share an identical top-N stack signature.
    Large groups often indicate a pool of threads stuck on the same resource.
    """
    groups: dict = defaultdict(list)
    for t in snapshot.threads:
        sig = tuple(t.stack[:depth])
        groups[sig].append(t.name)
    return {sig: names for sig, names in groups.items() if len(names) > 1}


def thread_pool_buckets(snapshot: Snapshot) -> Counter:
    """
    Buckets threads by inferred 'pool name' (text before trailing -N / #N), useful
    for spotting which pool has runaway thread counts.
    """
    bucket = Counter()
    for t in snapshot.threads:
        base = re.sub(r'[-#]?\d+$', '', t.name).strip()
        bucket[base or t.name] += 1
    return bucket
