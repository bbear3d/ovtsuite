# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scanmatik SM2 Pro crank/cam signal generator ("GPT" outputs).

The SM2 Pro exposes two independent hardware PWM channels through a Scanmatik
*vendor* J2534 ioctl (recovered from ``smj2534.dll``: ``PassThruIoctl`` id
**0x8008**, input a standard ``SCONFIG_LIST`` of ``{Parameter, Value}`` pairs,
parameter ids **0xC001..0xC00A**).  Driving these onto the ECU's crankshaft
(G28) and camshaft (G40) inputs makes an EDC16 believe the engine is turning
-- the "Bosch EDC16 BSM" unlock that brings the MCU/EEPROM subsystem live so
flash *and* EEPROM become readable/writable over K-line.

This module only computes the waveform parameters and pushes them through
``j2534.Channel.set_pwm`` (which marshals the SCONFIG_LIST and issues the
vendor ioctl -- in-process for a same-bitness DLL, or across the 32-bit bridge
for the 32-bit-only Scanmatik DLL).  It holds no ctypes/Windows specifics, so
it imports and unit-tests on any platform.

Signal geometry is the calibration-derived spec for 03G906016LQ / BPW.a2l:

* Crank G28 (ECU pin T60/58): trigger wheel = **60 increments/rev** (6.0 deg
  each), with 2 gaps of 2 missing increments ~180 deg apart (56 physical
  teeth).  Sensor type = **inductive/VR** (adaptive-threshold input), so the
  electrical drive ideally is bipolar/AC -- but the *timing* here (period,
  duty) is independent of the drive electronics.
* Cam G40 (ECU pin T60/28): runs at 1/2 crank speed (1 cam rev / 720 deg
  crank); 7-segment ``dSegSeries`` pattern.  Sensor type = **Hall** (digital,
  level+edge decoded), so a unipolar square with the pull-up enabled.

HARDWARE LIMIT: the SM2 PWM is *uniform* (one period/duty per channel); it has
no missing-tooth/segment engine.  We therefore generate uniform trains first
and read the ECU's measuring blocks (see :func:`engine_speed`) to learn
empirically whether the ECU's BSM gate keys off pulse presence/frequency
(uniform is enough) or needs the gap/segment pattern (which then requires
live reprogramming).  Nothing downstream assumes uniform is sufficient; that
is exactly the question the readback answers.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# --- vendor ioctl + SCONFIG parameter ids (from smj2534.dll RE) --------------
PWM_IOCTL = 0x8008                 # PassThruIoctl id: set PWM / signal generator
# IOCTL 0x800B → USB cmd 0x0053 (formula: USB_CMD = IOCTL - 0x7FB8, confirmed from
# smj2534.dll: "SUB EAX,0x8008" at file offset 0x458EB, all IOCTLs 0x8008-0x800D
# dispatched sequentially through the same SCONFIG_LIST handler fn@0x48320).
# Requires SM2 firmware patched by sm2_patch.py (redirects cmd 0x0053 dispatch
# from the no-op stub to the dead teardown fn at 0x080070B0 that clears DMA1_Ch2
# and DMA1_Ch3 EN bits -- fixing the ERR_DEVICE_NOT_CONNECTED crash after cam GPT).
DMA_TEARDOWN_IOCTL = 0x800B           # IOCTL id: trigger DMA1_Ch2/Ch3 teardown

# Parameter ids 0xC001..0xC00A, in the handler's marshalling order.  Field
# *names* are the DLL's own diagnostic strings; the PWM0-then-PWM1 grouping is
# the natural id order -- confirm against a capture/SM header before relying on
# it in the field, but it is what the recovered handler lays out.
PWM0_PIN            = 0xC001
PWM0_PERIOD_NS      = 0xC002
PWM0_ONTIME_NS      = 0xC003       # high time (duty = ontime/period)
PWM0_OUTPUT_ENABLE  = 0xC004
PWM1_PIN            = 0xC005
PWM1_PERIOD_NS      = 0xC006
PWM1_ONTIME_NS      = 0xC007
PWM1_OFFSET_NS      = 0xC008       # PWM1 phase delay relative to PWM0
PWM1_OUTPUT_ENABLE  = 0xC009
PWM1_PULLUP_ENABLE  = 0xC00A

# --- pin mapping -------------------------------------------------------------
# GPT1 -> OBD pin 1 (SM2 pin 23) -> crank;  GPT2 -> OBD pin 2 (SM2 pin 4) -> cam.
# The exact integer the firmware wants for "pin number" is not proven from the
# DLL (it range-checks against conflicts, not a fixed table), so these are
# defaults to confirm on the bench.  Override via the generator args.
DEFAULT_CRANK_PIN = 1
DEFAULT_CAM_PIN   = 2

