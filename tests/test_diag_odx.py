# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P5: importing ODX into definition documents -- a diagnostic
layer (ODX-D) into dids/routines/faults, a flash container (ODX-F, the
shape of Bear's real 8HP SGO files) into a module note. The produced docs
validate against the format-v1 schema."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.diag import defs as D
from ovtsuite.diag.uds_odx import import_odx

ODX_D = """<?xml version="1.0"?>
<ODX MODEL-VERSION="2.2.0"><DIAG-LAYER-CONTAINER><ECU-VARIANTS><ECU-VARIANT ID="EV">
<SHORT-NAME>GS19</SHORT-NAME><DIAG-COMMS>
 <DIAG-SERVICE ID="S1"><SHORT-NAME>RDBI_ATF</SHORT-NAME><LONG-NAME>ATF temperature</LONG-NAME>
  <REQUEST-REF ID-REF="Q1"/><POS-RESPONSE-REF ID-REF="R1"/></DIAG-SERVICE>
 <DIAG-SERVICE ID="S2"><SHORT-NAME>RDBI_GEAR</SHORT-NAME><LONG-NAME>Selected gear</LONG-NAME>
  <REQUEST-REF ID-REF="Q2"/><POS-RESPONSE-REF ID-REF="R2"/></DIAG-SERVICE>
 <DIAG-SERVICE ID="S3"><SHORT-NAME>RDBI_VIN</SHORT-NAME><LONG-NAME>VIN</LONG-NAME>
  <REQUEST-REF ID-REF="Q3"/><POS-RESPONSE-REF ID-REF="R3"/></DIAG-SERVICE>
 <DIAG-SERVICE ID="S4"><SHORT-NAME>RC_Reset</SHORT-NAME><LONG-NAME>Reset adaptation</LONG-NAME>
  <REQUEST-REF ID-REF="Q4"/></DIAG-SERVICE>
</DIAG-COMMS></ECU-VARIANT></ECU-VARIANTS>
<REQUESTS>
 <REQUEST ID="Q1"><PARAMS><PARAM SEMANTIC="SERVICE-ID"><SHORT-NAME>SID</SHORT-NAME><CODED-VALUE>34</CODED-VALUE></PARAM>
  <PARAM SEMANTIC="ID"><SHORT-NAME>DID</SHORT-NAME><CODED-VALUE>4660</CODED-VALUE></PARAM></PARAMS></REQUEST>
 <REQUEST ID="Q2"><PARAMS><PARAM SEMANTIC="SERVICE-ID"><SHORT-NAME>SID</SHORT-NAME><CODED-VALUE>34</CODED-VALUE></PARAM>
  <PARAM SEMANTIC="ID"><SHORT-NAME>DID</SHORT-NAME><CODED-VALUE>8452</CODED-VALUE></PARAM></PARAMS></REQUEST>
 <REQUEST ID="Q3"><PARAMS><PARAM SEMANTIC="SERVICE-ID"><SHORT-NAME>SID</SHORT-NAME><CODED-VALUE>34</CODED-VALUE></PARAM>
  <PARAM SEMANTIC="ID"><SHORT-NAME>DID</SHORT-NAME><CODED-VALUE>61840</CODED-VALUE></PARAM></PARAMS></REQUEST>
 <REQUEST ID="Q4"><PARAMS><PARAM><SHORT-NAME>SID</SHORT-NAME><CODED-VALUE>49</CODED-VALUE></PARAM>
  <PARAM><SHORT-NAME>SUB</SHORT-NAME><CODED-VALUE>1</CODED-VALUE></PARAM>
  <PARAM><SHORT-NAME>RID</SHORT-NAME><CODED-VALUE>515</CODED-VALUE></PARAM></PARAMS></REQUEST>
</REQUESTS>
<POS-RESPONSES>
 <POS-RESPONSE ID="R1"><PARAMS><PARAM SEMANTIC="DATA"><SHORT-NAME>v</SHORT-NAME><DOP-REF ID-REF="DTEMP"/></PARAM></PARAMS></POS-RESPONSE>
 <POS-RESPONSE ID="R2"><PARAMS><PARAM SEMANTIC="DATA"><SHORT-NAME>v</SHORT-NAME><DOP-REF ID-REF="DGEAR"/></PARAM></PARAMS></POS-RESPONSE>
 <POS-RESPONSE ID="R3"><PARAMS><PARAM SEMANTIC="DATA"><SHORT-NAME>v</SHORT-NAME><DOP-REF ID-REF="DVIN"/></PARAM></PARAMS></POS-RESPONSE>
</POS-RESPONSES>
<DATA-OBJECT-PROPS>
 <DATA-OBJECT-PROP ID="DTEMP"><SHORT-NAME>Temp</SHORT-NAME>
  <COMPU-METHOD><CATEGORY>LINEAR</CATEGORY><COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>
   <COMPU-RATIONAL-COEFFS><COMPU-NUMERATOR><V>-40</V><V>1</V></COMPU-NUMERATOR><COMPU-DENOMINATOR><V>1</V></COMPU-DENOMINATOR>
   </COMPU-RATIONAL-COEFFS></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>
  <DIAG-CODED-TYPE BASE-DATA-TYPE="A_UINT32"><BIT-LENGTH>8</BIT-LENGTH></DIAG-CODED-TYPE></DATA-OBJECT-PROP>
 <DATA-OBJECT-PROP ID="DGEAR"><SHORT-NAME>Gear</SHORT-NAME>
  <COMPU-METHOD><CATEGORY>TEXTTABLE</CATEGORY><COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>
   <COMPU-SCALE><LOWER-LIMIT>0</LOWER-LIMIT><COMPU-CONST><VT>Neutral</VT></COMPU-CONST></COMPU-SCALE>
   <COMPU-SCALE><LOWER-LIMIT>7</LOWER-LIMIT><COMPU-CONST><VT>7th</VT></COMPU-CONST></COMPU-SCALE>
  </COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>
  <DIAG-CODED-TYPE BASE-DATA-TYPE="A_UINT32"><BIT-LENGTH>8</BIT-LENGTH></DIAG-CODED-TYPE></DATA-OBJECT-PROP>
 <DATA-OBJECT-PROP ID="DVIN"><SHORT-NAME>Vin</SHORT-NAME>
  <DIAG-CODED-TYPE BASE-DATA-TYPE="A_ASCIISTRING"><MAX-LENGTH>17</MAX-LENGTH></DIAG-CODED-TYPE></DATA-OBJECT-PROP>
</DATA-OBJECT-PROPS>
<DTC-DOPS><DTC-DOP ID="DD"><SHORT-NAME>DTCs</SHORT-NAME><DTCS>
 <DTC ID="T1"><SHORT-NAME>P0729</SHORT-NAME><TROUBLE-CODE>29993</TROUBLE-CODE>
  <DISPLAY-TROUBLE-CODE>P0729</DISPLAY-TROUBLE-CODE><TEXT>Gear 6 incorrect ratio</TEXT></DTC>
 <DTC ID="T2"><SHORT-NAME>P1714</SHORT-NAME><TROUBLE-CODE>0x171400</TROUBLE-CODE>
  <DISPLAY-TROUBLE-CODE>P1714</DISPLAY-TROUBLE-CODE><TEXT>Pressure control solenoid 4</TEXT></DTC>
</DTCS></DTC-DOP></DTC-DOPS>
</DIAG-LAYER-CONTAINER></ODX>"""

# the shape of Bear's real 8HP SGO flash ODX (Reference/ZF8HP/SGO/ALX52/_raw/*.odx)
ODX_F = """<?xml version="1.0"?>
<ODX MODEL-VERSION="2.0.1"><FLASH ID="FL_4M0927158AA_1009"><SHORT-NAME>FL_4M0927158AA_1009</SHORT-NAME>
<ECU-MEMS><ECU-MEM ID="EM"><SHORT-NAME>EM</SHORT-NAME><MEM>
 <SESSIONS><SESSION><SECURITYS>
   <SECURITY><SECURITY-METHOD>SA2</SECURITY-METHOD><FW-SIGNATURE>68034845</FW-SIGNATURE></SECURITY>
   <SECURITY><SECURITY-METHOD>CRC32</SECURITY-METHOD><VALIDITY-FOR>DB_04FLASHDATA</VALIDITY-FOR><FW-CHECKSUM>0x1234</FW-CHECKSUM></SECURITY>
 </SECURITYS></SESSION></SESSIONS>
 <DATABLOCKS>
   <DATABLOCK TYPE="ERASE"><SHORT-NAME>DB_04ERASE</SHORT-NAME></DATABLOCK>
   <DATABLOCK><SHORT-NAME>DB_04FLASHDATA</SHORT-NAME><SEGMENTS><SEGMENT><SOURCE-START-ADDRESS>04</SOURCE-START-ADDRESS></SEGMENT></SEGMENTS></DATABLOCK>
   <DATABLOCK><SHORT-NAME>DB_01FLASHDATA</SHORT-NAME><SEGMENTS><SEGMENT><SOURCE-START-ADDRESS>01</SOURCE-START-ADDRESS></SEGMENT></SEGMENTS></DATABLOCK>
 </DATABLOCKS>
</MEM></ECU-MEM></ECU-MEMS></FLASH></ODX>"""


def test_import_odx_d_dids_routines_and_dtcs():
    imp = import_odx(ODX_D.encode(), match={"addr": "02", "protocol": "uds", "part": "8K0927158*"})
    assert imp.kind == "odx-d" and imp.ecu_name == "GS19"
    assert imp.counts == {"DIDs": 3, "routines": 1, "fault texts": 2}
    dids_doc = next(d for d in imp.docs if d["kind"] == "dids")
    dids = {e["did"]: e for e in dids_doc["dids"]}
    assert dids[0x1234]["name"] == "ATF temperature" and dids[0x1234]["type"] == "u8"
    assert dids[0x1234]["scale"] == {"kind": "linear", "factor": 1.0, "offset": -40.0, "unit": ""} and dids[0x1234]["live"]
    assert dids[0x2104]["scale"]["table"] == {"0": "Neutral", "7": "7th"}
    assert dids[0xF190]["type"] == "ascii" and dids[0xF190]["length"] == 17 and not dids[0xF190].get("live")
    assert dids_doc["routines"][0]["routine"] == 0x0203 and dids_doc["match"]["part"] == "8K0927158*"
    faults = {e["sae"]: e for e in next(d for d in imp.docs if d["kind"] == "faults")["codes"]}
    assert faults["P0729"]["text"] == "Gear 6 incorrect ratio" and faults["P1714"]["text"].startswith("Pressure")
    # every produced doc validates
    from ovtsuite.diag.defs.model import Pack, Doc, Match
    pack = Pack(id="imported", version="0.1.0", title="", publisher="", license="", requires={}, depends=[],
                coverage={}, path="", origin="user")
    for body in imp.docs:
        pack.docs.append(Doc(kind=body["kind"], match=Match.from_doc(body.get("match")), body=body, path="odx"))
    rep = D.validate_pack(pack)
    assert rep.ok, rep.text()
    # and resolve as a usable module
    res = D.Resolver([pack])
    ident = D.Identity(addr="02", protocol="uds", part="8K0927158C")
    assert res.view("dids", ident).entries["did.1234"].entry["name"] == "ATF temperature"
    assert res.fault_text(ident, sae="P0729") == "Gear 6 incorrect ratio"


def test_import_odx_f_flash_container():
    imp = import_odx(ODX_F.encode(), match={"addr": "02", "protocol": "uds", "part": "4M0927158*"})
    assert imp.kind == "odx-f" and imp.ecu_name == "FL_4M0927158AA_1009"
    assert imp.counts["flash blocks"] == 2                 # the ERASE block is skipped
    doc = next(d for d in imp.docs if d["kind"] == "module")
    assert doc["identification"]["record"] == "uds_f1xx" and "SA2" in doc["note"] and "CRC32" in doc["note"]
    from ovtsuite.diag.defs.model import Pack, Doc, Match
    pack = Pack(id="f", version="0", title="", publisher="", license="", requires={}, depends=[], coverage={},
                path="", origin="user")
    pack.docs.append(Doc(kind="module", match=Match.from_doc(doc["match"]), body=doc, path="odx"))
    assert D.validate_pack(pack).ok


def test_non_odx_is_rejected():
    with pytest.raises(ValueError):
        import_odx(b"<html><body>not odx</body></html>")
    with pytest.raises(ValueError):
        import_odx(b"not xml at all {")
