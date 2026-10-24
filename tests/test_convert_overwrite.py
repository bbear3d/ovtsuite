# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An output file already there: asked about, replaced or left alone -- in
the run itself, extraction, the command line, a watch folder and the AI
tool (no Qt). ``python -m pytest tests/test_convert_overwrite.py -q``"""

import os

import pytest

from ovtsuite import convert as C

ROOT = os.path.join(os.path.dirname(__file__), "..")
VP37 = os.path.join(ROOT, "Reference", "EDC15", "0281001410_MSA15_90hp_vp37.ols")


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


def _bin(path, n=0x400, fill=0x5A):
    path.write_bytes(bytes([fill]) * n)
    return str(path)


def _job(tmp_path, existing=None):
    src = _bin(tmp_path / "a.bin")
    out = tmp_path / "a.hex"
    out.write_text("OLD")
    o = {} if existing is None else {"existing": existing}
    return C.Job(sources=[src], target="hex", out=str(out), options=o), out


# ------------------------------------------------------------------ the run
def test_no_file_there_is_written_without_asking(tmp_path):
    asked = []
    src = _bin(tmp_path / "a.bin")
    res = C.run(C.Job(sources=[src], target="hex", out=str(tmp_path / "a.hex")),
                ask=lambda p: asked.append(p) or False)
    assert asked == [] and res.out == [str(tmp_path / "a.hex")] and not res.skipped


def test_ask_is_asked_before_the_work_and_its_answer_counts(tmp_path):
    job, out = _job(tmp_path)                           # no choice: "ask"
    asked = []
    res = C.run(job, ask=lambda p: asked.append(p) or False)
    assert asked == [str(out)]
    assert out.read_text() == "OLD" and res.out == [] and res.skipped == [str(out)]
    assert "left alone" in res.report
    res = C.run(job, ask=lambda p: True)
    assert out.read_text() != "OLD" and res.out == [str(out)] and not res.skipped


def test_ask_with_no_one_to_ask_leaves_it_alone(tmp_path):
    job, out = _job(tmp_path, "ask")
    res = C.run(job)
    assert out.read_text() == "OLD" and res.skipped == [str(out)]


@pytest.mark.parametrize("policy,replaced", [("overwrite", True), ("skip", False)])
def test_overwrite_and_skip_never_ask(tmp_path, policy, replaced):
    job, out = _job(tmp_path, policy)
    res = C.run(job, ask=lambda p: pytest.fail("asked"))
    assert (out.read_text() != "OLD") is replaced
    assert bool(res.skipped) is not replaced


def test_a_choice_not_known_is_ask(tmp_path):
    assert C.existing_policy(C.Job(sources=[], target="hex")) == "ask"
    assert C.existing_policy(C.Job(sources=[], target="hex",
                                   options={"existing": "bogus"})) == "ask"


def test_extract_asks_per_binary(tmp_path):
    if not os.path.isfile(VP37):
        pytest.skip("VP37 project not present")
    job = C.Job(sources=[VP37], target=C.EXTRACT, out=str(tmp_path),
                options={"extract": "all"})
    first = C.run(job)
    assert len(first.out) == 2
    for p in first.out:
        with open(p, "wb") as fh:
            fh.write(b"OLD")
    asked = []
    keep = first.out[0]
    res = C.run(C.Job(sources=[VP37], target=C.EXTRACT, out=str(tmp_path),
                      options={"extract": "all"}),
                ask=lambda p: asked.append(p) or p != keep)
    assert asked == first.out
    assert res.skipped == [keep] and res.out == [first.out[1]]
    assert open(keep, "rb").read() == b"OLD"
    assert open(first.out[1], "rb").read() != b"OLD"
    assert "1 already there, left alone" in res.values[0]
    # all of them there and skipped: reported, not "nothing chosen"
    res = C.run(C.Job(sources=[VP37], target=C.EXTRACT, out=str(tmp_path),
                      options={"extract": "all", "existing": "skip"}))
    assert res.out == [] and len(res.skipped) == 2


# ------------------------------------------------------------------ command line
def _cli(argv):
    from ovtsuite.cli import main
    return main(argv)


def test_cli_leaves_a_file_alone_unless_overwrite(tmp_path, capsys):
    src = _bin(tmp_path / "a.bin")
    out = tmp_path / "a.hex"
    out.write_text("OLD")
    assert _cli(["convert", src, "--to", "hex", "-o", str(out)]) == 3
    assert out.read_text() == "OLD"
    assert "already there (give --overwrite" in capsys.readouterr().out
    assert _cli(["convert", src, "--to", "hex", "-o", str(out), "--overwrite"]) == 0
    assert out.read_text() != "OLD"


def test_cli_a_preset_does_not_decide(tmp_path):
    import json
    src = _bin(tmp_path / "a.bin")
    out = tmp_path / "a.hex"
    out.write_text("OLD")
    preset = tmp_path / "p.json"
    preset.write_text(json.dumps({"target": "hex", "options": {"existing": "overwrite"}}))
    assert _cli(["convert", src, "--preset", str(preset), "-o", str(out)]) == 3
    assert out.read_text() == "OLD"
    # nor is the choice saved into one
    saved = tmp_path / "s.json"
    assert _cli(["convert", src, "--to", "hex", "--overwrite",
                 "--save-preset", str(saved)]) == 0
    assert "existing" not in json.load(open(saved)).get("options", {})


def test_cli_batch_skips_only_the_files_there(tmp_path, capsys):
    ins = tmp_path / "in"
    ins.mkdir()
    _bin(ins / "a.bin")
    _bin(ins / "b.bin")
    outs = tmp_path / "out"
    outs.mkdir()
    (outs / "a.hex").write_text("OLD")
    assert _cli(["convert", str(ins), "--to", "hex", "-o", str(outs)]) == 3
    assert (outs / "a.hex").read_text() == "OLD" and (outs / "b.hex").is_file()
    assert "skipped: %s" % os.path.join(str(outs), "a.hex") in capsys.readouterr().out


# ------------------------------------------------------------------ watch folder
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _settled(w, clock):
    first = w.poll()
    clock.t += 5
    return first + w.poll()


@pytest.mark.parametrize("policy,replaced", [(None, False), ("ask", False),
                                             ("skip", False), ("overwrite", True)])
def test_watch_never_asks(tmp_path, policy, replaced):
    from ovtsuite.convert.watch import Watcher
    inbox = tmp_path / "in"
    (inbox / "Converted").mkdir(parents=True)
    old = inbox / "Converted" / "a.hex"
    old.write_text("OLD")
    clock = Clock()
    o = {} if policy is None else {"existing": policy}
    w = Watcher(str(inbox), C.Job(sources=[], target="hex", options=o), clock=clock)
    _bin(inbox / "a.bin")
    ev = _settled(w, clock)
    assert len(ev) == 1
    assert (old.read_text() != "OLD") is replaced
    if not replaced:
        assert ev[0].ok is None and "already there" in ev[0].text


def test_watch_brings_its_own_result_up_to_date(tmp_path):
    """A result the watch wrote is replaced when its file arrives again
    changed, even with "skip"."""
    from ovtsuite.convert.watch import Watcher
    inbox = tmp_path / "in"
    inbox.mkdir()
    clock = Clock()
    w = Watcher(str(inbox), C.Job(sources=[], target="hex", options={"existing": "skip"}),
                clock=clock)
    _bin(inbox / "a.bin", fill=0x11)
    assert [e.ok for e in _settled(w, clock)] == [True]
    out = inbox / "Converted" / "a.hex"
    first = out.read_text()
    _bin(inbox / "a.bin", n=0x800, fill=0x22)
    os.utime(str(inbox / "a.bin"), (clock.t + 50, clock.t + 50))
    assert [e.ok for e in _settled(w, clock)] == [True]
    assert out.read_text() != first


# ------------------------------------------------------------------ AI tool
def test_ai_tool_leaves_it_alone_unless_told(tmp_path):
    from ovtsuite.ai.tools import Context, Registry
    from ovtsuite.convert import ai_tools
    reg = Registry()
    ai_tools.register(reg)
    src = _bin(tmp_path / "a.bin")
    out = tmp_path / "a.hex"
    out.write_text("OLD")
    ok, r = reg.call("convert_files", Context(None),
                     {"sources": [src], "target": "hex", "out": str(out)})
    assert ok and r["written"] == [] and r["skipped_existing"] == [str(out)]
    assert out.read_text() == "OLD"
    ok, r = reg.call("convert_files", Context(None),
                     {"sources": [src], "target": "hex", "out": str(out), "overwrite": True})
    assert ok and r["written"] == [str(out)] and out.read_text() != "OLD"