# --- trigger-wheel geometry (03G906016LQ / BPW.a2l) --------------------------
CRANK_INCREMENTS_PER_REV = 60      # ctIncs_C (incl. virtual gaps); 6.0 deg/inc
CAM_SPEED_RATIO          = 0.5     # cam turns at half crank speed
CAM_SEGMENTS_PER_CAM_REV = 7       # len(dSegSeries_C) = [6,1,0,5,2,4,3]

_NS = 1_000_000_000


def crank_period_ns(rpm: float, increments_per_rev: int = CRANK_INCREMENTS_PER_REV) -> int:
    """Period of one crank increment (one PWM cycle) at ``rpm``.

    revs/s = rpm/60; increments/s = increments_per_rev * revs/s; the ECU
    reconstructs RPM from this rate using its own increments/rev calibration.
    """
    if rpm <= 0:
        raise ValueError("rpm must be > 0")
    inc_per_s = increments_per_rev * rpm / 60.0
    return int(round(_NS / inc_per_s))


def cam_period_ns(rpm: float, segments_per_cam_rev: int = CAM_SEGMENTS_PER_CAM_REV) -> int:
    """Period of one uniform cam segment at ``rpm`` (cam runs at 1/2 crank).

    cam revs/s = (rpm/60) * CAM_SPEED_RATIO; segments/s = segments * cam revs/s.
    A uniform approximation of the 7-segment wheel -- enough to present cam
    activity at the right mean rate for the recognition test.
    """
    if rpm <= 0:
        raise ValueError("rpm must be > 0")
    cam_rev_per_s = (rpm / 60.0) * CAM_SPEED_RATIO
    seg_per_s = segments_per_cam_rev * cam_rev_per_s
    return int(round(_NS / seg_per_s))


@dataclass
class Pwm:
    """One PWM channel's settings.  Times in nanoseconds."""
    pin: int
    period_ns: int
    ontime_ns: int
    enable: bool = True
    offset_ns: int = 0            # PWM1 only (phase vs PWM0); ignored for PWM0
    pullup: Optional[bool] = None  # PWM1 only; None = leave unset


def duty_ontime(period_ns: int, duty: float = 0.5) -> int:
    return int(round(period_ns * max(0.0, min(1.0, duty))))


def crank_cam_items(rpm: float, *, crank_pin: int = DEFAULT_CRANK_PIN,
                    cam_pin: int = DEFAULT_CAM_PIN, crank_duty: float = 0.5,
                    cam_duty: float = 0.5, cam_phase_deg: float = 45.0,
                    cam_pullup: bool = True,
                    increments_per_rev: int = CRANK_INCREMENTS_PER_REV,
                    crank_only: bool = False
                    ) -> List[Tuple[int, int]]:
    """Build the (param_id, value) list for a crank+cam pair at ``rpm``.

    ``cam_phase_deg`` is the cam-vs-crank phase (default = phiSync2Zero 45 deg),
    converted to an SM2 PWM1 offset in ns against the crank period.

    When ``crank_only=True`` the cam (TIM4/DMA1) channel is omitted entirely.
    This avoids the DMA1 conflict between TIM4 and K-LINE that crashes SM2
    firmware after every GPT session (DMA1_Ch1/Ch4/Ch5 are never torn down
    by the stop sequence, so K-LINE re-initialisation corrupts an active DMA).
    """
    cp = crank_period_ns(rpm, increments_per_rev)
    crank = Pwm(crank_pin, cp, duty_ontime(cp, crank_duty), enable=True)
    if crank_only:
        return items_for(crank, None)
    kp = cam_period_ns(rpm)
    # cam phase in ns: fraction of a crank increment period.
    offset = int(round(cp * (cam_phase_deg / (360.0 / increments_per_rev)))) % max(1, kp)
    cam = Pwm(cam_pin, kp, duty_ontime(kp, cam_duty), enable=True,
              offset_ns=offset, pullup=cam_pullup)
    return items_for(crank, cam)


