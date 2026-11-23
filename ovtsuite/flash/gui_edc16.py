# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flasher's EDC16 operations beyond Read / Write calibration: Read full
and Write full (the blocks picked in gui_edc16_blocks), and the research
probes.

:class:`gui_flash.FlashDialog` and its worker call in here at three points --
``prepare`` (before the job: confirm, collect what it needs), ``run`` (on the
worker thread) and ``done`` (show the result) -- so the dialog carries no
EDC16 branches of its own beyond the plain Read / Write.
"""
import datetime

from PySide6 import QtWidgets

from . import edc16_blocks as B

#: Show the research probes (Advanced). They answered their questions while
#: the flasher was being built (see edc16_probes); off, they stay in the code
#: but the flasher does not offer them.
RESEARCH_TOOLS = False

#: block-selected operations (Read full / Write full: gui_edc16_blocks)
BLOCK_OPS = ("read_program_full", "write_program")

#: research op -> (title, confirmation text or None)
RESEARCH_OPS = {
    "boot_read_probe": (
        "Boot read-level probe",
        "Non-destructive: open the read session and try requestUpload of the "
        "boot/low region (0x10000) at LVL3 (read) and LVL1 (write) to find which "
        "security level unlocks reading boot. 0x40000 is the readable control.\n\n"
        "No data is transferred, nothing is written.\n\nProceed?"),
    "checksum_range_probe": (
        "Checksum-range probe",
        "Non-destructive: open the programming session and run 0x31 C5 checksum "
        "over candidate ranges to find which the ECU will VERIFY (73 C5) vs reject "
        "(7F 33 22) -- especially the 0xA0000+ region.\n\n"
        "Nothing is erased, written or transferred.\n\nProceed?"),
    "security_seed_probe": (
        "Security level probe",
        "Non-destructive: connect and send SecurityAccess seed requests "
        "(0x27 01/03/05/07/...) with NO key, to enumerate which security levels "
        "the ECU has. No key is sent, nothing unlocks, nothing is written."
        "\n\nProceed?"),
    "ram_read_probe": ("Read RAM / coding (probe)", None),
    "immo_coding_read": ("Read immo/coding", None),
    "find_eeprom": ("Find EEPROM", None),
    "gpt_verify": ("GPT crank/cam test", None),
    "gpt_verify_and_find": ("GPT -> Find EEPROM", None),
}

#: EDC16 ops that also run over CAN/TP 2.0 (the rest need K-line)
CAN_OPS = BLOCK_OPS + ("boot_read_probe", "checksum_range_probe",
                       "security_seed_probe", "ram_read_probe")

#: (attribute, label, op or None, tooltip) of the research controls
_RESEARCH_BUTTONS = (
    ("prog_bootprobe_btn", "Boot read-level probe", "boot_read_probe",
     "Find which security level unlocks READING the boot/low region: requestUpload "
     "(0x10000) at LVL3 (read) and LVL1 (write), 0x40000 as the readable control."),
    ("prog_cksumprobe_btn", "Checksum-range probe", "checksum_range_probe",
     "Run 0x31 C5 over candidate ranges in the programming session to find which "
     "the ECU will verify. No erase, no write, no transfer."),
    ("prog_seedprobe_btn", "Security level probe", "security_seed_probe",
     "Send 0x27 seed requests with NO key and record which return a seed."),
    ("prog_ramread_btn", "Read RAM / coding (probe)", "ram_read_probe",
     "readMemoryByAddress (0x23) of a RAM address at each security level held; "
     "the coding struct 0x7FE9E4 by default (decodes K5)."),
    ("prog_immoread_btn", "Read immo/coding (L3)", "immo_coding_read",
     "Read the coding struct, coding mirror and ImmCtl RAM state over 0x23 and "
     "decode the immobiliser status byte. Nothing is written."),
    ("prog_findeep_btn", "Find EEPROM (L3)", "find_eeprom",
     "Sweep RAM over 0x23 for the 4 KB EEPROM (or its shadow) by its block "
     "checksums. Reads only."),
    ("prog_gptverify_btn", "GPT crank/cam test", "gpt_verify",
     "Scanmatik SM2: drive the crank/cam inputs and read the engine-speed block "
     "to confirm the ECU sees them."),
    ("prog_gptverify_find_btn", "GPT → Find EEPROM", "gpt_verify_and_find",
     "Scanmatik SM2: crank/cam signals, confirm RPM, then sweep for the EEPROM."),
)
_RESEARCH_CHECKS = (
    ("gpt_crank_only_chk", "Crank only", "_gpt_crank_only", True,
     "Skip the cam (TIM4) channel: the SM2 firmware leaves DMA1 set up after GPT "
     "and crashes on the next PassThruOpen."),
    ("gpt_gap_chk", "Gap pattern (60-2)", "_gpt_gap_pattern", False,
     "Alternate the PWM period to simulate the 60-2 missing tooth."),
    ("gpt_force_ee_chk", "Force EEPROM sweep", "_gpt_force_eeprom", False,
     "Sweep for the EEPROM even when the ECU reported no RPM."),
)
#: every research control, by attribute name
RESEARCH_WIDGETS = (tuple(a for a, *_ in _RESEARCH_BUTTONS)
                    + tuple(a for a, *_ in _RESEARCH_CHECKS))


def make_research_widgets(dlg):
    """Create the research controls as attributes of ``dlg`` (hidden; the
    dialog places them in Advanced and shows them only with RESEARCH_TOOLS)."""
    for attr, label, op, tip in _RESEARCH_BUTTONS:
        w = QtWidgets.QPushButton(label)
        w.setToolTip("EDC16 research probe (non-destructive). " + tip)
        w.clicked.connect(lambda _c=False, op=op: dlg._start(op))
        w.setVisible(False)
        setattr(dlg, attr, w)
    for attr, label, flag, default, tip in _RESEARCH_CHECKS:
        w = QtWidgets.QCheckBox(label)
        w.setChecked(default)
        w.setToolTip(tip)
        w.toggled.connect(lambda v, flag=flag: setattr(dlg, flag, v))
        w.setVisible(False)
        setattr(dlg, attr, w)


def research_visible(is_edc16) -> bool:
    return bool(is_edc16 and RESEARCH_TOOLS)


# ---------------------------------------------------------------- the dialog
def prepare(dlg, op):
    """Confirm and collect what ``op`` needs. Returns the worker's ``edc16``
    options (and may set ``image``), or None when the user cancelled."""
    warn = lambda text: QtWidgets.QMessageBox.warning(dlg, "Flashing", text)  # noqa: E731
    ask = lambda title, text: QtWidgets.QMessageBox.question(dlg, title, text) \
        == QtWidgets.QMessageBox.StandardButton.Yes                          # noqa: E731
    if dlg._ecu() != "EDC16":
        warn("This operation is EDC16 only.")
        return None
    if op == "read_program_full":
        return _prepare_read(dlg, warn)
    if op == "write_program":
        return _prepare_write(dlg, warn, ask)
    if op not in RESEARCH_OPS:
        return {}
    title, text = RESEARCH_OPS[op]
    opts = {}
    if op == "checksum_range_probe":
        try:
            opts["image"] = dlg._prepare_write_image()
        except Exception:
            opts["image"] = None
    if op == "ram_read_probe":
        txt, ok = QtWidgets.QInputDialog.getText(
            dlg, title, "24-bit RAM address and byte count (hex), e.g. 7FE9E4 40.\n"
            "Default reads the coding struct and decodes the level-5 K5.",
            text="7FE9E4 40")
        if not ok:
            return None
        try:
            parts = txt.replace(",", " ").split()
            opts["ram_addr"] = int(parts[0], 16) & 0xFFFFFF
            opts["ram_size"] = ((int(parts[1], 16) if len(parts) > 1 else 0x40) & 0xFF) or 0x40
        except Exception:
            warn("Couldn't parse the address/size.")
            return None
        dlg._ram_probe_addr = opts["ram_addr"]
    if text and not ask(title, text):
        return None
    return opts


def _prepare_read(dlg, warn):
    from . import gui_edc16_blocks as _pick
    sel = _pick.pick(dlg, _pick.READ)
    if not sel:
        return None                     # cancelled (the picker needs one)
    return {"blocks": sel}


def _prepare_write(dlg, warn, ask):
    from . import gui_edc16_blocks as _pick
    from .gui_flash import _WriteCancelled
    from ..modules import registry as _registry
    src_image, src_mpc = dlg._block_sources()
    avail = B.available(src_image, src_mpc)
    if not any(avail.values()):
        warn("Open the file or project to write first: the one in front holds "
             "no EDC16 flash blocks.")
        return None
    sel = _pick.pick(dlg, _pick.WRITE, avail)
    if not sel:
        return None
    missing = [B.get(k).label for k in sel if not avail.get(k)]
    if missing:
        warn("The loaded file holds no data for: %s. Untick %s to write the rest."
             % (", ".join(missing), "it" if len(missing) == 1 else "them"))
        return None
    try:
        image = dlg._prepare_write_image()       # corrects external incl program sig
    except _WriteCancelled:
        return None
    except Exception as ex:
        QtWidgets.QMessageBox.critical(dlg, "Flashing", str(ex))
        return None
    opts = {"blocks": sel, "image": image,
            "program_end": dlg._source_program_end()}
    if "mpc" in sel:
        try:
            mpc_image, src = dlg._source_mpc_image()
            if mpc_image is not None and src == "dim":
                # a .dim may be edited: correct the MPC checksum/signature with
                # the external image as the key (.sgo / .edc16 are final)
                cs = _registry.checksum("edc16")
                if cs is not None:
                    mpc_image = cs.correct(mpc_image, key_image=image)
        except Exception as ex:
            warn("Couldn't prepare the MPC internal flash from the loaded source "
                 "(%s)." % ex)
            return None
        opts["mpc_image"] = mpc_image
    labels = ", ".join(B.get(k).label.lower() for k in B.UI_ORDER if k in sel)
    boot = ("\n\n*** BOOT BLOCK INCLUDED ***\nErases and rewrites boot "
            "(0x10000-0x3FEFF) FIRST. HIGHEST brick risk -- a failed boot erase "
            "needs BDM recovery." if "boot" in sel else "")
    skip = ("" if dlg.force_full.isChecked() else
            "\n\nEach block is pre-verified (0x31 C5) and SKIPPED if the ECU already "
            "holds it; tick Force full write to rewrite regardless.")
    if not ask("Write full — ARM",
               "DESTRUCTIVE: this ERASES and REPROGRAMS these blocks: %s." % labels
               + boot + skip + "\n\nA failed or interrupted erase/write can leave the "
               "ECU unbootable -- recover via BDM.\n\nProceed with the armed write?"):
        return None
    return opts


# ------------------------------------------------------------ worker thread
def run(worker, flash, allow_fast, fast_init_prime):
    """Run EDC16 ``worker._op`` on ``flash``; returns the result to hand back."""
    from . import edc16_probes as P
    from . import edc16_read, edc16_write
    op, opts = worker._op, worker._edc16 or {}
    stage, stop = worker.stage.emit, worker._stop.is_set
    common = dict(speed=worker._speed, allow_fast=allow_fast, on_stage=stage, is_stop=stop)
    if op == "write_program":
        edc16_write.write_blocks(
            flash, worker._image, opts.get("mpc_image"), blocks=opts["blocks"],
            arm=True, force_full=worker._force_full, pre_verify=True,
            program_end=opts.get("program_end"), fast_init_prime=fast_init_prime,
            on_percent=worker.percent.emit, **common)
        return None
    if op == "read_program_full":
        out = edc16_read.read_blocks(flash, opts["blocks"], on_percent=worker.percent.emit,
                                     **common)
        out["blocks"] = list(opts["blocks"])
        return out
    if op == "boot_read_probe":
        return P.boot_read_probe(flash, **common)
    if op == "checksum_range_probe":
        return P.checksum_range_probe(flash, worker._image, **common)
    if op == "security_seed_probe":
        return P.security_seed_probe(flash, **common)
    if op == "ram_read_probe":
        return P.ram_read_probe(flash, addr=opts.get("ram_addr", P.CODING_STRUCT_ADDR),
                                size=opts.get("ram_size", 0x40), **common)
    if op == "immo_coding_read":
        return P.read_immo_coding(flash, **common)
    if op == "find_eeprom":
        return P.find_eeprom(flash, **common)
    gpt = dict(rpm=getattr(worker, "_gpt_rpm", 850),
               use_gap_pattern=getattr(worker, "_gpt_gap_pattern", False),
               crank_only=getattr(worker, "_gpt_crank_only", True),
               on_stage=stage, is_stop=stop)
    if op == "gpt_verify":
        return P.gpt_verify_running(flash, stop_after=not getattr(worker, "_gpt_keep", False),
                                    **gpt)
    if op == "gpt_verify_and_find":
        return P.gpt_verify_and_find(
            flash, force_eeprom=getattr(worker, "_gpt_force_eeprom", False), **gpt)
    raise ValueError("unknown EDC16 operation %r" % op)


# --------------------------------------------------------------- results
def done(dlg, op, result) -> bool:
    """Show ``op``'s result. False when ``op`` is not one of ours."""
    info = lambda title, text: QtWidgets.QMessageBox.information(dlg, title, text)  # noqa: E731
    if op == "write_program":
        dlg.stage.setText("Write full complete.")
        dlg._flashed("write full")
        info("Write full",
             "Selected blocks written and verified (blocks already matching were "
             "skipped). Turn the ignition off for 5 seconds. If the ECU does not "
             "start, restore via BDM. See the log above for per-block results.")
        return True
    if op == "read_program_full":
        _done_read(dlg, result if isinstance(result, dict) else {}, info)
        return True
    shown = _RESULTS.get(op)
    if shown is None:
        return False
    title = RESEARCH_OPS[op][0]
    dlg.stage.setText("%s complete (no flash modified)." % title)
    info(title, shown(dlg, result))
    return True


