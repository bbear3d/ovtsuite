# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scan a vehicle: identify every module, read its faults, into a
:class:`ScanResult`.

On CAN the scan opens one bus (:class:`~.link.Bus`) and, when the address
table names a gateway with an installation list, asks the gateway first:
the list says which modules the car has and at which TP 2.0 addresses, so
the scan visits those -- including modules the table does not know, which
get a row of their own -- and skips the rest. The modules are then scanned
over several TP 2.0 channels at once (``workers``), each on its own listen
id. Without a gateway list the table's rows with a TP 2.0 address are
scanned, still in parallel.

On K-line there is one wire and one session at a time, so the scan is
sequential over every row (the address convention gives each a 5-baud
address).

A module that does not answer is recorded as not reached with the reason,
so the report can say "not reached" rather than drop it. A scan the user
stops keeps the rows it never got to as "not tried" (``reached=None``) and
sets ``ScanResult.stopped``, so a partial scan is never mistaken for a
complete one.
"""
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

from .defs import Resolver, as_int
from .link import Bus, Interface, open_bus, open_link
from .model import ModuleInfo, ScanResult, Vehicle
from .session import ModuleSession

Progress = Callable[[str, int, int], None]      # message, done, total
Log = Callable[[str], None]

#: TP 2.0 channels open at once during a CAN scan
DEFAULT_WORKERS = 4
#: channel-setup wait per attempt during a scan (three attempts): a fitted
#: module answers within milliseconds (the autoscan capture: 1-10 ms), an
#: absent one never does
SCAN_SETUP_TIMEOUT_MS = 500


def scannable(modules: Iterable[dict], transport: str) -> List[dict]:
    out = []
    for m in modules:
        if transport == "can":
            if (m.get("tp20") or {}).get("dest") is None:
                continue
        out.append(m)
    return out


def gateway_row(modules: Iterable[dict]) -> Optional[dict]:
    """The address-table row that serves an installation list, if any."""
    for m in modules:
        gw = m.get("gateway") or {}
        if gw.get("installation_list") and gw.get("installation_list") != "none" \
                and (m.get("tp20") or {}).get("dest") is not None:
            return m
    return None


def rows_from_gateway(lst, table: Iterable[dict]) -> List[dict]:
    """The modules to visit per the gateway: the table row for each installed
    entry (its TP 2.0 dest filled in from the list when the table has none),
    and a synthesised row for an installed module the table does not know."""
    by_addr: Dict[str, dict] = {m["addr"]: m for m in table}
    out = []
    for e in lst.installed:
        row = dict(by_addr.get(e.addr) or {"addr": e.addr, "name": "Module %s (gateway list)" % e.addr,
                                            "source": "gateway"})
        tp20 = dict(row.get("tp20") or {})
        table_dest = as_int(tp20.get("dest"), None)
        if table_dest is None:
            tp20["dest"] = e.dest
            tp20.setdefault("source", "gateway")
        elif table_dest != e.dest:
            row["_dest_conflict"] = "table 0x%02X, gateway 0x%02X" % (table_dest, e.dest)
            tp20["dest"] = e.dest               # the car knows its own addresses
        row["tp20"] = tp20
        row["_gateway"] = e.to_dict()
        out.append(row)
    return out


def scan_vehicle(iface: Interface, transport: str, vehicle: Vehicle, resolver: Resolver,
                 modules: Optional[Iterable[dict]] = None, log: Optional[Log] = None,
                 progress: Optional[Progress] = None, is_stop: Optional[Callable[[], bool]] = None,
                 can_rate: int = 500000, read_faults: bool = True, workers: int = DEFAULT_WORKERS,
                 use_gateway: bool = True, bus: Optional[Bus] = None) -> ScanResult:
    """``bus``: an open :class:`Bus` to use instead of opening ``iface``
    (tests, or a window that already holds the connection); it is not closed."""
    log = log or (lambda s: None)
    progress = progress or (lambda m, d, t: None)
    table = list(modules if modules is not None else resolver.addressing(vehicle.generation or None))
    rows = scannable(table, transport)
    result = ScanResult(vehicle_id=vehicle.id, interface=iface.label, transport=transport,
                        generation=vehicle.generation)
    t0 = time.time()
    if transport != "can":
        _scan_sequential(iface, transport, rows, resolver, result, log, progress, is_stop, can_rate,
                         vehicle.generation, read_faults)
        result.duration_s = time.time() - t0
        progress("done", len(rows), len(rows))
        return result
    own_bus = bus is None
    if own_bus:
        try:
            bus = open_bus(iface, can_rate, log)
        except Exception as ex:
            log("Parallel channels unavailable (%s); scanning one module at a time" % ex)
            _scan_sequential(iface, transport, rows, resolver, result, log, progress, is_stop, can_rate,
                             vehicle.generation, read_faults)
            result.duration_s = time.time() - t0
            progress("done", len(rows), len(rows))
            return result
    try:
        gw = gateway_row(rows) if use_gateway else None
        gw_info = None
        if gw is not None and not (is_stop and is_stop()):
            progress("%s %s: installation list" % (gw.get("addr"), gw.get("name", "")), 0, len(rows))
            gw_info, lst = _read_gateway(bus, gw, resolver, log, is_stop, vehicle.generation, read_faults)
            if lst is not None:
                result.gateway = lst.to_dict()
                result.basis = "gateway"
                rows = [r for r in rows_from_gateway(lst, table) if r["addr"] != gw["addr"]]
                log("Gateway lists %d modules installed; scanning them over up to %d channels"
                    % (len(lst.installed), max(1, workers)))
                for r in rows:
                    if r.get("_dest_conflict"):
                        log("Module %s: TP 2.0 address differs (%s) -- using the gateway's" % (r["addr"], r["_dest_conflict"]))
            else:
                rows = [r for r in rows if r["addr"] != gw["addr"]]
        _scan_parallel(bus, rows, resolver, result, log, progress, is_stop, vehicle.generation,
                       read_faults, max(1, workers), offset=1 if gw_info is not None else 0)
        if gw_info is not None:
            result.modules.insert(0, gw_info)
        result.modules.sort(key=lambda m: m.addr)
    finally:
        if own_bus:
            bus.close()
    result.duration_s = time.time() - t0
    progress("done", len(result.modules), len(result.modules))
    return result


def _read_gateway(bus: Bus, row: dict, resolver: Resolver, log: Log, is_stop, generation: str,
                  read_faults: bool):
    """Identify the gateway, read its list (and faults); never raises."""
    info = ModuleInfo(addr=row.get("addr", "19"), name=row.get("name", ""), transport="can")
    try:
        link = bus.link_for(row, log, setup_timeout_ms=SCAN_SETUP_TIMEOUT_MS)
    except Exception as ex:
        info.reached = False
        info.error = "could not open the link: %s" % ex
        return info, None
    sess = ModuleSession(link, row, resolver, log, generation=generation)
    lst = None
    try:
        sess.connect(is_stop=is_stop)
        try:
            lst = sess.installation_list()
        except Exception as ex:
            log("Installation list not available from %s: %s" % (info.addr, ex))
        if read_faults:
            try:
                sess.read_faults(with_freeze=False)
            except Exception as ex:
                sess.info.error = "faults: %s" % ex
        return sess.info, lst
    except Exception as ex:
        info.reached = False
        info.error = str(ex)
        return info, None
    finally:
        try:
            sess.close()
        except Exception:
            pass


def _not_tried(row: dict, transport: str) -> ModuleInfo:
    info = ModuleInfo(addr=row.get("addr", "00"), name=row.get("name", ""), transport=transport)
    info.reached = None
    info.error = "not tried (scan stopped)"
    if row.get("_gateway"):
        info.extra["gateway_entry"] = row["_gateway"]
    return info


def _scan_sequential(iface, transport, rows, resolver, result, log, progress, is_stop, can_rate,
                     generation, read_faults):
    total = len(rows)
    for i, row in enumerate(rows):
        if is_stop and is_stop():
            result.stopped = True
            result.modules.extend(_not_tried(r, transport) for r in rows[i:])
            log("Scan stopped: %d of %d modules not tried" % (total - i, total))
            break
        progress("%s %s" % (row.get("addr"), row.get("name", "")), i, total)
        result.modules.append(scan_module(iface, transport, row, resolver, log, is_stop,
                                          can_rate, generation, read_faults))


def _scan_parallel(bus: Bus, rows: List[dict], resolver, result, log, progress, is_stop,
                   generation, read_faults, workers: int, offset: int = 0):
    """``rows`` over up to ``workers`` TP 2.0 channels at once; results land
    in ``result.modules`` in address order."""
    lock = threading.Lock()
    pending = list(rows)
    done: Dict[str, ModuleInfo] = {}
    total = len(rows) + offset
    state = {"n": offset}

    def worker():
        while True:
            if is_stop and is_stop():
                return
            with lock:
                if not pending:
                    return
                row = pending.pop(0)
                progress("%s %s" % (row.get("addr"), row.get("name", "")), state["n"], total)
            info = scan_module(None, "can", row, resolver, log, is_stop, 0, generation, read_faults, bus=bus)
            with lock:
                done[row["addr"]] = info
                state["n"] += 1
                progress("%s %s: %s" % (info.addr, info.name, "ok" if info.reached else "not reached"),
                         state["n"], total)

    threads = [threading.Thread(target=worker, name="ovt-diag-scan-%d" % i, daemon=True)
               for i in range(min(workers, len(rows)) or 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if pending and is_stop and is_stop():
        result.stopped = True
        for row in pending:
            done.setdefault(row["addr"], _not_tried(row, "can"))
        log("Scan stopped: %d of %d modules not tried" % (len(pending), total))
    for addr in sorted(done):
        result.modules.append(done[addr])


def scan_module(iface: Optional[Interface], transport: str, row: dict, resolver: Resolver,
                log: Optional[Log] = None, is_stop=None, can_rate: int = 500000,
                generation: str = "", read_faults: bool = True, bus: Optional[Bus] = None) -> ModuleInfo:
    """Identify one module and read its faults; never raises -- a module
    that cannot be reached comes back with ``reached=False`` and ``error``."""
    log = log or (lambda s: None)
    info = ModuleInfo(addr=row.get("addr", "00"), name=row.get("name", ""), transport=transport)
    try:
        if bus is not None:
            link = bus.link_for(row, log, setup_timeout_ms=SCAN_SETUP_TIMEOUT_MS)
        else:
            link = open_link(iface, transport, row, log, can_rate=can_rate)
            tp = getattr(link.k, "tp", None)
            if transport == "can" and tp is not None and hasattr(tp, "poll_timeout_ms"):
                tp.poll_timeout_ms = SCAN_SETUP_TIMEOUT_MS     # an absent module never answers the setup
    except Exception as ex:
        info.reached = False
        info.error = "could not open the link: %s" % ex
        return info
    sess = ModuleSession(link, row, resolver, log, generation=generation)
    try:
        sess.connect(is_stop=is_stop, probe=True)
        if read_faults:
            try:
                # freeze frames are one request per fault: fetched on demand,
                # not for every module of a scan
                sess.read_faults(with_freeze=False)
            except Exception as ex:
                sess.info.error = "faults: %s" % ex
        if row.get("_gateway"):
            sess.info.extra["gateway_entry"] = row["_gateway"]
        return sess.info
    except Exception as ex:
        info.reached = False
        info.error = str(ex)
        if row.get("_gateway"):
            info.extra["gateway_entry"] = row["_gateway"]
        return info
    finally:
        try:
            sess.close()
        except Exception:
            pass
