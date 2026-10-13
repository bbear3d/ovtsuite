# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ECU flash read/write over K-line (ported from KWHack).

``ovtsuite.flash`` is the in-app port of KWHack's bench-validated EDC16
(and, later, EDC15) KWP2000/K-line flash routines: a pyserial transport with
the 5-baud slow init, the ISO14230 framing, the security-access key
algorithms, the EDC16 transferData cipher, and the read/write state machines
with progress + cancel hooks suitable for a GUI worker thread.

The protocol details are documented per module; the authoritative reference
is KWHack's ``vendor-kw1281test/EDC16/Edc16FlashVM.cs`` and
``docs/EDC16-Write-Protocol-Map.md``.
"""