def _done_read(dlg, res, info):
    from .. import edc16_container as _cont
    ext, mpc = res.get("external"), res.get("internal")
    blocks = res.get("blocks") or []
    container = _cont.Edc16Container()
    if ext:
        container.add(_cont.BLOCK_EXTERNAL, ext)
    if mpc:
        container.add(_cont.BLOCK_MPC, mpc)    # at its default address
    if not container.blocks:
        dlg.stage.setText("Read full complete -- nothing to save.")
        info("Read full", "Read complete; nothing to save.")
        return
    # one line in the stage label; a box only for what the user must know
    summary = "Read full complete -- %s." % " ".join(container.summary().split())
    dlg.stage.setText(summary)
    warn = ""
    bootv = res.get("boot_virtual")
    if bootv and bootv.get("quality") != "exact":
        warn = ("Boot: VIRTUAL %s match from %s (approximate -- correct code, "
                "reference SW id/signature)." % (bootv.get("quality"), bootv.get("source")))
    elif not bootv and "boot" in blocks:
        warn = "Boot: left 0xFF (unreadable over OBD, no boot-DB match)."
    if res.get("padded"):
        warn = (warn + "\n\n" if warn else "") + (
            "%d range(s) the ECU refused were left 0xFF (see the log)."
            % len(res["padded"]))
    if warn:
        QtWidgets.QMessageBox.warning(dlg, "Read full",
                                      "Read complete, with a caveat:\n\n" + warn)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # into a project, like every read (external + MPC as one set); the flasher
    # closes first so the prompt is not left behind it
    dlg._hand_over(dlg._window.load_from_image, container.to_bytes(),
                   "edc16-full-%s" % stamp)


