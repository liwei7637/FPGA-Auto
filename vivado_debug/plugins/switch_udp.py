"""Symmetric UDP traffic recipe; all board-specific meanings live in this plugin."""
from __future__ import annotations

import ipaddress
import time

from ..analysis import check_pair, throughput
from ..common import DebugError, integer, pause, timeout_seconds


def run_recipe(service, params):
    if service.profile.get("plugin") != "switch_udp_v1" or len(service.machine["endpoints"]) != 2:
        raise DebugError("plugin_required", "Recipe requires a two-endpoint switch_udp_v1 profile")
    endpoints = list(service.machine["endpoints"])
    direction = params.get("direction", "duplex")
    if direction not in ("forward", "reverse", "duplex"):
        raise DebugError("invalid_argument", "direction must be forward, reverse or duplex")
    senders = endpoints if direction == "duplex" else [endpoints[direction == "reverse"]]
    payload = integer(params.get("payload_bytes", 1472), "payload_bytes", 32, 1472)
    mode = integer(params.get("mode", 1), "mode", 0, 1)
    count = integer(params.get("packet_limit", "10000"), "packet_limit", 0, 10**12)
    gap = integer(params.get("gap_cycles", 80), "gap_cycles", 0, 0xffffffff)
    seed = integer(params.get("seed", 1), "seed", 0, 0xffffffff)
    session = integer(params.get("session_id", int(service.job.id[-6:], 16)), "session_id", 0, 0xffffffff)
    duration = timeout_seconds(params.get("duration_seconds", 10))
    deadline = time.monotonic() + timeout_seconds(params.get("timeout", 90))
    network = {}
    for ep, binding in service.machine["endpoints"].items():
        try:
            network[ep] = (int(ipaddress.IPv4Address(binding["ip"])), int(binding["mac"].replace(":", ""), 16))
        except (KeyError, ValueError) as exc:
            raise DebugError("machine_invalid", "Recipe requires each endpoint IP/MAC") from exc
    if network[endpoints[0]][0] == network[endpoints[1]][0] or network[endpoints[0]][1] == network[endpoints[1]][1]:
        raise DebugError("machine_invalid", "Both IP and MAC must be unique")
    for ip, mac in network.values():
        if not 0 < mac < (1 << 48) or mac >> 40 & 1:
            raise DebugError("machine_invalid", "Use nonzero unicast MAC addresses")
    cleanup = {}
    touched = False
    stop_attempted = False

    def check_time():
        service.job.check_cancelled()
        if time.monotonic() >= deadline:
            raise DebugError("recipe_timeout", "Traffic recipe deadline expired")

    def status(ep):
        check_time()
        return int(service.read(ep, "traffic", "in0")["decimal"])

    def stop_sources():
        nonlocal stop_attempted
        stop_attempted = True
        service.backend.check = lambda: None
        until = time.monotonic() + 10
        for ep in senders:
            try:
                word = int(service.read(ep, "traffic", "in0")["decimal"])
                if word & 0x30:
                    service.toggle(ep, 2)
                while int(service.read(ep, "traffic", "in0")["decimal"]) & 0x30:
                    if time.monotonic() >= until:
                        raise DebugError("stop_unconfirmed", "STOP has not reached TX idle")
                    time.sleep(0.03)
                cleanup[ep] = "tx_idle_confirmed"
            except Exception as exc:
                cleanup[ep] = "unconfirmed: " + str(exc)
        service.record("stop_sources", "result", {"endpoints": cleanup})
        service.last_cleanup = dict(cleanup)

    try:
        for ep in endpoints:
            if status(ep) & 0x30:
                raise DebugError("traffic_busy", "Source already running before recipe: " + ep)
        for i, ep in enumerate(endpoints):
            peer = endpoints[1 - i]
            current = int(service.read(ep, "traffic", "out13")["decimal"])
            touched = True
            service.apply(ep, "traffic", {"out0": str(network[ep][1]), "out1": str(network[ep][0]),
                                          "out2": str(int(ipaddress.IPv4Address(params.get("gateway", "192.168.0.1")))),
                                          "out3": str(int(ipaddress.IPv4Address(params.get("netmask", "255.255.255.0")))),
                                          "out4": str(network[peer][0]), "out5": 5001, "out6": 5001,
                                          "out7": payload, "out8": mode, "out9": seed, "out10": session,
                                          "out11": str(count), "out12": gap, "out13": (current & 0x1f) | 0x60})
        for ep in endpoints:
            while not status(ep) & 4:
                pause(0.05, check_time)
            if int(service.read(ep, "traffic", "in3")["decimal"]) != network[ep][1] or \
                    int(service.read(ep, "traffic", "in4")["decimal"]) != network[ep][0]:
                raise DebugError("network_readback_mismatch", "Active MAC/IP do not match submitted identity")
            service.toggle(ep, 0)
            if status(ep) & 0xf008 != 8:
                raise DebugError("apply_rejected", "Test configuration was rejected")
            service.toggle(ep, 3)
            if status(ep) & 0xf000:
                raise DebugError("clear_rejected", "Counter clear was rejected")
        for ep in senders:
            service.toggle(ep, 1)
        rates = {}
        if count:
            for ep in senders:
                while status(ep) & 0x60 != 0x40:
                    pause(0.03, check_time)
        else:
            pause(0.05, check_time)
            begin = {ep: service.snapshot(ep, "stream_begin") for ep in endpoints}
            pause(duration, check_time)
            end = {ep: service.snapshot(ep, "stream_end") for ep in endpoints}
            for ep in endpoints:
                if (direction == "duplex" or endpoints[1 - endpoints.index(ep)] in senders):
                    rates[ep] = throughput(begin[ep], end[ep], service.profile["clock_hz"])
        stop_sources()
        if any(v != "tx_idle_confirmed" for v in cleanup.values()):
            raise DebugError("stop_unconfirmed", "Could not confirm all test sources idle")
        service.backend.check = service.job.check_cancelled
        previous = None
        attempt = 0
        while True:
            check_time()
            snapshots = {ep: service.snapshot(ep, f"drain{attempt}") for ep in endpoints}
            counts = tuple(tuple(snapshots[ep]["values"][i] for i in (0, 2, 3, 28)) for ep in endpoints)
            if counts == previous:
                break
            previous = counts
            attempt += 1
            pause(0.05, check_time)
        final = {ep: service.snapshot(ep, "final") for ep in endpoints}
        expected = {ep: str(count if ep in senders else 0) for ep in endpoints} if count else None
        result = check_pair(final, payload, expected)
        return {**result, "recipe": "switch_udp_v1", "direction": direction, "payload_bytes": payload,
                "packet_limit": str(count), "gap_cycles": gap, "snapshots": final,
                "throughput": rates, "cleanup": cleanup, "simultaneous_start": False}
    finally:
        if touched and not stop_attempted:
            stop_sources()
