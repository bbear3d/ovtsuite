# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SM2 firmware patcher: redirect cmd 0x0053 dispatch to the dead DMA teardown.

Background
----------
SM2 Pro firmware 2.21.32 (STM32F105, ``SM2_2.21.32_A111788.bin``) crashes
the device after every crank+cam GPT session because DMA1_Ch2 and DMA1_Ch3
(the TIM4 capture channels that feed the cam waveform generator) are *never*
disabled by any USB command.  The USB stop sequence issues no teardown, so
the DMA channels remain live, and the next ``PassThruOpen`` call returns
``ERR_DEVICE_NOT_CONNECTED (0x08)`` until the firmware is power-cycled.

A teardown routine that fixes this already exists in the firmware at
``0x080070B0`` -- it clears DMA1_IFCR, disables DMA1_Ch3 and DMA1_Ch2 (EN
bits), resets TIM3 state, then returns.  It has **zero callers** (dead code).

USB command 0x0053 (dispatch table entry 23, file offset ``0x0F228``) points
to a no-op ``bx lr`` stub at ``0x0800E6F5``.  This patch replaces that fn_ptr
with the Thumb address of the teardown (``0x080070B1 = 0x080070B0 | 1``), so
calling IOCTL ``0x800B`` (→ USB cmd 0x0053 via the ``IOCTL - 0x7FB8`` formula
in ``smj2534.dll``) triggers a safe, synchronous DMA teardown.

The firmware has no stored CRC (last 64 bytes are all ``0xFF``), so no
checksum recalculation is needed.

Usage
-----
From the repo root::

    python -m ovtsuite.flash.sm2_patch                     # default paths
    python -m ovtsuite.flash.sm2_patch path/to/input.bin   # explicit input
    python -m ovtsuite.flash.sm2_patch input.bin out.bin    # explicit both