def _mark(v):
    return "READ" if v else ("-" if v is None else "no")


def _show_boot_probe(_dlg, res):
    res = res if isinstance(res, dict) else {}
    lines, hit = [], []
    for lvl, row in res.items():
        lines.append("  %s:  boot[0x35=%s 0x23=%s]  program[0x35=%s 0x23=%s]"
                     % (lvl, _mark(row.get("boot")), _mark(row.get("boot_rma")),
                        _mark(row.get("program")), _mark(row.get("program_rma"))))
        if row.get("boot"):
            hit.append("%s via upload(0x35)" % lvl)
        if row.get("boot_rma"):
            hit.append("%s via readMemoryByAddress(0x23)" % lvl)
    verdict = ("Boot IS readable -- %s." % "; ".join(hit) if hit else
               "Boot refused by BOTH read services (0x35 upload and 0x23 "
               "readMemoryByAddress) at every level tested.")
    return ("0x35 upload vs 0x23 readMemoryByAddress, per level:\n\n%s\n\n%s"
            % ("\n".join(lines) or "  (nothing probed)", verdict))


def _show_checksum_probe(_dlg, res):
    res = res if isinstance(res, list) else []
    lines = ["  0x%06X-0x%06X  %-34s -> %s" % r for r in res]
    acc = [r for r in res if str(r[3]).startswith("accepted")]
    return ("%s\n\nThe ECU verifies %d of %d probed ranges. 'accepted' = the 0x31 C5 "
            "routine ran over that range; 'rejected:7F 33 22' = not a verifiable range."
            % ("\n".join(lines) or "  (nothing probed)", len(acc), len(res)))


