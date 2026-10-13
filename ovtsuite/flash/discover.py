# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Find KW-Dash head units on whatever network this laptop is on.

WHY A SWEEP AND NOT A BEACON
----------------------------
Three topologies have to work, and only one method survives all three:

* both machines on the car's or the garage's Wi-Fi;
* the HEAD UNIT hot-spotting and this laptop joined to it -- the common case in a
  car, and the one where the head unit is this machine's default gateway, so it
  is found by a single connect rather than a scan;
* this LAPTOP hot-spotting and the head unit joined to it, where the head unit is
  a DHCP client somewhere in the hotspot's own /24.

A UDP broadcast beacon works in all three too, but Android drops non-directed
broadcast unless the app holds a ``MulticastLock``, and it means a second wire
format saying the same things as the greeting -- a second place to be wrong.
mDNS is worse: access points routinely drop multicast, and Windows' hotspot with
Bonjour is a known misery.

So discovery is the PROTOCOL ITSELF, asked of many addresses at once.  What the
list shows -- the cable, whether it can do CAN or a 5-baud init, its speed cap,
whether this laptop is already paired with it -- is what the real ``hello``
answered, not what an advertisement claimed.  The two cannot disagree, because
there is only one of them.

WHAT A PROBE COSTS THE HEAD UNIT
--------------------------------
Nothing that shows.  A greeting does not claim the cable, so a head unit driving
gauges keeps driving them; it issues a nonce and closes.  That is why this can be
run from a dialog without asking first.
"""

import socket
import threading

from . import headunits

#: Addresses tried per sweep, past the gateways.  A /24 because that is what an
#: Android hotspot (192.168.43.0/24), a Windows hotspot (192.168.137.0/24) and
#: essentially every domestic access point hand out -- and because a wider prefix
#: could not be swept in a dialog's worth of time anyway.  It is a bound on the
#: work, not a claim about the netmask; anything it misses, the address box takes.
HOSTS = 254

#: One probe.  Long enough for a head unit that is awake on the same Wi-Fi to
#: answer, short enough that 254 of them in parallel finish while the user is
#: still looking at the dialog.
PROBE_TIMEOUT = 0.6

#: How many probes are in the air at once.
WORKERS = 64


class Found(object):
    """A head unit that answered, and what it said about itself."""

    def __init__(self, host, port, capabilities):
        self.host = host
        self.port = int(port)
        self.cable = capabilities.get("cable", "")
        self.can = bool(capabilities.get("can", False))
        self.five_baud = bool(capabilities.get("five_baud", True))
        self.max_speed = int(capabilities.get("max_speed", 0x50))
        #: Whether THIS laptop is already paired with it -- the head unit answers
        #: that against the client id in the greeting, so it is per-laptop.
        self.paired = bool(capabilities.get("paired", False))

    @property
    def label(self):
        state = "paired" if self.paired else "needs pairing"
        where = "%s:%d" % (self.host, self.port)
        return "%s — %s (%s)" % (where, self.cable or "no cable", state)

    def as_unit(self, name=""):
        """A :class:`headunits.HeadUnit` for it, with no token: pair to get one."""
        return headunits.HeadUnit(name or self.host, self.host, self.port,
                                  b"", self.cable, self.can, self.five_baud,
                                  self.max_speed)

    def __repr__(self):
        return "<Found %s>" % self.label


def probe(host, port=headunits.DEFAULT_PORT, timeout=PROBE_TIMEOUT, context=None):
    """Greet one address.  Returns a :class:`Found`, or None for anything else.

    Every failure is the same answer here -- refused, timed out, something else
    listening on the port, a version this build cannot speak -- because to a
    driver scanning a subnet they all mean "not a head unit I can use", and
    telling them apart would only fill the list with noise.
    """
    from .remote import BridgeClient

    client = BridgeClient(host, port, timeout=timeout,
                          ssl_context=headunits._context(context))
    try:
        capabilities = client.connect()
    except Exception:                                   # noqa: BLE001 - see above
        return None
    finally:
        try:
            client.close()
        except Exception:                               # noqa: BLE001
            pass

    return Found(host, port, capabilities)


def local_addresses():
    """This machine's own IPv4 addresses, as the OS routes them.

    The UDP socket is never sent on: ``connect`` on a datagram socket only picks
    a route and binds a source address, which is exactly the question being
    asked -- "if I spoke to something out there, who would I be?".  Asking the OS
    beats ``gethostbyname(gethostname())``, which on Windows answers with
    whatever the hosts file and the DNS suffix conspire to produce.
    """
    out = []
    for target in ("8.8.8.8", "192.168.1.1", "10.0.0.1"):
        probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe_socket.connect((target, 9))
            address = probe_socket.getsockname()[0]
        except OSError:
            continue
        finally:
            probe_socket.close()
        if address and not address.startswith("127.") and address not in out:
            out.append(address)
    return out


def default_gateways():
    """Default gateways, from the OS routing table.

    Worth the platform code for one topology: when the head unit is the hotspot
    it IS the gateway, so this turns a 254-address sweep into a single connect.
    Every reader returns nothing rather than raising -- a sweep that has to find
    the gateway the slow way still finds it, since the gateway is inside the /24.
    """
    out = []

    # Linux, and the head unit itself: /proc/net/route is fixed-width hex, little
    # endian, and a default route is the row whose destination is 00000000.
    try:
        with open("/proc/net/route", "r", encoding="ascii") as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    packed = int(fields[2], 16).to_bytes(4, "little")
                    out.append(socket.inet_ntoa(packed))
    except (OSError, ValueError, StopIteration):
        pass

    if out:
        return out

    try:
        import ctypes
        import ctypes.wintypes
    except ImportError:
        return out

    if not hasattr(ctypes, "windll"):
        return out

    # Windows: GetBestRoute to a public address returns the row the stack would
    # actually use, next hop included. Reading the structure rather than parsing
    # `route print`, whose columns are localised and whose format is not a
    # contract.
    class _Row(ctypes.Structure):
        _fields_ = [("dwForwardDest", ctypes.c_ulong),
                    ("dwForwardMask", ctypes.c_ulong),
                    ("dwForwardPolicy", ctypes.c_ulong),
                    ("dwForwardNextHop", ctypes.c_ulong),
                    ("dwForwardIfIndex", ctypes.c_ulong),
                    ("dwForwardType", ctypes.c_ulong),
                    ("dwForwardProto", ctypes.c_ulong),
                    ("dwForwardAge", ctypes.c_ulong),
                    ("dwForwardNextHopAS", ctypes.c_ulong),
                    ("dwForwardMetric1", ctypes.c_ulong),
                    ("dwForwardMetric2", ctypes.c_ulong),
                    ("dwForwardMetric3", ctypes.c_ulong),
                    ("dwForwardMetric4", ctypes.c_ulong),
                    ("dwForwardMetric5", ctypes.c_ulong)]

    row = _Row()
    try:
        destination = int.from_bytes(socket.inet_aton("8.8.8.8"), "little")
        if ctypes.windll.iphlpapi.GetBestRoute(destination, 0, ctypes.byref(row)) == 0:
            hop = socket.inet_ntoa(int(row.dwForwardNextHop).to_bytes(4, "little"))
            if hop != "0.0.0.0":
                out.append(hop)
    except (OSError, AttributeError, ValueError):
        pass

    return out


def candidates():
    """Every address worth probing, gateways first, without duplicates.

    Gateways lead because of the hot-spotting head unit: in that topology the
    first address tried is the answer.
    """
    seen = []

    def add(address):
        if address and address not in seen:
            seen.append(address)

    for gateway in default_gateways():
        add(gateway)

    for address in local_addresses():
        head = address.rsplit(".", 1)[0]
        for last in range(1, HOSTS + 1):
            candidate = "%s.%d" % (head, last)
            if candidate != address:                    # not ourselves
                add(candidate)

    return seen


def sweep(port=headunits.DEFAULT_PORT, timeout=PROBE_TIMEOUT, workers=WORKERS,
          context=None, on_found=None, should_stop=None):
    """Probe the local network and return what answered, in the order found.

    ``on_found`` is called from a worker thread as each one answers, so a dialog
    can fill its list while the rest of the sweep is still running; ``should_stop``
    is polled between probes so Cancel does not wait for 254 timeouts.
    """
    addresses = candidates()
    found = []
    lock = threading.Lock()
    index = [0]

    def worker():
        while True:
            if should_stop is not None and should_stop():
                return
            with lock:
                if index[0] >= len(addresses):
                    return
                host = addresses[index[0]]
                index[0] += 1

            unit = probe(host, port, timeout, context)
            if unit is None:
                continue

            with lock:
                found.append(unit)
            if on_found is not None:
                on_found(unit)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(workers, len(addresses) or 1)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return found


__all__ = ["Found", "probe", "sweep", "candidates", "local_addresses",
           "default_gateways", "HOSTS", "PROBE_TIMEOUT", "WORKERS"]
