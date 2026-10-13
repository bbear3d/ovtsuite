# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Staged jobs from this end: the descriptor, the paired-head-unit store, the client.

What these pin is the half OVT-Suite owns. The head unit's half -- validating the
descriptor, running the state machines -- is the C# one, and the two are checked
against each other directly by the cross-language run rather than by a mock
agreeing with itself.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import headunits, staged  # noqa: E402
from ovtsuite.flash.edc16 import CAL_SIZE, CAL_START, FULL_SIZE, _BLOCK  # noqa: E402
from ovtsuite.flash.remote import BridgeClient, BridgeError  # noqa: E402
from mock_bridge import MockBridge  # noqa: E402


@pytest.fixture
def image():
    data = bytearray(b"\xFF" * FULL_SIZE)
    for i in range(CAL_SIZE):
        data[CAL_START + i] = (i * 7) & 0xFF
    return bytes(data)


@pytest.fixture
def bridge():
    server = MockBridge()
    try:
        yield server
    finally:
        server.close()


def _paired(bridge):
    client = BridgeClient("127.0.0.1", bridge.port, timeout=5.0)
    client.connect()
    client.pair(bridge.pin)
    client.claim()
    return client


def test_a_write_descriptor_carries_what_cannot_be_derived(image):
    job, blob = staged.build_write_job(image)

    assert job["kind"] == "write"
    assert job["session"] == 0x85, "a write wants the download session"
    assert len(job["blocks"]) == 2

    seven, six = job["blocks"]
    assert (seven["addr"], six["addr"]) == (0x1C0000, 0x180000), "block 7 first, as the reference does"

    # The four figures that do NOT follow from each other. On block 7 the checksum
    # span stops at 0x1FDFFF while the erase runs to 0x1FFFFF - the last 8 KB is
    # erased and not summed, and an app deriving one from the other gets it wrong.
    assert seven["cks_end"] == 0x1FDFFF
    assert seven["erase_end"] == 0x1FFFFF
    assert seven["cks_end"] != seven["erase_end"]
    assert six["cks_end"] == six["erase_end"] == 0x1BFFFF

    for block, number in ((seven, 7), (six, 6)):
        start, cks_end, erase_end, dl_size = _BLOCK[number]
        assert (block["addr"], block["cks_end"], block["erase_end"], block["dl_size"]) == (
            start, cks_end, erase_end, dl_size), "straight from the reference's own table"

    # The blob is the blocks' ciphertext, laid out exactly where the offsets say.
    assert len(blob) == seven["length"] + six["length"]
    assert seven["offset"] == 0
    assert six["offset"] == seven["length"]
    assert blob[six["offset"]:six["offset"] + six["length"]] != image[0x180000:0x1C0000], \
        "what goes over the wire is ciphertext, not the image"

    assert job["completion"].lower().startswith("write complete"), \
        "the app should not hold the ignition rule; the job carries the sentence"


def test_the_init_strategy_is_read_from_the_image(image):
    """P350 accepts a cold slow init, P475 has to be primed. Structural, from the
    dataset project code - never a part-number string."""
    assert staged.build_write_job(image)[0]["init"] in ("slow", "prime-then-slow", "auto")


def test_a_read_descriptor_chunks_only_over_can():
    kline = staged.build_read_job(transport="kline")
    can = staged.build_read_job(transport="can")

    assert kline["session"] == 0x86, "a read is 0x86, which has no 5-baud requirement"
    assert kline["upload_chunk"] == 0, "K-line takes the whole region in one upload"

    # Over CAN this ECU refuses a single 512 KB requestUpload and drops the session
    # about a minute in; MPPS reads it as 8 x 64 KB by address.
    assert can["upload_chunk"] == staged.CAN_UPLOAD_CHUNK == 0x10000
    assert kline["regions"] == [{"addr": CAL_START, "size": CAL_SIZE}]