def _show_seed_probe(_dlg, res):
    res = res if isinstance(res, list) else []
    lines, live = [], []
    for sf, verdict in res:
        known = {0x01: " write/SA2", 0x03: " read"}.get(sf, "")
        lines.append("  27 %02X%s -> %s" % (sf, known, verdict))
        if str(verdict).startswith(("seed", "open")):
            live.append("27 %02X" % sf)
    return ("%s\n\nLive security levels: %s. Known: 27 01=write/SA2, 27 03=read. "
            "No key was sent." % ("\n".join(lines) or "  (nothing probed)",
                                  ", ".join(live) or "none beyond the standard set"))


def _show_ram_probe(dlg, res):
    res = res if isinstance(res, dict) else {}
    addr = getattr(dlg, "_ram_probe_addr", 0x7FE9E4)
    lines, readable, k5 = [], [], None
    for lvl, row in res.items():
        data = row.get("data") if isinstance(row, dict) else None
        if data:
            readable.append(lvl)
            lines.append("  [%s] 0x%06X: %s" % (lvl, addr, bytes(data).hex()))
            if row.get("k5") is not None:
                k5 = row["k5"]
        else:
            err = row.get("error") if isinstance(row, dict) else "?"
            lines.append("  [%s] refused (%s)" % (lvl, err))
    if k5 is not None:
        verdict = "K5 = 0x%04X  =>  level-5 key = (seed + 0x%04X) & 0xFFFFFFFF." % (k5, k5)
    elif readable:
        verdict = "RAM is readable over OBD at %s." % ", ".join(readable)
    else:
        verdict = "RAM read refused at every level held."
    return "%s\n\n%s" % ("\n".join(lines) or "  (nothing read)", verdict)


def _show_dict(_dlg, res):
    if not isinstance(res, dict):
        return str(res)
    return "\n".join("%s: %s" % (k, v if not isinstance(v, bytes) else v.hex())
                     for k, v in res.items() if k != "items") or "(no result)"


_RESULTS = {
    "boot_read_probe": _show_boot_probe,
    "checksum_range_probe": _show_checksum_probe,
    "security_seed_probe": _show_seed_probe,
    "ram_read_probe": _show_ram_probe,
    "immo_coding_read": _show_dict,
    "find_eeprom": _show_dict,
    "gpt_verify": _show_dict,
    "gpt_verify_and_find": _show_dict,
}
