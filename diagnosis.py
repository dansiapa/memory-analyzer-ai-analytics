"""
diagnosis.py
Turns a parsed Snapshot into a plain-English verdict: what's actually wrong,
what's the likely root cause / blocking thread, and how severe it is.

This is the "so what" layer on top of parser.py's raw extraction — the same
kind of synthesis a human would do after staring at a thread dump: not just
"here are the threads", but "here is the one thread holding everyone up".

Design notes:
- Lock addresses (e.g. "<0x000000061ff12345>") are used to cross-reference
  WHO holds a lock that others are waiting on. That's how we name an actual
  culprit thread instead of just reporting symptoms.
- Findings are returned ordered by severity (CRITICAL first), so the UI can
  just render them top to bottom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from parser import Snapshot, ThreadInfo, group_by_stack_signature, thread_pool_buckets


SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Frame substrings that suggest the thread is blocked waiting on an external
# dependency rather than purely internal application logic / lock contention.
EXTERNAL_IO_HINTS = [
    ("socketread", "network socket read"),
    ("socketwrite", "network socket write"),
    ("socketconnect", "network connect"),
    ("sockinputstream", "network socket"),
    ("sockoutputstream", "network socket"),
    ("java.sql.", "JDBC / database call"),
    ("jdbc", "JDBC / database call"),
    ("hikari", "HikariCP connection pool"),
    ("c3p0", "c3p0 connection pool"),
    ("mongodb", "MongoDB driver"),
    ("mongo.", "MongoDB driver"),
    ("redis", "Redis client"),
    ("jedis", "Redis client (Jedis)"),
    ("lettuce", "Redis client (Lettuce)"),
    ("kafka", "Kafka client"),
    ("amqp", "RabbitMQ / AMQP client"),
    ("rabbitmq", "RabbitMQ client"),
    ("httpclient", "outbound HTTP call"),
    ("okhttp", "outbound HTTP call (OkHttp)"),
    ("apache.http", "outbound HTTP call (Apache HttpClient)"),
    ("filereader", "file I/O"),
    ("fileinputstream", "file I/O"),
    ("filewriter", "file I/O"),
    ("nio.channels", "NIO channel I/O"),
    ("dnsresolve", "DNS resolution"),
    ("inetaddress", "DNS resolution"),
]

# Frame substrings suggesting GC-related stalls (not always present in thread
# dumps, but worth flagging if a dedicated GC thread looks abnormal).
LOCK_ADDR_RE = re.compile(r'<(0x[0-9a-fA-F]+)>')


@dataclass
class Finding:
    severity: str            # CRITICAL, HIGH, MEDIUM, LOW, INFO
    title: str
    detail: str
    affected_threads: List[str] = field(default_factory=list)

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 99)


@dataclass
class Diagnosis:
    verdict: str                     # one-line plain-English summary
    is_healthy: bool
    findings: List[Finding] = field(default_factory=list)

    @property
    def top_finding(self) -> Optional[Finding]:
        return self.findings[0] if self.findings else None


def _extract_lock_addr(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = LOCK_ADDR_RE.search(text)
    return m.group(1) if m else None


def _classify_external_io(frame: str) -> Optional[str]:
    lower = frame.lower()
    for hint, label in EXTERNAL_IO_HINTS:
        if hint in lower:
            return label
    return None


def _find_lock_holder(lock_addr: str, threads: List[ThreadInfo]) -> Optional[ThreadInfo]:
    for t in threads:
        for locked in t.locked_monitors:
            if _extract_lock_addr(locked) == lock_addr:
                return t
    return None


def diagnose(snapshot: Snapshot) -> Diagnosis:
    """
    Main entry point. Runs a series of rule-based checks against a Snapshot
    and returns a Diagnosis with a one-line verdict plus ranked findings.
    """
    findings: List[Finding] = []
    threads = snapshot.threads
    total = len(threads) or 1

    # ------------------------------------------------------------------
    # 1. Deadlocks — always the most severe possible finding.
    # ------------------------------------------------------------------
    if snapshot.deadlocks:
        for i, dl in enumerate(snapshot.deadlocks, 1):
            findings.append(Finding(
                severity="CRITICAL",
                title=f"Deadlock detected (cycle #{i})",
                detail=(
                    f"{len(dl.threads_involved)} threads are permanently stuck in a circular lock wait: "
                    f"{', '.join(dl.threads_involved)}. None of these threads can ever proceed without "
                    f"intervention (a restart). This alone is enough to explain a hung or unresponsive service."
                ),
                affected_threads=dl.threads_involved,
            ))

    # ------------------------------------------------------------------
    # 2. Lock-holder analysis — find threads that are BLOCKING many others
    #    even without a formal deadlock cycle (the classic "one slow thread
    #    holds a lock, twenty request threads pile up behind it" pattern).
    # ------------------------------------------------------------------
    blocked = [t for t in threads if t.is_blocked]
    waiting_addr_count: dict = {}
    for t in blocked:
        addr = _extract_lock_addr(t.waiting_on)
        if addr:
            waiting_addr_count.setdefault(addr, []).append(t)

    for addr, waiters in sorted(waiting_addr_count.items(), key=lambda kv: -len(kv[1])):
        if len(waiters) < 2:
            continue
        holder = _find_lock_holder(addr, threads)
        holder_desc = f'"{holder.name}" (state: {holder.state})' if holder else "a thread not captured in this dump (it may have already finished, or the dump caught it mid-transition)"
        severity = "CRITICAL" if len(waiters) >= 5 else "HIGH"
        findings.append(Finding(
            severity=severity,
            title=f"{len(waiters)} threads blocked waiting on the same lock",
            detail=(
                f"{len(waiters)} threads ({', '.join(w.name for w in waiters[:8])}"
                f"{' ...' if len(waiters) > 8 else ''}) are all BLOCKED waiting to acquire lock {addr}, "
                f"currently held by {holder_desc}. "
                + (f'"{holder.name}" is the likely root cause — until it releases this lock, '
                   f"all {len(waiters)} waiting threads stay stuck." if holder else
                   "Re-capture a dump while the issue is happening to catch the holder thread in the act.")
            ),
            affected_threads=[w.name for w in waiters] + ([holder.name] if holder else []),
        ))

    # ------------------------------------------------------------------
    # 3. Thread pool saturation — e.g. all (or nearly all) of a request-
    #    handling pool (http-nio-exec, tomcat, jetty, etc.) is stuck.
    # ------------------------------------------------------------------
    pools = thread_pool_buckets(snapshot)
    REQUEST_POOL_HINTS = ["http", "exec", "tomcat", "jetty", "undertow", "grizzly", "worker", "request"]
    for pool_name, pool_total in pools.items():
        if pool_total < 3:
            continue
        lname = pool_name.lower()
        if not any(hint in lname for hint in REQUEST_POOL_HINTS):
            continue
        pool_threads = [t for t in threads if t.name.startswith(pool_name)]
        stuck = [t for t in pool_threads if t.is_blocked or t.is_waiting]
        if pool_total and len(stuck) / pool_total >= 0.6:
            severity = "CRITICAL" if len(stuck) / pool_total >= 0.9 else "HIGH"
            findings.append(Finding(
                severity=severity,
                title=f"Request-handling pool '{pool_name}' is saturated",
                detail=(
                    f"{len(stuck)} of {pool_total} threads in the '{pool_name}' pool are BLOCKED or WAITING, "
                    f"not RUNNABLE. This pool typically handles incoming requests — if nearly all of its "
                    f"threads are stuck, the service has little or no capacity left to accept new traffic, "
                    f"which presents to users as hanging requests, timeouts, or connection refusals."
                ),
                affected_threads=[t.name for t in stuck],
            ))

    # ------------------------------------------------------------------
    # 4. External dependency stalls — many threads parked in JDBC/socket/
    #    HTTP-client/etc. frames, suggesting a slow or hung downstream
    #    dependency rather than an application bug.
    # ------------------------------------------------------------------
    io_groups: dict = {}
    for t in threads:
        if not (t.is_blocked or t.is_waiting):
            continue
        for frame in t.stack[:6]:
            label = _classify_external_io(frame)
            if label:
                io_groups.setdefault(label, []).append(t.name)
                break

    for label, names in sorted(io_groups.items(), key=lambda kv: -len(kv[1])):
        if len(names) < 2:
            continue
        severity = "HIGH" if len(names) >= 5 else "MEDIUM"
        findings.append(Finding(
            severity=severity,
            title=f"{len(names)} threads stalled on {label}",
            detail=(
                f"{len(names)} threads ({', '.join(names[:8])}{' ...' if len(names) > 8 else ''}) are "
                f"stuck waiting on {label}. This points to a slow, hung, or exhausted downstream dependency "
                f"(e.g. a database, cache, or remote API) rather than a bug inside the application itself. "
                f"Check the health and connection-pool sizing of that dependency."
            ),
            affected_threads=names,
        ))

    # ------------------------------------------------------------------
    # 5. Large groups of threads stuck on an identical stack — generic
    #    catch-all for pool exhaustion / stuck resource patterns not
    #    covered by the more specific rules above.
    # ------------------------------------------------------------------
    groups = group_by_stack_signature(snapshot, depth=4)
    flagged_names = {n for f in findings for n in f.affected_threads}
    for sig, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(names) < 4:
            continue
        if set(names).issubset(flagged_names):
            continue  # already explained by a more specific finding above
        top = sig[0] if sig else "(empty stack)"
        findings.append(Finding(
            severity="MEDIUM",
            title=f"{len(names)} threads stuck at the same code location",
            detail=(
                f"{len(names)} threads ({', '.join(names[:8])}{' ...' if len(names) > 8 else ''}) "
                f"share an identical stack, all currently at: {top}. "
                f"A large group piled up at one location usually means that location is a bottleneck — "
                f"worth checking what resource or lock it's waiting on."
            ),
            affected_threads=names,
        ))

    # ------------------------------------------------------------------
    # 6. High overall blocked/waiting ratio with no other specific finding
    #    — a softer, catch-all signal that something's off.
    # ------------------------------------------------------------------
    blocked_or_waiting = sum(1 for t in threads if t.is_blocked or t.is_waiting)
    ratio = blocked_or_waiting / total
    if not findings and ratio >= 0.5:
        findings.append(Finding(
            severity="MEDIUM",
            title="High proportion of non-runnable threads",
            detail=(
                f"{blocked_or_waiting} of {total} threads ({ratio*100:.0f}%) are BLOCKED or WAITING. "
                f"No single dominant cause was identified automatically — review the Threads tab, "
                f"sorted by state, to inspect what each group is waiting on."
            ),
            affected_threads=[],
        ))

    # ------------------------------------------------------------------
    # Sort findings by severity, build the one-line verdict.
    # ------------------------------------------------------------------
    findings.sort(key=lambda f: f.severity_rank)

    if not findings:
        verdict = "No stopper issue found — thread states look healthy."
        is_healthy = True
    else:
        top = findings[0]
        is_healthy = False
        if top.severity == "CRITICAL" and "Deadlock" in top.title:
            verdict = f"STOPPER: {top.title}. {top.affected_threads[0] if top.affected_threads else 'Multiple threads'} and {len(top.affected_threads)-1 if len(top.affected_threads) > 1 else 0} other thread(s) are permanently stuck."
        else:
            verdict = f"Likely root cause: {top.title}."

    return Diagnosis(verdict=verdict, is_healthy=is_healthy, findings=findings)
