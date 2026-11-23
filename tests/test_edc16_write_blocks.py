# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""edc16_write.write_blocks runs the block table's write cycles: the ODIS order,
one RequestDownload per span, nothing erased on a dry run, and every span
encrypted before the session opens."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import edc16_blocks as B  # noqa: E402
from ovtsuite.flash import edc16_write as W  # noqa: E402


class Fl:
    """Records the protocol calls write_blocks makes."""

    def __init__(self, matches=()):
        self.calls = []
        self.matches = set(matches)

    def bus_name(self):
        return "test"

    def log(self, _m):
        pass

    def open_program_session(self, *a, **k):
        self.calls.append(("session",))

    def _checksum_probe_classify(self, start, end, cks):
        return "accepted:match" if start in self.matches else "accepted:mismatch"

    def _request_download(self, addr, size):
        self.calls.append(("download", addr, size))

    def _erase_block(self, start, end):
        self.calls.append(("erase", start, end))

    def _transfer_data(self, enc, _pct):
        self.calls.append(("transfer", len(enc)))

    def _request_transfer_exit(self):
        pass

    def _verify_block(self, start, end, cks):
        self.calls.append(("verify", start, end))

    def _finalize(self):
        self.calls.append(("finalize",))


def _image():
    img = bytearray(b"\xFF" * B.FULL_SIZE)
    for a in range(0, B.FULL_SIZE, 0x100):
        img[a] = 0x5A
    return bytes(img)


def _mpc():
    img = bytearray(b"\xFF" * B.MPC_IMAGE_SIZE)
    img[0x4000:0x4100] = b"\x11" * 0x100
    return bytes(img)


def _downloads(fl):
    return [c[1] for c in fl.calls if c[0] == "download"]


def test_blocks_run_in_the_odis_order():
    fl = Fl()
    W.write_blocks(fl, _image(), _mpc(), blocks=("cal", "mpc", "program", "boot"),
                   arm=True, force_full=True, fast_init_prime=False,
                   program_end=B.PROGRAM_END)
    assert fl.calls[0] == ("session",) and fl.calls[-1] == ("finalize",)
    assert _downloads(fl) == [0x010000, 0x040000, 0x404000, 0x1C0000, 0x180000]
    erases = [c[1:] for c in fl.calls if c[0] == "erase"]
    assert erases == [(0x010000, 0x03FFFF), (0x040000, 0x09FFFF), (0x404000, 0x47FFFF),
                      (0x1C0000, 0x1FFFFF), (0x180000, 0x1BFFFF)]
    verifies = [c[1:] for c in fl.calls if c[0] == "verify"]
    assert verifies[0] == (0x010000, 0x03FEFF) and verifies[3] == (0x1C0000, 0x1FDFFF)


def test_a_dry_run_erases_nothing():
    fl = Fl()
    W.write_blocks(fl, _image(), blocks=("program", "cal"), arm=False,
                   fast_init_prime=False, program_end=B.PROGRAM_END)
    assert _downloads(fl) == [0x040000, 0x1C0000, 0x180000]
    assert not [c for c in fl.calls if c[0] in ("erase", "transfer")]


def test_a_matching_span_is_skipped_unless_forced():
    fl = Fl(matches={0x1C0000})
    W.write_blocks(fl, _image(), blocks=("cal",), arm=True, force_full=False,
                   fast_init_prime=False)
    assert _downloads(fl) == [0x180000]
    fl = Fl(matches={0x1C0000})
    W.write_blocks(fl, _image(), blocks=("cal",), arm=True, force_full=True,
                   fast_init_prime=False)
    assert _downloads(fl) == [0x1C0000, 0x180000]


def test_the_program_extent_follows_the_source():
    fl = Fl()
    W.write_blocks(fl, _image(), blocks=("program",), arm=True, fast_init_prime=False,
                   program_end=0x180000)
    assert ("download", 0x040000, 0x140000) in fl.calls
    assert ("erase", 0x040000, 0x17FFFF) in fl.calls