def test_padding_happens_on_this_end(image):
    """The head unit hands back the regions and nothing else: padding is a statement
    about one controller's memory map, and the model that knows it lives here."""
    regions = image[CAL_START:FULL_SIZE]
    assembled = staged.assemble_read(regions)

    assert len(assembled) == FULL_SIZE
    assert assembled == image
    assert assembled[:CAL_START] == b"\xFF" * CAL_START


def test_head_units_round_trip(tmp_path):
    unit = headunits.HeadUnit("Golf", "192.168.1.42", 35510, bytes(range(32)),
                              cable="ch34x", can=False, five_baud=True, max_speed=0x87)
    headunits.remember(unit, str(tmp_path))

    back = headunits.load(str(tmp_path))
    assert len(back) == 1
    assert back[0].token == unit.token, "the token survives as BYTES, which is why this is not QSettings"
    assert back[0].max_speed == 0x87
    assert "pass-through" in back[0].label

    # Same host and port replaces rather than duplicating - a re-pair is not a second head unit.
    headunits.remember(headunits.HeadUnit("Golf", "192.168.1.42", 35510, b"\x01" * 32),
                       str(tmp_path))
    assert len(headunits.load(str(tmp_path))) == 1

    headunits.forget("192.168.1.42", 35510, str(tmp_path))
    assert headunits.load(str(tmp_path)) == []


def test_a_damaged_entry_costs_one_head_unit_not_the_file(tmp_path):
    path = os.path.join(str(tmp_path), "headunits.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"head_units": [{"host": "10.0.0.1", "token": "zz"}, '
                     '{"name": "ok", "host": "10.0.0.2", "token": "aabb"}]}')

    kept = headunits.load(str(tmp_path))
    assert [u.host for u in kept] == ["10.0.0.2"]


def test_staging_ships_the_descriptor_and_the_image_together(bridge, image):
    client = _paired(bridge)
    job, blob = staged.build_write_job(image, job_id="w1")

    reply = client.stage(job, blob)
    assert reply["status"] == "staged"
    assert reply["bytes"] == len(blob)
    assert bridge.job_image == blob, "the image arrives byte for byte behind the frame"
    assert bridge.job["blocks"][0]["cks_end"] == 0x1FDFFF, "and the descriptor with it"

    client.start()
    assert client.wait(poll=0.01, timeout=5.0)["status"] == "finished"
    client.close()


def test_a_truncated_image_is_refused_at_staging(bridge, image):
    """The last moment refusing is free. After this there may be an erased block."""
    client = _paired(bridge)
    job, blob = staged.build_write_job(image, job_id="w2")

    with pytest.raises(BridgeError) as refused:
        client.stage(job, blob[:1024])
    assert refused.value.code == "unsupported"
    client.close()


def test_a_read_comes_home_as_a_blob(bridge):
    client = _paired(bridge)
    client.stage(staged.build_read_job(job_id="r1"))
    client.start()
    client.wait(poll=0.01, timeout=5.0)

    data = client.fetch()
    assert len(data) == CAL_SIZE, "hex in the JSON would have doubled this"
    assert len(staged.assemble_read(data)) == FULL_SIZE
    client.close()


def test_fetching_before_it_has_run_is_refused(bridge):
    client = _paired(bridge)
    client.stage(staged.build_read_job(job_id="r2"))

    with pytest.raises(BridgeError) as early:
        client.fetch()
    assert early.value.code == "bad_state", \
        "a partial image looks exactly like a whole one from here"
    client.close()


# ---------------------------------------------------------------------- EDC15

def _edc15_image():
    return bytes((i * 7 + 3) & 0xFF for i in range(0x80000))


def test_an_edc15_job_carries_sectors_rather_than_blocks():
    job, blob = staged.build_edc15_write_job(_edc15_image())

    assert job["ecu"] == "edc15" and job["kind"] == "write"
    assert job["init"] == "kw1281-then-kwp"
    assert job["session"] == 0x85 and job["address"] == 0x01
    assert job["wake_baud"] == 9600 and job["kwp_baud"] == 10400
    assert "blocks" not in job, "sectors and blocks are different conversations"
    assert len(job["sectors"]) == 11, "an Am29F400BT has eleven"
    assert len(blob) == job["loader"]["length"] + 0x80000


