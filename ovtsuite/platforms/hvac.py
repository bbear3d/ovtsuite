# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Audi Climatronic 4F/4L HVAC: identification (base-app knowledge).

An HVAC image is recognised at the CONTAINER level -- OVT's own ``.hvac``
(magic ``OVTHVAC\\x01``), a ``.hvac`` native container, or a Climatronic
``.sgo`` (all-plain sections in the high F2MC flash window) -- by
:mod:`ovtsuite.flash.hvac_image`, which is base code because the flasher
needs it. The per-firmware variable map and the checksum are the HVAC
module's (:mod:`ovtsuite.hvac.firmware`). A bare flat image carries no marker of its own, so there is no
``detect(data)`` here: :func:`ovtsuite.platforms.identify.identify` is told
by its caller that the container was an HVAC one.
"""

OWN_MAGIC = b"OVTHVAC\x01"
