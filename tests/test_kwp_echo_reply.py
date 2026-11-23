# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A cable that drops echoed bytes makes the echo drain read the ECU's answer
as echo; ``send_receive(accept_echo_reply=True)`` then takes that as the answer."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ovtsuite.flash import kwp as K


class _Link:
    read_timeout = 0.1

    def __init__(self, echo, sent):
        self.last_echo, self.last_sent = echo, sent
        self.read = []

    def write_bytes(self, data):
        pass

    def write_byte(self, b):
        pass

    def clear_rx(self):
        pass

    def read_byte(self):
        raise AssertionError("must not wait for an answer")


def _kwp(link):
    k = K.Kwp2000(link)
    k.fast = True
    k.P3 = 0
    return k


def test_short_echo_ending_in_the_answer_counts_as_the_answer():
    link = _Link(b"\x00\xff\x36\x01\x76\x77", 258)
    assert _kwp(link).send_receive(K.SVC_TRANSFER, b"\x00" * 10,
                                   exclude_addresses=True,
                                   accept_echo_reply=True) is None


def test_full_echo_still_reads_the_answer():
    link = _Link(b"\x00" * 258, 258)
    try:
        _kwp(link).send_receive(K.SVC_TRANSFER, b"\x00" * 10,
                                exclude_addresses=True, accept_echo_reply=True)
    except AssertionError:
        return
    raise AssertionError("expected a read of the answer")


def test_five_baud_w4_is_kept_across_a_channel_reopen():
    from ovtsuite.flash.kline_j2534_raw import J2534RawKLine

    class Ch:
        def __init__(self, log):
            self.log = log

        def pass_all(self):
            pass

        def set_config(self, **kw):
            self.log.append(kw)

    class Dev:
        def __init__(self):
            self.log = []

        def connect(self, *a):
            return Ch(self.log)

    dev = Dev()
    link = J2534RawKLine(dev, baud=9600)
    link.set_five_baud_w4(30)
    link._open()                               # what a baud change does on the OpenPort
    assert dev.log.count({"W4": 30}) == 2