def test_the_three_address_windows_are_all_shipped():
    """None of them follows from the others: the loader erases and programs at the CPU
    bus address and checksums at the read window, while the image is indexed from zero.
    Deriving one from another on the head unit would be it deciding something about a
    memory map it cannot see."""
    job, _ = staged.build_edc15_write_job(_edc15_image())
    first, last = job["sectors"][0], job["sectors"][-1]

    assert (first["addr"], first["write_addr"]) == (0x00000, 0x200000)
    assert (first["cks_from"], first["cks_to"]) == (0x080000, 0x090000)
    assert (last["addr"], last["write_addr"]) == (0x7C000, 0x27C000)
    assert (last["cks_from"], last["cks_to"]) == (0x0FC000, 0x100000)


def test_the_sectors_are_the_chips_and_not_a_uniform_split():
    job, _ = staged.build_edc15_write_job(_edc15_image())
    sizes = [s["size"] for s in job["sectors"]]

    # Top-boot: eight 64 KB, then 32 KB, 8 KB, 8 KB, 16 KB. A uniform split would be the
    # one thing this chip is not, and an erase sized from one would take out a neighbour.
    assert sizes == [0x10000] * 7 + [0x8000, 0x2000, 0x2000, 0x4000]
    assert sum(sizes) == 0x80000


def test_the_checksums_are_the_images_and_match_the_loaders_routine():
    from ovtsuite.flash.edc15 import SECTORS, sector_checksum

    image = _edc15_image()
    job, _ = staged.build_edc15_write_job(image)

    for sector, (start, end) in zip(job["sectors"], SECTORS):
        assert sector["cks"] == sector_checksum(image[start:end])


def test_the_payload_offsets_point_past_the_loader():
    image = _edc15_image()
    job, blob = staged.build_edc15_write_job(image)
    loader = job["loader"]["length"]

    for sector in job["sectors"]:
        assert sector["offset"] == loader + sector["addr"]
        piece = blob[sector["offset"]:sector["offset"] + sector["size"]]
        assert piece == image[sector["addr"]:sector["addr"] + sector["size"]]


def test_one_variant_travels_and_an_unvalidated_one_is_refused():
    """V is the only EDC15 variant this tuner drives -- all eight call sites in the flash
    dialog name it. Shipping a list to try in turn would be a capability nobody decided
    on, and each extra candidate is a security-access attempt with a WRONG key on a real
    controller."""
    from ovtsuite.flash.edc15 import KEY3, VARIANTS

    job, _ = staged.build_edc15_write_job(_edc15_image())
    assert "variants" not in job, "one variant, not a list to work down"
    assert job["variant"]["name"] == "V"
    assert job["variant"]["key"] == VARIANTS["V"][0]
    assert job["variant"]["key3"] == KEY3

    for unvalidated in ("P", "VM", "Auto"):
        with pytest.raises(ValueError):
            staged.build_edc15_write_job(_edc15_image(), variant=unvalidated)

    with pytest.raises(ValueError):
        staged.build_edc15_read_job(variant="Auto")


def test_a_full_write_marks_every_sector_without_changing_the_shape():
    """So the head unit runs one path and not two."""
    job, _ = staged.build_edc15_write_job(_edc15_image(), force_full=True)

    assert all(s["always"] for s in job["sectors"])
    assert len(job["sectors"]) == 11
    assert "every sector" in staged.describe(job)


def test_a_short_image_is_refused_here_rather_than_at_the_erase():
    with pytest.raises(ValueError):
        staged.build_edc15_write_job(_edc15_image()[:0x40000])


