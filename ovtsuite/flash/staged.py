# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Turn an image into a job the head unit can run on its own.

STAGED mode: this end decides everything derivable from the binary and ships it
as values; the app re-derives nothing.  That is not tidiness -- the laptop has
the image, the knowledge base, the family fingerprints and the layout catalogue,
and the head unit has a cable.  Every decision left to the app would be a second
place for it to be wrong, in the half that is hardest to update.

So the descriptor carries the init strategy (read from the dataset PROJECT code,
not from part-number strings), the session, the per-block addresses INCLUDING the
ones that are not derivable from each other, and the sentence to show when it
finishes.  The image goes with it already encrypted: the transferData cipher
needs no ECU data, costs a second and a half of CPU, and an idle TP 2.0 channel
does not survive that -- which is why ``edc16.write_flash`` encrypts before it
opens the session too.
"""

from . import edc15boot as _boot
from . import loaders as _loaders
from .crypto import encrypt
from .edc15 import (
    EEPROM_SIZE as EDC15_EEPROM_SIZE,
    FLASH_END as EDC15_FLASH_END,
    LOADER_ADDR as _EDC15_LOADER_ADDR,
    FLASH_SIZE as EDC15_FLASH_SIZE,
    KEY3 as EDC15_KEY3,
    PROGRAM_CHUNK as EDC15_PROGRAM_CHUNK,
    SECTOR_READ_BASE,
    SECTOR_WRITE_BASE,
    SECTORS,
    SPEEDS as EDC15_SPEEDS,
    VARIANTS,
    sector_checksum,
)
from .edc16 import (
    CAL_SIZE,
    CAL_START,
    FULL_SIZE,
    SPEEDS,
    _BLOCK,
    fast_prime_from_image,
    image_project,
)

#: Written in the order the reference writes them: block 7 (the tune), then 6.
WRITE_BLOCKS = (7, 6)

#: What to tell the driver afterwards.  An EDC16 NAKs the soft ecuReset (7F 11 90)
#: and wants the ignition off instead; the app should not hold that rule, so the
#: job carries the sentence.
EDC16_COMPLETION = "Write complete. Turn off the ignition for 5 seconds."
EDC15_COMPLETION = "Write complete."

#: Over CAN this ECU refuses a single 512 KB requestUpload -- it drops the session
#: about a minute in -- and MPPS reads it as 8 x 64 KB uploads by address.
CAN_UPLOAD_CHUNK = 0x10000


def _init_strategy(image):
    """slow / prime-then-slow / auto, from the dataset project code.

    P350 accepts a cold slow init; P475 is a later CAN-init family that has to be
    primed with a fast init first.  Anything unrecognised is AUTO: try a cold slow
    init and prime only if the ECU ignores it.  Structural, from the Bosch block
    header -- never a part-number string and never a fixed calibration offset.
    """
    prime = fast_prime_from_image(image)
    if prime is True:
        return "prime-then-slow"
    if prime is False:
        return "slow"
    return "auto"


def _speed_code(speed):
    if isinstance(speed, int):
        return speed
    return SPEEDS[speed][0]


def build_write_job(image, job_id="write", transport="kline", speed="Medium",
                    ecu="edc16u31", blocks=WRITE_BLOCKS, wake_baud=10400):
    """(descriptor, blob) for a staged EDC16 write.

    ``image`` is the full 2 MB image, plaintext.  The blob that comes back is the
    ciphertext of the blocks, in the order they will be written.
    """
    if len(image) < FULL_SIZE:
        raise ValueError("EDC16 write needs a full %d-byte image" % FULL_SIZE)

    payload = bytearray()
    described = []

    for blockno in blocks:
        start, cks_end, erase_end, dl_size = _BLOCK[blockno]
        finish = cks_end + 1
        ciphertext, checksum = encrypt(image, start, finish)

        described.append({
            "addr": start,
            "size": finish - start,
            "offset": len(payload),
            "length": len(ciphertext),
            # Four figures that do NOT follow from each other: on block 7 the
            # checksum span ends at 0x1FDFFF while the erase runs to 0x1FFFFF, so
            # the last 8 KB is erased and not summed.
            "cks_end": cks_end,
            "erase_end": erase_end,
            "dl_size": dl_size,
            # The 16-bit additive sum of the PLAINTEXT -- what the ECU's 0x31 C5
            # routine compares.  The app only ever sees ciphertext, so it could
            # not compute this even if it wanted to.
            "cks": checksum,
        })

        payload += ciphertext

    return {
        "id": job_id,
        "kind": "write",
        "transport": transport,
        "ecu": ecu,
        "init": _init_strategy(image),
        "session": 0x85,
        "speed": _speed_code(speed),
        "wake_baud": wake_baud,
        "completion": EDC16_COMPLETION,
        "blocks": described,
    }, bytes(payload)


def build_read_job(job_id="read", transport="kline", speed="Medium",
                   ecu="edc16u31", upload_chunk=None, wake_baud=10400):
    """A staged EDC16 calibration read."""
    if upload_chunk is None:
        upload_chunk = CAN_UPLOAD_CHUNK if transport == "can" else 0

    return {
        "id": job_id,
        "kind": "read",
        "transport": transport,
        "ecu": ecu,
        # A read is granted over a fast init: the 0x85 slow-init requirement is
        # the DOWNLOAD session's, and this is 0x86.
        "init": "auto",
        "session": 0x86,
        "speed": _speed_code(speed),
        "wake_baud": wake_baud,
        "upload_chunk": upload_chunk,
        "regions": [{"addr": CAL_START, "size": CAL_SIZE}],
    }


# ---------------------------------------------------------------- EDC15

#: The EDC15's two-stage wake-up: KW1281 at 9600 to identify and say goodbye,
#: then KWP2000 at 10400.  Not a strategy to pick between like the EDC16's -- it
#: is what the controller does -- so it is named rather than chosen.
EDC15_INIT = "kw1281-then-kwp"

#: Boot mode has no wake-up to name: the CPU's ROM loader auto-bauds on a single edge
#: and there is no diagnostic session at all.
EDC15_BOOT_INIT = "boot"

EDC15_BOOT_COMPLETION = "Boot-mode flash write complete."

#: Address byte both inits are sent to.
EDC15_ADDRESS = 0x01

EDC15_KWP_BAUD = 10400
EDC15_WAKE_BAUD = 9600


#: The only EDC15 variant this tuner drives.  Every ``edc15`` call site in the flash
#: dialog names it -- flash read, sector write, EEPROM, backup and restore, all eight --
#: and P and VM have never been on a bench.  ``edc15.connect`` still takes ``"Auto"``
#: and still has the table, but nothing here reaches it, and staged mode must not be
#: the one path that quietly does: each extra candidate is a security-access attempt
#: with a WRONG key on a real controller, and some of them count those.
EDC15_VARIANT = "V"


def _edc15_variant(variant):
    """This job's variant and its constants.  One, never a list to try in turn.

    The TABLE stays here: the head unit holding a copy would be a second place for a
    key to be wrong, in the half that is hardest to update.  What travels is the one
    variant's numbers.
    """
    name = variant or EDC15_VARIANT
    if name != EDC15_VARIANT:
        # Named rather than attempted. Sending an unvalidated variant to a head unit
        # would be this end promising something no one has tried on a car, and the
        # controller would be the thing that found out.
        raise ValueError(
            "This tuner only drives EDC15 variant %s; %r has not been validated."
            % (EDC15_VARIANT, name))

    key, _ardu, _ecu = VARIANTS[name]
    return {"name": name, "key": key, "key3": EDC15_KEY3}


def build_edc15_write_job(image, job_id="write", speed="Medium", variant=EDC15_VARIANT,
                          verify=True, force_full=False, transport="kline"):
    """(descriptor, blob) for a staged EDC15 sector write.

    WHY THE SECTOR CHECKSUMS ARE SHIPPED AND THE DECISION IS NOT
    ------------------------------------------------------------
    The EDC15's good write path only erases sectors that differ from the image,
    so an unchanged boot sector is never erased -- which is the single best safety
    property this controller has.  Deciding which those are needs two numbers per
    sector: one from the ECU, one from the image.  Only the head unit can ask the
    ECU; only this end has the image.

    So this end computes all eleven image checksums and ships them, and the head
    unit asks the ECU for its eleven and COMPARES.  A comparison is not a
    derivation: the app still works nothing out for itself, and the alternative --
    shipping the plaintext image and a checksum routine to run over it -- would put
    a second copy of ``sector_checksum`` somewhere it could drift.

    ``force_full`` marks every sector to be written without changing the shape, so
    the head unit runs one path and not two.
    """
    if len(image) != EDC15_FLASH_SIZE:
        # The head unit runs the job autonomously with the sector geometry baked
        # into this descriptor, so it cannot detect a 1 MB part and adapt. A 1 MB
        # EDC15 must be written over a direct connection (which does detect size);
        # refuse here rather than ship 512 KB of geometry for a 1 MB image.
        raise ValueError(
            "Run on the head unit supports 512 KB (0x80000) EDC15 images only; this "
            "image is %d bytes. Write a 1 MB EDC15 over a direct connection." % len(image))

    loader = _loaders.sector_loader()
    described = []

    for index, (start, end) in enumerate(SECTORS):
        described.append({
            "index": index,
            "addr": start,
            "size": end - start,
            # Into the blob, which is the loader followed by the whole image: any
            # sector may turn out to be the one that differs, so all of them travel.
            "offset": len(loader) + start,
            # Three different address windows onto the same flash, and none of them
            # follows from the others: the loader ERASES and PROGRAMS at the CPU bus
            # address (+0x200000) and CHECKSUMS at the read window (+0x080000).
            "write_addr": start + SECTOR_WRITE_BASE,
            "cks_from": start + SECTOR_READ_BASE,
            "cks_to": end + SECTOR_READ_BASE,
            "cks": sector_checksum(image[start:end]),
            "always": bool(force_full),
        })

    job = {
        "id": job_id,
        "kind": "write",
        "ecu": "edc15",
        "transport": transport,
        "init": EDC15_INIT,
        "session": 0x85,
        "speed": _edc15_speed_code(speed),
        "wake_baud": EDC15_WAKE_BAUD,
        "kwp_baud": EDC15_KWP_BAUD,
        "address": EDC15_ADDRESS,
        "variant": _edc15_variant(variant),
        # Already checksum-patched for the boot ROM: `patch_loader` pads, fixes the
        # end pointer and appends the correction, and it is pure computation on
        # bytes this end already holds.
        "loader": {"addr": int.from_bytes(_EDC15_LOADER_ADDR, "big"),
                   "offset": 0, "length": len(loader)},
        "program_chunk": EDC15_PROGRAM_CHUNK,
        "verify": bool(verify),
        "sectors": described,
        "completion": EDC15_COMPLETION,
    }

    return job, loader + bytes(image[:EDC15_FLASH_SIZE])


#: What the flash loader's requestDownload declares, which is NOT its length: the
#: reference asks for 0x420 and sends 1016 bytes plus an 8-byte tail. Shipped as the
#: value it is rather than computed, because the two are simply different numbers.
EDC15_FLASH_LOADER_DOWNLOAD = 0x420

#: The eight bytes that follow the flash loader's last chunk and start it running.
EDC15_FLASH_LOADER_MAGIC = bytes([0xC3, 0xC3, 0x89, 0xC5, 0x45, 0xDA, 0x63, 0x0B])

#: Bytes per read packet, and the address-high offset the loader's Cmd23 wants.
EDC15_READ_PACKET = 0xFE
EDC15_READ_BASE = 0x08


def _edc15_handshake(ardu):
    """How the post-loader handshake is sliced: send N, then verify M.

    Two shapes, and which one is used follows from the variant's own request bytes -
    a nine-byte ``ardu`` is the single-exchange variant (V), anything longer is the
    four-exchange one (P and VM). Structural, from the table, not from a name.
    """
    if len(ardu) == 9:
        return [{"send": 9, "expect": 4}]
    return [{"send": 9, "expect": 4}, {"send": 6, "expect": 18},
            {"send": 6, "expect": 18}, {"send": 9, "expect": 2}]


def _edc15_read_variant(variant):
    """Like :func:`_edc15_variant`, plus the handshake the FLASH loader needs.

    The sector loader is uploaded and simply runs; the flash loader answers with a
    per-variant exchange whose request bytes and expected replies are in the same
    table the key is.  The head unit compares what it is given against what it is
    told to expect - it never has to know which variant this is.
    """
    described = _edc15_variant(variant)
    _key, ardu, ecu = VARIANTS[described["name"]]
    described["ardu"] = ardu.hex()
    described["ecu"] = ecu.hex()
    described["handshake"] = _edc15_handshake(ardu)
    return described


def build_edc15_read_job(job_id="read", speed="Medium", variant=EDC15_VARIANT,
                         transport="kline"):
    """(descriptor, blob) for a staged EDC15 whole-chip read.

    STAGED BECAUSE THE THIN PATH CANNOT CARRY IT, not only because it is safer.
    After the loader starts, the controller stops speaking KWP2000 and the framing
    becomes a plain length byte - and ``RemoteKLine`` assembles outgoing bytes into
    KWP2000 frames, where the top bit of that length byte means "addressed". Any
    loader message longer than 0x3F is mis-framed. The short ones line up by
    coincidence, which is why it looks like it works.

    On top of that the flash loader's handshake is written a byte at a time with
    inter-byte pauses, and the read is ~2100 solicited packets. Both of those belong
    on the cable's own end of the link whatever the framing did.
    """
    loader = _loaders.flash_loader()

    return {
        "id": job_id,
        "kind": "read",
        "ecu": "edc15",
        "transport": transport,
        "init": EDC15_INIT,
        "session": 0x85,
        "speed": _edc15_speed_code(speed),
        "wake_baud": EDC15_WAKE_BAUD,
        "kwp_baud": EDC15_KWP_BAUD,
        "address": EDC15_ADDRESS,
        "variant": _edc15_read_variant(variant),
        "loader": {
            "addr": int.from_bytes(_EDC15_LOADER_ADDR, "big"),
            "offset": 0,
            "length": len(loader),
            # Declared and sent are different numbers here; see the constant.
            "declared": EDC15_FLASH_LOADER_DOWNLOAD,
            "magic": EDC15_FLASH_LOADER_MAGIC.hex(),
        },
        "read": {
            "packet": EDC15_READ_PACKET,
            "base": EDC15_READ_BASE,
            "end": EDC15_FLASH_END,
            # The last two reads are not on the address ladder: the loop stops at
            # 0x7FEE2 and the remaining 0x11E bytes come out of one fixed request.
            # Built here, where the arithmetic that produced it lives.
            "tail": _edc15_tail().hex(),
        },
        "regions": [{"addr": 0, "size": EDC15_FLASH_SIZE}],
    }, loader


#: The EEPROM loader's command set.  Dump the whole part; write a batched run below
#: 0x100 with one command and above it with another -- a different command, not a flag,
#: so a run may never cross the boundary.
EDC15_EEPROM_DUMP = 0xA6
EDC15_EEPROM_PAGE0 = 0xA9
EDC15_EEPROM_PAGE1 = 0xAA

#: Most pairs in one run.  The header carries the count in a single byte.
EDC15_EEPROM_MAX_RUN = 255

EDC15_EEPROM_COMPLETION = "EEPROM written and verified."


def _edc15_eeprom_plan():
    return {
        "size": EDC15_EEPROM_SIZE,
        "dump": EDC15_EEPROM_DUMP,
        "page0": EDC15_EEPROM_PAGE0,
        "page1": EDC15_EEPROM_PAGE1,
        "max_run": EDC15_EEPROM_MAX_RUN,
    }


def _edc15_eeprom_job(kind, job_id, variant, speed):
    """The half an EEPROM read and an EEPROM write have in common.

    Both upload the same loader and then speak its command set; only the direction
    and what travels with it differ.  Speed stays at 10400 -- the reference does not
    negotiate for EEPROM work, and 512 bytes are not worth the renegotiation.
    """
    loader = _loaders.eeprom_loader()

    return {
        "id": job_id,
        "kind": kind,
        "ecu": "edc15",
        "transport": "kline",
        "init": EDC15_INIT,
        "session": 0x85,
        "speed": 0,
        "wake_baud": EDC15_WAKE_BAUD,
        "kwp_baud": EDC15_KWP_BAUD,
        "address": EDC15_ADDRESS,
        "variant": _edc15_variant(variant),
        "loader": {
            "addr": int.from_bytes(_EDC15_LOADER_ADDR, "big"),
            "offset": 0,
            "length": len(loader),
        },
        "eeprom": _edc15_eeprom_plan(),
    }, loader


def build_edc15_eeprom_read_job(job_id="ee_read", variant=EDC15_VARIANT, speed="Low"):
    """(descriptor, blob) for a staged EDC15 EEPROM read.

    STAGED FOR THE SAME REASON THE FLASH LOADER IS, and less obviously.  The dump is a
    framed loader command whose 512 bytes of answer are RAW -- no length byte, no
    checksum -- and ``RemoteKLine`` assembles outgoing bytes into KWP2000 frames, so the
    conversation only survives as long as nothing unframed has to cross.  It does.
    """
    job, loader = _edc15_eeprom_job("read", job_id, variant, speed)
    job["regions"] = [{"addr": 0, "size": EDC15_EEPROM_SIZE}]
    return job, loader


def build_edc15_eeprom_write_job(eeprom, job_id="ee_write", variant=EDC15_VARIANT,
                                 speed="Low"):
    """(descriptor, blob) for a staged EDC15 EEPROM write.

    The blob is the loader followed by the 512 bytes wanted in the part.  The head unit
    dumps what is there, works out the difference and writes only that: an EEPROM byte
    rewritten with the value it already holds costs a write cycle on a part that has a
    limited number of them.  Comparing is not deriving, which is why it happens there.

    The WRITE is the half thin mode mangles outright: a run is a framed header and then
    up to 510 raw address/value pairs, and a run of pairs has no length byte for the
    laptop's framing to find -- so it invents one out of the first pair's address.
    """
    eeprom = bytes(eeprom)
    if len(eeprom) != EDC15_EEPROM_SIZE:
        raise ValueError("EDC15 EEPROM image must be %d bytes; got %d"
                         % (EDC15_EEPROM_SIZE, len(eeprom)))

    job, loader = _edc15_eeprom_job("write", job_id, variant, speed)
    job["completion"] = EDC15_EEPROM_COMPLETION
    return job, loader + eeprom


# ---------------------------------------------------------------- boot mode


def _boot_plan(registers, stage1, monitor, helper=None):
    plan = {
        "baud": _boot.BAUD,
        "cpu": _boot.CPU_C167,
        "chunk": _boot.CHUNK,
        "flash_base": _boot.FLASH_CPU_BASE,
        "helper_ram": _boot.HELPER_RAM,
        "helper_entry": _boot.HELPER_ENTRY,
        "program_buf": _boot.PROGRAM_BUF,
        "write_seg": _boot.WRITE_SEG_BASE,
        "read_seg": _boot.READ_SEG_BASE,
        # The hello is waiting for a person to bridge a pin and turn a key, not for a
        # controller to think.  A diagnostic timeout here would fail every attempt made
        # by somebody working alone.
        "hello_ms": 20000,
        "regs": [{"addr": a, "value": v} for a, v in registers],
        "stage1": {"offset": 0, "length": len(stage1)},
        "monitor": {"offset": len(stage1), "length": len(monitor)},
    }
    blob = stage1 + monitor
    if helper is not None:
        plan["helper"] = {"offset": len(blob), "length": len(helper)}
        blob += helper
    return plan, blob


def _boot_job(kind, job_id, plan):
    return {
        "id": job_id,
        "kind": kind,
        "ecu": "edc15",
        "transport": "kline",
        # Boot mode bypasses the diagnostics entirely: there is no wake-up, no session
        # and no security access, because there may be no firmware left to run any of
        # them.  The init is named for what it is so nothing downstream assumes one.
        "init": EDC15_BOOT_INIT,
        "session": 0,
        "speed": 0,
        "wake_baud": _boot.BAUD,
        "kwp_baud": _boot.BAUD,
        "address": EDC15_ADDRESS,
        "boot": plan,
        "regions": [{"addr": 0, "size": _boot.FLASH_SIZE}],
    }


def build_edc15_boot_read_job(job_id="read"):
    """(descriptor, blob) for a staged boot-mode whole-chip read.

    THE PATH THAT WORKS WHEN THE FIRMWARE IS GONE, and the one thin mode fails at most
    completely.  Nothing in boot mode is framed -- commands are single bytes, addresses
    three bytes little-endian -- and ``RemoteKLine`` reads a one-byte command as a
    KWP2000 format byte declaring a 32-byte frame, so it buffers it waiting for
    thirty-three more that never come.  The auto-baud byte that starts the conversation
    is swallowed, and it fails as a timeout indistinguishable from an unbridged pin.
    """
    plan, blob = _boot_plan(
        _boot.READ_MODE_REGS, _loaders.boot_stage1(), _loaders.boot_monitor())
    return _boot_job("read", job_id, plan), blob


def build_edc15_boot_write_job(image, job_id="write"):
    """(descriptor, blob) for a staged boot-mode erase-and-write.

    The whole chip, because the helper's erase takes the whole chip: anything less than
    a full image would leave the rest erased.  The image follows the three blobs in the
    payload.
    """
    image = bytes(image)
    if len(image) != _boot.FLASH_SIZE:
        raise ValueError("boot-mode write needs exactly %d bytes; got %d"
                         % (_boot.FLASH_SIZE, len(image)))

    plan, blob = _boot_plan(
        _boot.WRITE_MODE_REGS, _loaders.boot_stage1(), _loaders.boot_monitor(),
        _loaders.boot_flash_helper())
    job = _boot_job("write", job_id, plan)
    job["completion"] = EDC15_BOOT_COMPLETION
    return job, blob + image


def _edc15_tail():
    request = bytearray([0x05, 0x23,
                         ((EDC15_FLASH_END >> 16) & 0xFF) + EDC15_READ_BASE,
                         0xFF, 0xE0, 0x20])
    request.append(sum(request) & 0xFF)
    return bytes(request)


def _edc15_speed_code(speed):
    """0 means stay at 10400 -- the EDC15's Low, which is a real choice here and
    not an absent one, so it is a number rather than a missing member."""
    if isinstance(speed, int):
        return speed
    selection = EDC15_SPEEDS.get(speed)
    return selection[0] if selection else 0


def assemble_read(data):
    """Pad a staged read's regions back into the full 2 MB image.

    The head unit hands back the regions and nothing else, deliberately: padding
    is a statement about one controller's memory map, and this end is where the
    model that knows it lives.  Matches what ``edc16.read_flash`` returns.
    """
    image = bytearray(b"\xFF" * CAL_START)
    image += data
    if len(image) < FULL_SIZE:
        image += b"\xFF" * (FULL_SIZE - len(image))
    del image[FULL_SIZE:]
    return bytes(image)


def describe(job):
    """One line for a log or a dialog, from the descriptor alone."""
    if job.get("kind") == "read" and not job.get("eeprom") and not job.get("boot"):
        total = sum(r["size"] for r in job.get("regions", []))
        return "read %d KB over %s" % (total // 1024, job.get("transport"))
    if job.get("boot"):
        if job.get("kind") == "read":
            return "read the whole chip in boot mode"
        return "erase and write the whole chip in boot mode"

    if job.get("eeprom"):
        size = job["eeprom"]["size"]
        if job.get("kind") == "read":
            return "read the %d-byte EEPROM" % size
        return "write the %d-byte EEPROM, only what differs" % size

    if job.get("sectors"):
        # An EDC15 write is described by what it MIGHT write, because which
        # sectors actually differ is not known until the ECU has been asked.
        sectors = job["sectors"]
        total = sum(s["size"] for s in sectors)
        return "write up to %d sectors (%d KB) over %s, %s" % (
            len(sectors), total // 1024, job.get("transport"),
            "every sector" if all(s.get("always") for s in sectors)
            else "only what differs")

    total = sum(b["length"] for b in job.get("blocks", []))
    return "write %d blocks (%d KB) over %s, init %s" % (
        len(job.get("blocks", [])), total // 1024,
        job.get("transport"), job.get("init"))


__all__ = [
    "build_write_job",
    "build_read_job",
    "build_edc15_write_job",
    "build_edc15_read_job",
    "build_edc15_boot_read_job",
    "build_edc15_boot_write_job",
    "build_edc15_eeprom_read_job",
    "build_edc15_eeprom_write_job",
    "EDC15_COMPLETION",
    "EDC15_VARIANT",
    "EDC15_INIT",
    "assemble_read",
    "describe",
    "EDC16_COMPLETION",
    "CAN_UPLOAD_CHUNK",
]