def items_for(crank: Pwm, cam: Optional[Pwm]) -> List[Tuple[int, int]]:
    """Marshal one or two :class:`Pwm` into the 0xC001.. SCONFIG pairs."""
    items: List[Tuple[int, int]] = [
        (PWM0_PIN, crank.pin),
        (PWM0_PERIOD_NS, crank.period_ns),
        (PWM0_ONTIME_NS, crank.ontime_ns),
        (PWM0_OUTPUT_ENABLE, 1 if crank.enable else 0),
    ]
    if cam is not None:
        items += [
            (PWM1_PIN, cam.pin),
            (PWM1_PERIOD_NS, cam.period_ns),
            (PWM1_ONTIME_NS, cam.ontime_ns),
            (PWM1_OFFSET_NS, cam.offset_ns),
            (PWM1_OUTPUT_ENABLE, 1 if cam.enable else 0),
        ]
        if cam.pullup is not None:
            items.append((PWM1_PULLUP_ENABLE, 1 if cam.pullup else 0))
    return items


def stop_items(crank_pin: int = DEFAULT_CRANK_PIN,
               cam_pin: int = DEFAULT_CAM_PIN,
               crank_only: bool = False) -> List[Tuple[int, int]]:
    """Disable PWM outputs.

    When ``crank_only=True`` only PWM0 (crank) is disabled; PWM1 (cam)
    params are omitted because the cam channel was never started.
    """
    items = [(PWM0_PIN, crank_pin), (PWM0_OUTPUT_ENABLE, 0)]
    if not crank_only:
        items += [(PWM1_PIN, cam_pin), (PWM1_OUTPUT_ENABLE, 0)]
    return items


# --- driving a live channel --------------------------------------------------
def start(channel, rpm: float, **kw) -> List[Tuple[int, int]]:
    """Push a crank+cam pair at ``rpm`` onto an open j2534 channel.

    ``channel`` is a ``j2534.Channel`` / ``BridgeChannel`` (anything with
    ``set_pwm``).  Returns the items sent (for logging)."""
    items = crank_cam_items(rpm, **kw)
    channel.set_pwm(items, ioctl_id=PWM_IOCTL)
    return items


def teardown_dma(channel) -> None:
    """Send cmd 0x0053 to disable DMA1_Ch2 and DMA1_Ch3 (cam GPT channels).

    Must be called after :func:`stop` when ``crank_only=False`` (i.e., whenever
    the cam channel was active).  Requires the SM2 firmware to have been patched
    by :mod:`ovtsuite.flash.sm2_patch`; on unpatched firmware the command hits
    the original no-op stub and returns silently -- no error, no teardown.

    Calling with an empty SCONFIG_LIST is intentional: IOCTL 0x800B carries no
    parameters; the firmware handler (post-patch) ignores the list and goes
    straight to the DMA register writes.
    """
    channel.set_pwm([], ioctl_id=DMA_TEARDOWN_IOCTL)


def stop(channel, crank_pin: int = DEFAULT_CRANK_PIN, cam_pin: int = DEFAULT_CAM_PIN,
         crank_only: bool = False):
    """Disable GPT outputs (both channels, or crank only when ``crank_only=True``).

    When ``crank_only=False`` (the default -- cam channel was active) this also
    calls :func:`teardown_dma` after a short delay to let the SM2 background task
    finish processing the stop command before the DMA channels are torn down.
    On unpatched firmware the teardown call is a harmless no-op.
    """
    channel.set_pwm(stop_items(crank_pin, cam_pin, crank_only=crank_only), ioctl_id=PWM_IOCTL)
    if not crank_only:
        # Give the SM2 background task ~50 ms to process the stop ring-buffer
        # entry before we tear down the DMA channels underneath it.
        _time.sleep(0.05)
        try:
            teardown_dma(channel)
        except Exception:
            # Don't let a teardown IOCTL failure (e.g. on unpatched firmware
            # variants that return an error instead of silently no-oping) abort
            # the caller's flow.  The crash is survivable; it just requires a
            # power-cycle.  Log suppressed to avoid noise in the GUI.
            pass


# --- readback: does the ECU think it is turning? -----------------------------
# Measuring block group 1 field 1 on VAG EDC16 is engine speed (decode formula
# 1 = 0.2*a*b rpm, handled by diag.decode_cell).  A non-zero, roughly-target
# RPM here is the direct confirmation the ECU recognises the crank signal.
RPM_GROUP = 1


def engine_speed(diag, group: int = RPM_GROUP) -> Optional[float]:
    """Read the RPM measuring block via an open :class:`diag.DiagSession`.

    Returns engine speed in rpm, or None if the group is unavailable / has no
    rpm cell.  Does not raise on an empty group."""
    cells = diag.read_group(group)
    if not cells:
        return None
    for c in cells:
        if c.unit == "rpm":
            return c.value
    # fall back to the first numeric cell
    for c in cells:
        if c.value is not None:
            return c.value
    return None