def test_mpc_needs_its_image():
    with pytest.raises(ValueError, match="internal-flash"):
        W.write_blocks(Fl(), _image(), None, blocks=("mpc",), fast_init_prime=False)


@pytest.mark.parametrize("size", [0x80000, 0x80100, 0x80200])
def test_a_shorter_stored_mpc_is_padded_to_the_working_image(size):
    short = _mpc()[:size]
    padded = B.pad_mpc(short)
    assert len(padded) == B.MPC_IMAGE_SIZE and padded[:0x80000] == _mpc()[:0x80000]
    assert padded[0x80000:] == b"\xFF" * 0x1000
    assert B.available(_image(), short)["mpc"]
    fl = Fl()
    W.write_blocks(fl, _image(), short, blocks=("mpc",), arm=True,
                   fast_init_prime=False)
    assert _downloads(fl) == [0x404000]
    assert B.pad_mpc(_mpc()[:0x7FFFF]) == _mpc()[:0x7FFFF]     # too short: as is


def test_the_mpc_read_stops_at_the_end_of_the_flash():
    """0x480000-0x480FFF is the image's padding, not flash: the ECU refuses
    it, so it is never asked for (every unpacked SGO .mpc ends by 0x47FFFF)."""
    assert B.MPC.read == (0x404000, 0x480000)


def test_the_error_says_what_was_given():
    with pytest.raises(ValueError, match="0x1000 bytes"):
        W.write_blocks(Fl(), _image(), b"\x00" * 0x1000, blocks=("mpc",),
                       fast_init_prime=False)


def test_the_flasher_takes_the_mpc_kept_with_a_project_set(monkeypatch):
    """A project tab has no container: its set's internal flash is used."""
    import types
    from ovtsuite.project import companions as co
    from ovtsuite.flash import gui_flash
    mpc = _mpc()
    monkeypatch.setattr(co, "bytes_of", lambda p, sid, role, branch=None:
                        mpc if (sid, role) == ("s1", "mpc") else None)
    ref = types.SimpleNamespace(doc=types.SimpleNamespace(project=object()),
                                set=types.SimpleNamespace(id="s1"), bid="b1")
    win = types.SimpleNamespace(primary=types.SimpleNamespace(),
                                current=types.SimpleNamespace(project_ref=ref))
    dlg = types.SimpleNamespace(_window=win)
    got, label = gui_flash.FlashDialog._source_mpc_image(dlg)
    assert (got, label) == (mpc, "project")


def test_nothing_selected_is_an_error():
    with pytest.raises(ValueError):
        W.write_blocks(Fl(), _image(), blocks=(), fast_init_prime=False)


def test_available_blocks():
    avail = B.available(_image(), _mpc())
    assert avail == {"boot": True, "program": True, "cal": True, "mpc": True}
    blank = B.available(b"\xFF" * B.FULL_SIZE, None)
    assert blank == {"boot": False, "program": False, "cal": False, "mpc": False}


def test_the_status_lines_read_as_sentences():
    stages = []
    W.write_blocks(Fl(), _image(), _mpc(), blocks=("mpc",), arm=True,
                   fast_init_prime=False, on_stage=stages.append)
    assert "Writing 496 KB to the internal flash (MPC)..." in stages
    assert any(s.startswith("Erasing the internal flash (MPC)") for s in stages)
    assert stages[-1] == "Done."


def test_a_busy_ecu_keeps_the_step_on_screen():
    """The ECU answers "busy" (7F 31 78) at the start of a long erase: the
    status line keeps saying what it is busy with."""
    import types
    from ovtsuite.flash import gui_flash
    from ovtsuite.flash.tp20_transport import BUSY
    shown = []
    dlg = types.SimpleNamespace(stage=types.SimpleNamespace(setText=shown.append))
    gui_flash.FlashDialog._show_stage(dlg, "Erasing the program...")
    gui_flash.FlashDialog._show_stage(dlg, BUSY + "service 0x31, 7F 31 78)")
    assert shown == ["Erasing the program..."]