Or import and call :func:`patch_firmware` from other code.
"""

import argparse
import os
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (all verified against SM2_2.21.32_A111788.bin disassembly)
# ---------------------------------------------------------------------------

# Firmware file properties
FW_SIZE         = 0x0004_0000          # 256 KiB (STM32F105 flash)
FW_BASE         = 0x0800_0000          # STM32 flash load address
FW_VECTOR_MAGIC = 0x2000_1800          # expected initial SP (Stack Pointer) at offset 0

# USB dispatch table: 42 entries × 8 bytes each, entry = {fn_ptr u32, cmd_id u32}
DISPATCH_ENTRY_OFFSET = 0x0F228        # entry 23 fn_ptr field (file offset)
DISPATCH_CMD_OFFSET   = 0x0F22C        # entry 23 cmd_id field (file offset)
EXPECTED_CMD_ID       = 0x0000_0053   # sanity-check: must be cmd 0x0053
EXPECTED_FN_PTR       = 0x0800_E6F5   # current value: no-op stub "bx lr"

# Teardown function
TEARDOWN_ADDR   = 0x0800_70B0         # first instruction of teardown fn
TEARDOWN_THUMB  = TEARDOWN_ADDR | 1   # Thumb interworking address stored in fn_ptr

# Patch: 4 bytes at DISPATCH_ENTRY_OFFSET
PATCH_OLD = struct.pack("<I", EXPECTED_FN_PTR)   # b'\xF5\xE6\x00\x08'
PATCH_NEW = struct.pack("<I", TEARDOWN_THUMB)    # b'\xB1\x70\x00\x08'

# Default file paths (resolved relative to the *repo root*, not this file)
_HERE = Path(__file__).resolve().parent          # ovtsuite/flash/
_REPO = _HERE.parent.parent                      # repo root
DEFAULT_INPUT  = _REPO / "Reference" / "Logs" / "SM2_2.21.32_A111788.bin"
DEFAULT_SUFFIX = "_patched"


def _default_output(input_path: Path) -> Path:
    """Derive patched filename: ``foo.bin`` → ``foo_patched.bin``."""
    stem = input_path.stem + DEFAULT_SUFFIX
    return input_path.with_name(stem + input_path.suffix)


# ---------------------------------------------------------------------------
# Core patcher
# ---------------------------------------------------------------------------

class PatchError(Exception):
    """Raised when the input firmware fails a pre-patch sanity check."""


def patch_firmware(
    input_path: Path | str,
    output_path: Path | str | None = None,
    *,
    force: bool = False,
    verbose: bool = True,
) -> Path:
    """Patch *input_path* and write the result to *output_path*.

    Parameters
    ----------
    input_path:
        Source binary (unpatched SM2 firmware).
    output_path:
        Destination file.  Defaults to ``<stem>_patched<ext>`` alongside the
        source.
    force:
        If ``True``, skip the "already patched?" check and allow overwriting
        a file that already has ``PATCH_NEW`` at the dispatch entry.
    verbose:
        Print progress to stdout when ``True``.

    Returns
    -------
    Path
        Absolute path of the written output file.

    Raises
    ------
    PatchError
        If any sanity check fails (wrong size, wrong cmd_id, already patched,
        etc.).  The output file is *not* written in this case.
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg)

    input_path  = Path(input_path)
    if output_path is None:
        output_path = _default_output(input_path)
    output_path = Path(output_path)

    # --- read ---------------------------------------------------------------
    log(f"Reading  {input_path}")
    data = input_path.read_bytes()

    # --- size check ---------------------------------------------------------
    if len(data) != FW_SIZE:
        raise PatchError(
            f"Unexpected firmware size: {len(data):#x} bytes "
            f"(expected {FW_SIZE:#x}).  Wrong file?"
        )

    # --- vector table spot-check (initial SP) --------------------------------
    sp = struct.unpack_from("<I", data, 0)[0]
    if sp != FW_VECTOR_MAGIC:
        raise PatchError(
            f"Initial SP = {sp:#010x} (expected {FW_VECTOR_MAGIC:#010x}).  "
            f"Not an SM2 firmware image?"
        )
    log(f"  Vector table OK  (SP = {sp:#010x})")

    # --- cmd_id sanity check ------------------------------------------------
    cmd_id = struct.unpack_from("<I", data, DISPATCH_CMD_OFFSET)[0]
    if cmd_id != EXPECTED_CMD_ID:
        raise PatchError(
            f"cmd_id at file offset {DISPATCH_CMD_OFFSET:#07x} = {cmd_id:#010x} "
            f"(expected {EXPECTED_CMD_ID:#010x}).  Dispatch table has moved?"
        )
    log(f"  cmd_id  OK  (0x{cmd_id:08X} @ fw+{DISPATCH_CMD_OFFSET:#07x})")

    # --- current fn_ptr check -----------------------------------------------
    fn_ptr = struct.unpack_from("<I", data, DISPATCH_ENTRY_OFFSET)[0]

    if fn_ptr == TEARDOWN_THUMB:
        if force:
            log(f"  fn_ptr already = {fn_ptr:#010x} (teardown) -- forcing re-patch")
        else:
            raise PatchError(
                f"fn_ptr at {DISPATCH_ENTRY_OFFSET:#07x} is already {fn_ptr:#010x} "
                f"(= TEARDOWN_THUMB).  Firmware appears already patched.  "
                f"Use force=True to skip this check."
            )
    elif fn_ptr != EXPECTED_FN_PTR:
        raise PatchError(
            f"fn_ptr at file offset {DISPATCH_ENTRY_OFFSET:#07x} = {fn_ptr:#010x} "
            f"(expected {EXPECTED_FN_PTR:#010x}).  "
            f"Unknown firmware variant -- refusing to patch."
        )
    else:
        log(
            f"  fn_ptr  OK  ({fn_ptr:#010x} = no-op stub @ "
            f"fw+{DISPATCH_ENTRY_OFFSET:#07x})"
        )

    # --- verify teardown fn looks sane (check first 2 bytes) ----------------
    teardown_file_off = TEARDOWN_ADDR - FW_BASE
    first2 = data[teardown_file_off : teardown_file_off + 2]
    # Thumb-2: 0x4B48 = LDR R0, [PC, #imm] (the teardown opens with a PC-relative load)
    if first2 != b"\x4B\x48":
        raise PatchError(
            f"Teardown fn at fw+{teardown_file_off:#07x} starts with "
            f"{first2.hex()} (expected 4B 48 = LDR R0,[PC,#imm]).  "
            f"Firmware layout has changed."
        )
    log(f"  teardown fn OK  (0x{TEARDOWN_ADDR:08X}: {first2.hex().upper()}...)")

    # --- apply patch --------------------------------------------------------
    ba = bytearray(data)
    assert ba[DISPATCH_ENTRY_OFFSET : DISPATCH_ENTRY_OFFSET + 4] == PATCH_OLD
    ba[DISPATCH_ENTRY_OFFSET : DISPATCH_ENTRY_OFFSET + 4] = PATCH_NEW
    log(
        f"  Patched  fw+{DISPATCH_ENTRY_OFFSET:#07x}: "
        f"{PATCH_OLD.hex().upper()} → {PATCH_NEW.hex().upper()}"
    )

    # --- write output -------------------------------------------------------
    output_path.write_bytes(bytes(ba))
    log(f"Written  {output_path}  ({len(ba):#x} bytes)")
    return output_path.resolve()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Patch SM2 firmware: redirect cmd 0x0053 to the DMA1 teardown fn "
            "so calling IOCTL 0x800B after a cam GPT session prevents the "
            "'ERR_DEVICE_NOT_CONNECTED' crash."
        )
    )
    p.add_argument(
        "input",
        nargs="?",
        default=str(DEFAULT_INPUT),
        metavar="INPUT_BIN",
        help=f"SM2 firmware binary (default: {DEFAULT_INPUT})",
    )
    p.add_argument(
        "output",
        nargs="?",
        default=None,
        metavar="OUTPUT_BIN",
        help="Patched output path (default: <input>_patched.bin)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Patch even if the file already looks patched",
    )
    args = p.parse_args(argv)

    try:
        out = patch_firmware(args.input, args.output, force=args.force)
        print(f"\nPatch applied.  Upload {out.name} to the SM2 via the Scanmatik "
              f"updater, then power-cycle the device before connecting.")
        return 0
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"I/O error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