def test_an_eeprom_job_carries_the_loader_and_the_command_set():
    """The EEPROM is a third shape alongside blocks and sectors, because it is a third
    conversation: dump the part, work out the difference, write only that."""
    read, read_blob = staged.build_edc15_eeprom_read_job()

    assert read["kind"] == "read" and read["ecu"] == "edc15"
    assert read["regions"] == [{"addr": 0, "size": 512}]
    assert "sectors" not in read and "blocks" not in read and "read" not in read
    assert read["eeprom"] == {"size": 512, "dump": 0xA6,
                              "page0": 0xA9, "page1": 0xAA, "max_run": 255}
    assert len(read_blob) == read["loader"]["length"], "a read ships the loader alone"
    assert read["speed"] == 0, "EEPROM work stays at 10400"

    image = bytes((i * 3 + 1) & 0xFF for i in range(512))
    write, write_blob = staged.build_edc15_eeprom_write_job(image)

    assert write["kind"] == "write"
    assert write_blob == staged._loaders.eeprom_loader() + image
    assert write["eeprom"]["size"] == 512
    assert "regions" not in write

    with pytest.raises(ValueError):
        staged.build_edc15_eeprom_write_job(image[:64])


def test_the_two_eeprom_page_commands_are_different_commands():
    """Below 0x100 and above it are answered by different commands, not by a flag on one
    -- so a batched run may never cross the boundary."""
    job, _ = staged.build_edc15_eeprom_read_job()
    assert job["eeprom"]["page0"] != job["eeprom"]["page1"]


def test_an_eeprom_job_describes_itself_as_one():
    read, _ = staged.build_edc15_eeprom_read_job()
    write, _ = staged.build_edc15_eeprom_write_job(bytes(512))

    assert staged.describe(read) == "read the 512-byte EEPROM"
    assert "only what differs" in staged.describe(write)


def test_a_boot_mode_job_carries_the_three_blobs_and_the_bus_setup():
    """Boot mode is the path that works when the firmware is gone -- and the one thin
    pass-through fails at most completely, because nothing in it is framed and the
    laptop's framing never sends even the auto-baud byte."""
    from ovtsuite.flash import edc15boot as boot

    read, read_blob = staged.build_edc15_boot_read_job()

    assert read["kind"] == "read" and read["init"] == "boot"
    assert read["session"] == 0, "there is no diagnostic session in boot mode"
    assert read["boot"]["baud"] == boot.BAUD == 28800
    assert read["boot"]["flash_base"] == 0x800000
    assert read["boot"]["chunk"] == 0x200
    assert read["boot"]["stage1"]["length"] == 32, "the ROM loader takes exactly 32 bytes"
    assert "helper" not in read["boot"], "a read needs no flash helper"
    assert len(read_blob) == 32 + read["boot"]["monitor"]["length"]

    # The register table travels: what the external bus needs is a property of the board.
    assert [(r["addr"], r["value"]) for r in read["boot"]["regs"]] == list(boot.READ_MODE_REGS)

    image = bytes((i * 11 + 7) & 0xFF for i in range(0x80000))
    write, write_blob = staged.build_edc15_boot_write_job(image)

    assert write["kind"] == "write"
    assert [(r["addr"], r["value"]) for r in write["boot"]["regs"]] == list(boot.WRITE_MODE_REGS)
    assert write["boot"]["helper"]["length"] > 0, "a write needs one"
    assert write_blob.endswith(image), "the image follows the three blobs"
    assert len(write_blob) == write["boot"]["helper"]["offset"] \
        + write["boot"]["helper"]["length"] + len(image)

    # The helper's erase takes the whole chip, so anything less would leave the rest erased.
    with pytest.raises(ValueError):
        staged.build_edc15_boot_write_job(image[:0x40000])


def test_boot_mode_describes_itself_as_the_whole_chip():
    read, _ = staged.build_edc15_boot_read_job()
    write, _ = staged.build_edc15_boot_write_job(bytes(0x80000))

    assert staged.describe(read) == "read the whole chip in boot mode"
    assert staged.describe(write) == "erase and write the whole chip in boot mode"
