"""A patch carries the coding-variant copies (EDC16) too: the copy a coding
selects is a parameter of its own, and an edit for that coding lands there."""
import types

from ovtsuite import patch as P
from ovtsuite.session import TunerSession


class _Ent:
    kind = "constant"
    rows = cols = 1
    units = label = description = ""
    categories = ()
    tier = 0
    scan_key = None

    def __init__(self, name, addr, value, source="placed", coding=""):
        self.name, self.addr, self.value = name, addr, value
        self.end = addr + 2
        self.source, self.coding = source, coding

    def cell(self, r, c):
        return self.value


class _Sess:
    """The few things a diff, a gap list and a lookup ask of a session."""
    _coding_counterpart = TunerSession._coding_counterpart
    _by_group_alias = TunerSession._by_group_alias
    family = "p350"

    def __init__(self, mains, variants, data=b"\x00" * 64):
        self.entities, self._variants = list(mains), list(variants)
        self.binfile = types.SimpleNamespace(data=bytearray(data), name="x.bin")
        self.region = (0, len(data))
        self.by_key = {}

    def variant_entities(self):
        return self._variants

    def checksum_exempt_ranges(self):
        return []

    def counterpart(self, other, entity):
        return TunerSession.counterpart(self, other, entity)

    def _key_of(self):
        return {id(e): (e.name,) for e in self.entities}


def _file(main, variant, vaddr=0x20, coding="coding 150031 (5-speed manual)"):
    m = _Ent("AOHt_NumHeat_C", 0x10, main)
    v = _Ent("AOHt_NumHeat_C (variant @0x%X)" % vaddr, vaddr, variant,
             source="variant", coding=coding)
    s = _Sess([m], [v])
    s.by_key = {("AOHt_NumHeat_C",): m}
    return s


def test_a_changed_variant_copy_is_a_patch_entry_with_its_coding():
    a, b = _file(3, 3), _file(3, 0)          # only the variant copy differs
    es = P.diff_entities(a, b)
    assert [e.name for e in es] == ["AOHt_NumHeat_C (variant @0x20)"]
    assert es[0].coding == "coding 150031 (5-speed manual)"
    assert es[0].values == [[0]]
    assert "coding 150031" in es[0].title()


def test_a_main_copy_entry_has_no_coding():
    a, b = _file(3, 3), _file(1, 3)
    es = P.diff_entities(a, b)
    assert [e.name for e in es] == ["AOHt_NumHeat_C"]
    assert es[0].coding == ""


def test_the_coding_survives_a_save():
    pe = P.diff_entities(_file(3, 3), _file(3, 0))[0]
    assert P.PatchEntry.from_dict(pe.to_dict()).coding == pe.coding


def test_a_variant_copy_is_found_by_name_then_by_coding():
    t = _file(3, 3)
    assert P.find_entity(t, "AOHt_NumHeat_C (variant @0x20)").addr == 0x20
    # a target with the copy at another address: the coding picks it
    other = _file(3, 3, vaddr=0x30)
    got = P.find_entity(other, "AOHt_NumHeat_C (variant @0x20)",
                        "coding 150031 (5-speed manual)")
    assert got is not None and got.addr == 0x30
    # ... and none of its codings: not the wrong copy
    none = _file(3, 3, vaddr=0x30, coding="coding 150034 (automatic)")
    assert P.find_entity(none, "AOHt_NumHeat_C (variant @0x20)",
                         "coding 150031 (5-speed manual)") is None


def test_a_variant_copy_needs_no_confirmation_to_be_written():
    assert P.trusted(_Ent("x (variant @0x20)", 0x20, 1, source="variant"))
    assert P.trusted(_Ent("x", 0x20, 1, source="struct"))
    assert not P.trusted(_Ent("x", 0x20, 1, source="projected"))


def test_changed_variant_bytes_are_not_undefined_data():
    a, b = _file(3, 3), _file(3, 0)
    b.binfile.data[0x20:0x22] = b"\x00\x07"       # the variant copy's bytes
    assert P.diff_gaps(a, b) == []
    b.binfile.data[0x30:0x31] = b"\x01"          # bytes no entity covers
    assert P.diff_gaps(a, b) == [(0x30, 0x31)]
