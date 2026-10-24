# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The converter as tools for the assistant and the MCP server: the same
job the window and ``ovtsuite convert`` build."""

from __future__ import annotations

import os


def register(reg) -> None:
    from ..ai.tools import ToolError
    from . import DEF_OUT, DIFF, IMG, VAL, ConvertError, Job, load_definition, run, sniff

    @reg.tool("inspect_file_format",
              "Say what a definition, value or binary file is, from its structure (not its "
              "name): TunerPro XDF, ASAP2 A2L, Bosch DAMOS, WinOLS project/map pack, OVT "
              "definition JSON, CSV map list, EcuFlash or RomRaider XML, a DCM or CDFX "
              "calibration value file, or a BIN / Intel HEX / S-record image; for a definition, "
              "how many tables it describes and whether it embeds an image; for a RomRaider "
              "file (many ROMs in one), every ROM's xmlid and id string, and with binary, "
              "which ROM that binary is; for a value file, how many values it holds.",
              {"type": "object", "required": ["path"], "properties": {
                  "path": {"type": "string", "description": "Absolute path of the file."},
                  "binary": {"type": "string", "description": "Optional binary: for a "
                             "RomRaider file, find which of its ROMs this is."}}},
              title="Inspect a file's format")
    def inspect_file_format(ctx, path, binary=None):
        if not os.path.isfile(path):
            raise ToolError("no such file: %s" % path)
        with open(path, "rb") as fh:
            data = fh.read()
        kind, t = sniff(data)
        out = {"kind": kind, "format": t, "bytes": len(data)}
        if kind == "definition" and t == "romraider":
            from . import fmt_romxml, image as _image
            roms = fmt_romxml.rom_ids(data)
            out["roms"] = [{"xmlid": x, "id_string": i,
                            "id_address": a, "description": d} for x, i, a, d in roms[:200]]
            out["rom_count"] = len(roms)
            if binary:
                if not os.path.isfile(binary):
                    raise ToolError("no such file: %s" % binary)
                with open(binary, "rb") as fh:
                    img = _image.read(fh.read()).flat()
                out["binary_is"] = fmt_romxml.identify(data, img)
            return out
        if kind == "definition":
            try:
                d = load_definition(data, path=path)
            except Exception as exc:
                raise ToolError("not readable: %s" % exc)
            out.update(tables=len(d.tables), title=d.title,
                       embeds_image=bool(d.images), checksum_blocks=len(d.checksums),
                       warnings=d.warnings[:10])
            if t == "ols":
                from . import embedded
                stem = os.path.splitext(os.path.basename(path))[0]
                out["summary"] = embedded.summary(path, data)
                out["embedded_images"] = [
                    {"key": e.key, "what": e.label, "bytes": e.size,
                     "default_name": e.name, "extracted_by_default": e.default,
                     "note": e.note} for e in embedded.images(data, stem)]
        elif kind == "values":
            from . import calvalues
            try:
                vs = calvalues.load(data)
            except Exception as exc:
                raise ToolError("not readable: %s" % exc)
            kinds = {}
            for p in vs.params:
                kinds[p.kind] = kinds.get(p.kind, 0) + 1
            out.update(values=len(vs.params), by_kind=kinds, title=vs.title,
                       made_from=vs.origin, warnings=vs.warnings[:10])
        return out

    @reg.tool("extract_embedded_images",
              "Write out the binaries a WinOLS project (.ols) embeds: the original WinOLS "
              "imported, every version made from it, and (by key) each memory area of a "
              "binary put together from several (e.g. an EDC16's flash and MPC). The bytes "
              "are written exactly as stored (format raw), or as Intel HEX / S-record. "
              "inspect_file_format lists what a project holds and each one's key.",
              {"type": "object", "required": ["sources"], "properties": {
                  "sources": {"type": "array", "items": {"type": "string"},
                              "description": "Absolute paths of the .ols projects."},
                  "out_dir": {"type": "string", "description": "Folder to write into "
                              "(default: beside the first project)."},
                  "only": {"type": "array", "items": {"type": "string"},
                           "description": "Keys (original, version1, original-area2 ...), "
                           "version names or file names; ['all'] for every one. Default: "
                           "the original and every version."},
                  "format": {"type": "string", "enum": ["raw", "bin", "hex", "srec"]},
                  "overwrite": {"type": "boolean", "description": "Replace files already "
                                "there (default false: they are left alone and listed "
                                "as skipped -- ask the user before passing true)."}}},
              read_only=False, title="Extract a WinOLS project's binaries")
    def extract_embedded_images(ctx, sources, out_dir=None, only=None, format="raw",
                                overwrite=False):
        from . import EXTRACT
        job = Job(sources=list(sources), target=EXTRACT, out=out_dir or "")
        job.options = {"extract": list(only) if only else None,
                       "extract_format": format or "raw",
                       "existing": "overwrite" if overwrite else "skip"}
        try:
            res = run(job)
        except (ConvertError, ValueError, OSError) as exc:
            raise ToolError(str(exc))
        return {"written": res.out, "what": res.values[1:], "out_dir": job.out,
                "skipped_existing": res.skipped,
                "read_back_differences": res.check}

    @reg.tool("convert_files",
              "Convert definitions (XDF, A2L, DAM, OLS, KP, JSON, CSV) to another definition "
              "format (also EcuFlash / RomRaider XML), or binaries (BIN, HEX, S-record) to another image format -- with no "
              "project open. Calibration values: a definition + binary with target dcm/cdfx "
              "exports every table's values; DCM/CDFX sources alone with target dcm/cdfx "
              "convert them; a definition + DCM/CDFX files + binary with an image target "
              "(bin/hex/srec) writes the values into a copy of the binary (correct_checksums "
              "to fix its checksums). port=true: the binary is another software version -- "
              "every table is placed in it by its own evidence (reference: the image the "
              "definition was made for, if at hand); unconfirmed tables go into an "
              "'Unverified' category. Two definitions with target diff: a CSV of tables "
              "added, removed, renamed, moved, resized, rescaled, retyped. XDF with patch_with "
              "(a changed binary): a TunerPro patch (XDFPATCH) turning the binary into it. "
              "RomRaider XML holds many ROMs: the one whose id string is in the binary is "
              "used, or name it with rom (its xmlid; inspect_file_format lists them). "
              "Optional: a binary "
              "to settle the base and stored sizes, "
              "a filter, translation, and (XDF) a TunerPro checksum plugin block. Writes the "
              "output file and returns what was written, what could not be expressed and "
              "the read-back check.",
              {"type": "object", "required": ["sources", "target"], "properties": {
                  "sources": {"type": "array", "items": {"type": "string"},
                              "description": "Absolute paths; several definitions are merged."},
                  "target": {"type": "string", "enum": list(DEF_OUT + IMG + VAL + (DIFF,))},
                  "out": {"type": "string", "description": "Output path (default: beside the "
                          "first source with the target's extension)."},
                  "binary": {"type": "string", "description": "Optional binary."},
                  "base": {"type": "integer"}, "size": {"type": "integer"},
                  "pattern": {"type": "string", "description": "Keep only tables whose "
                              "name/symbol/description matches this regular expression."},
                  "translate": {"type": "string", "enum": ["builtin", "online"]},
                  "to_lang": {"type": "string"},
                  "checksum": {"type": "string", "description": "XDF: none, keep, auto, a "
                               "module plugin id, or tp:<TunerPro plugin id>:<method index>."},
                  "correct_checksums": {"type": "boolean", "description": "Image targets: "
                                        "correct the binary's checksums afterwards (needs the "
                                        "platform's module)."},
                  "port": {"type": "boolean", "description": "The binary is another software "
                           "version than the definition was made for."},
                  "reference": {"type": "string", "description": "With port: the image the "
                                "definition was made for."},
                  "patch_with": {"type": "string", "description": "XDF: a changed binary; "
                                 "the patch turns the binary into it."},
                  "patch_split": {"type": "string", "enum": ["one", "table"]},
                  "rom": {"type": "string", "description": "RomRaider XML: the ROM to use, "
                          "by xmlid (default: found from the binary)."},
                  "overwrite": {"type": "boolean", "description": "Replace the output file "
                                "if it is already there (default false: it is left alone "
                                "and reported as skipped -- ask the user before passing "
                                "true)."}}},
              read_only=False, title="Convert files")
    def convert_files(ctx, sources, target, out=None, binary=None, base=None, size=None,
                      pattern=None, translate=None, to_lang="en", checksum="none",
                      correct_checksums=False, port=False, reference=None,
                      patch_with=None, patch_split="one", rom=None, overwrite=False):
        job = Job(sources=list(sources), target=target, out=out or "", binary=binary,
                  base=base, size=size, checksum=checksum or "none")
        job.options["existing"] = "overwrite" if overwrite else "skip"
        if correct_checksums:
            job.options["checksum"] = True
        if patch_with:
            job.options["patch_with"] = patch_with
            job.options["patch_split"] = patch_split or "one"
        if rom:
            job.options["rom"] = rom
        if port:
            job.options["port"] = True
            if reference:
                job.options["reference"] = reference
        if binary and job.out and os.path.abspath(job.out) == os.path.abspath(binary):
            raise ToolError("the output would replace the binary")
        if pattern:
            job.filters = {"pattern": pattern}
        if translate:
            job.translate = {"backend": translate, "source": "auto",
                             "target": to_lang or "en", "mode": "replace"}
        if job.out and os.path.abspath(job.out) in map(os.path.abspath, job.sources):
            raise ToolError("the output would replace a source file")
        try:
            res = run(job)
        except (ConvertError, ValueError, OSError) as exc:
            raise ToolError(str(exc))
        if res.skipped:
            return {"written": [], "skipped_existing": res.skipped,
                    "note": "the output is already there and was left alone; pass "
                            "overwrite=true (after asking the user) to replace it"}
        loss = dict(list(res.loss.items())[:40])
        return {"written": res.out, "tables_in": res.tables_in, "tables_out": res.tables_out,
                "image_size": res.size, "base": res.base, "notes": res.warnings[:20],
                "not_expressed": loss, "not_expressed_count": len(res.loss),
                "values": res.values[:20], "placement": res.placement,
                "differences": res.diff[:60], "differences_count": len(res.diff),
                "read_back_differences": res.check[:20]}