# --- gap-pattern simulation (60-2 missing-tooth emulation) -------------------
import threading as _threading
import time as _time


def gap_thread_start(channel, rpm: float, *,
                     crank_pin: int = DEFAULT_CRANK_PIN,
                     cam_pin: int = DEFAULT_CAM_PIN,
                     crank_duty: float = 0.5,
                     cam_duty: float = 0.5,
                     cam_phase_deg: float = 45.0,
                     cam_pullup: bool = True,
                     increments_per_rev: int = CRANK_INCREMENTS_PER_REV,
                     crank_only: bool = False):
    """Simulate the 60-2 missing-tooth pattern by rapidly alternating set_pwm.

    The SM2 GPT only supports *uniform* PWM (no native missing-tooth engine).
    This thread alternates between two IOCTL calls:

    * **Normal phase** -- standard tooth period for ``increments_per_rev - 2``
      consecutive teeth (58 teeth for a 60-2 wheel, ~68 ms at 850 rpm).
    * **Gap phase** -- crank period = 3 × tooth period for one pulse, which
      the EDC16 crank decoder sees as a period ratio > 2.5 x average and
      classifies as the missing-tooth gap, resynchronising.

    Python timing is imprecise (~1-15 ms resolution), which widens the gap
    beyond the ideal 3T -- but the period ratio stays well above the detection
    threshold and well below the sync-loss timeout (hundreds of ms).

    The first call in the thread measures the round-trip IOCTL latency so it
    can be subtracted from subsequent sleep intervals.

    Parameters
    ----------
    channel : j2534.Channel or BridgeChannel
        Already-open J2534 channel (must have ``set_pwm``).
    rpm : float
        Target engine speed.
    crank_pin, cam_pin : int
        SM2 GPT output pin numbers.

    Returns
    -------
    (thread, stop_event)
        ``thread`` is a daemon thread already started.
        Call ``stop_event.set()`` to stop the thread.
    """
    T_ns            = crank_period_ns(rpm, increments_per_rev)
    gap_period_ns   = 3 * T_ns                             # gap = 3 × tooth period
    normal_teeth    = increments_per_rev - 2               # 58 for 60-2
    normal_duration = normal_teeth * T_ns / _NS            # seconds
    gap_duration    = gap_period_ns / _NS                  # seconds (~3.5 ms at 850 rpm)

    # Normal-phase items (crank + optional cam)
    normal_items = crank_cam_items(
        rpm, crank_pin=crank_pin, cam_pin=cam_pin,
        crank_duty=crank_duty, cam_duty=cam_duty,
        cam_phase_deg=cam_phase_deg, cam_pullup=cam_pullup,
        increments_per_rev=increments_per_rev, crank_only=crank_only)

    # Gap-phase items: crank period = 3 × tooth, cam unchanged
    gap_crank = Pwm(crank_pin, gap_period_ns, duty_ontime(gap_period_ns, crank_duty), enable=True)
    if crank_only:
        gap_items = items_for(gap_crank, None)
    else:
        kp = cam_period_ns(rpm)
        gap_offset = int(round(gap_period_ns * (cam_phase_deg / (360.0 / increments_per_rev)))) % max(1, kp)
        gap_cam = Pwm(cam_pin, kp, duty_ontime(kp, cam_duty), enable=True,
                      offset_ns=gap_offset, pullup=cam_pullup)
        gap_items = items_for(gap_crank, gap_cam)

    stop_event = _threading.Event()

    def _run():
        # First call: measure IOCTL latency for subsequent sleep correction.
        t0 = _time.monotonic()
        channel.set_pwm(normal_items, ioctl_id=PWM_IOCTL)
        ioctl_latency = _time.monotonic() - t0

        while not stop_event.is_set():
            # Normal phase: wait 58 tooth-periods, then switch to gap
            stop_event.wait(max(0.0, normal_duration - ioctl_latency))
            if stop_event.is_set():
                break
            channel.set_pwm(gap_items, ioctl_id=PWM_IOCTL)

            # Gap phase: the gap period itself (latency already "spent" in set_pwm)
            stop_event.wait(max(0.0, gap_duration))
            if stop_event.is_set():
                break
            channel.set_pwm(normal_items, ioctl_id=PWM_IOCTL)

    t = _threading.Thread(target=_run, daemon=True, name="sm2_gap_thread")
    t.start()
    return t, stop_event
