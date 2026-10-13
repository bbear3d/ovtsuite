# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Embedded EDC15 RAM loaders (from KWHack ``KWHack.Core/vendor-kw1281test/EDC15``).

Provenance: the ``Loader-*`` programs are kw1281test's (MIT, Greg Menounos);
``Edc15FlashLoader.bin`` (EDC15P.FLD) comes from fjvva's ecu-tool
(https://github.com/fjvva/ecu-tool), which kw1281test credits and carries under
its MIT licence. Both are credited in Help > About (``ovtsuite.credits``).

The ECU only executes an uploaded loader whose C167 boot-ROM checksum comes out
to EFCD8631; ``patch_loader`` appends the 4-byte correction (and end pointer)
exactly like KWHack's ``Edc15VM.GetLoaderFromResource``. The whole-chip flash
loader is sent raw (KWHack's ``SendLoader`` does not patch it).
"""
import base64

# Edc15FlashLoader.bin: whole-chip flash loader (used raw, unpatched), 1016 bytes, sha256 41ec7ee7220d234d
_FLASH_LOADER_B64 = (
    "paUU4AAAPOAAAELgAACw4wAAAkcTAAABAAIAAwAEAAUABgAHAAgACQAKAAsADAANAA4AD4APoA/A"
    "DwAQRS99ZJvDvohP2Obw/ufaACThSGEtOUhiLRbm8QDm9EEBAEf0IwAtEkf0ogAtEkf0owAtF0f0"
    "pAAtGUf0pQAtKUf0NgAtLdoA6OAN4doAPuIN3uf+VQDaAIjh2gC+4rdIt7fn/lUA2gCI4Q3R2gAG"
    "4doAvuLgAvRBAgD28rT+5/6qANoAiOENw6nhCeHaAIjhDb7aAAbh2gDm4toABuENt9oAxOHaAAbh"
    "DbLbAIgQiCDm8QDm4RS5Qef0fwDkQQEA2gBe4ZggmBDbAIgQiCDm8QDm4RS5Qef0dgDkQQEA2gBe"
    "4ZggmBDbAIgQiCCIMObxAObaAJLh4ALxTvFuueEIEdoAouFIYD0JueEBbggRoAI99yFuQW4tAeAm"
    "mDCYIJgQ2wCIEIggiDDm8QDm4AKpQeEGmeEBbtoAiOGgAj368ebaAIjhmDCYIJgQ2wD3/rD+2gCS"
    "4dsAp1inp5q3/HDy97L+frfbAIgQ5vH//+AGp1inp4q3BHCgAT364BYNA/L3sv5+t5gQ2wCIEIgg"
    "iDCIQObzAObgAvRDAgD0MwMA9CMEAOAEqYMoRHwUKEEINadYp6eZ45nz2gAI4ggSGCCgBD32mECY"
    "MJggmBDbAIgQiHDm8aqq5/6qANoAdOPm8VVV5/5VANoAdOPm8aqq5/6gANoAdOOYcJgQ2gCE49oA"
    "VOPbAIgQiCCIMIhAiFDm8wDm4AL0QwIA9DMDAPQjBADgBPSDBQAJgbmDCDHn/jYAueMIMSmB2gCQ"
    "4rnjCDEIERggoAQ9+NoAXuGYUJhAmDCYIJgQ2wCIEKdYp6faAKTiZvH/P6nhmBDbAIgQiCBcImby"
    "/AAcIWgTcCH28gD+mCCYENsAiDDgA6dYp6cIMUbzAAE9+pgw2wCIMOADp1inpwgxRvMQAD36mDDb"
    "AIgQiCCIMIhw5vMA5ubyIADm8aqq5/6qANoAdOPm8VVV5/5VANoAdOPm8aqq5/6AANoAdOPm8aqq"
    "5/6qANoAdOPm8VVV5/5VANoAdOPm8aqq5/4QANoAdOPaAL7i5/6AANoAVOOYcJgwmCCYENsAiCCI"
    "cCbyGADx/mf/gADaAJDiZ/6AAEHvPfqYcJgg2wCIENoApOJm8f8/ueGYENsAiBDaAKTiZvH/P7hx"
    "mBDbAMPDw8PDw8PDw8PDw8PDw8PDw8PDw8OJxRLOx7OlpRTgAAA84AAAQuAAAADkAAACRxMAAAEA"
    "AgADAAQABQAGAAcACAAJAAoACwAMAA0ADgAPgA+gD8APABBFL31km8PDw8PDw8M="
)

# Loader-sector-erase.bin: per-sector erase/checksum/program loader (patched), 1356 bytes, sha256 96fcde02822b8ad9
_SECTOR_LOADER_B64 = (
    "paUU4AAAPOAAAELgAABM5QAAAkcTAAABAAIAAwAEAAUABgAHAAgACQAKAAsADAANAA4AD4APoA/A"
    "DwAQRS99ZJvDvohP2Obw/ue7d0hhLV1IYi0l5vEA5vRBAQBH9CMALSBH9DYALSBH9KIALSFH9KMA"
    "LSZH9KQALSdH9KUALS9H9KYALTBH9KcALTBH9KgALTFH9LAALTJH9LIALTO7OA3UygBW4w3RygDm"
    "4rs6Dc27OMoAnOLn/lUAu2q3SLe35/5VALtlDcG7LMoAnOLgAvRBAgD28rT+5/6qAA3zuyHKAMTj"
    "DeO7HbtuDeC7GsoAtuIN3LsWygDG4g3YuxLKAFjkDdTKANLkDaCp4QnhDdqIEIgg5vEA5uEUuUHn"
    "9H8ADQiIEIgg5vEA5uEUuUHn9HYA5EEBALscDRiIEIggiDDm8QDmuyjgAvFO8W654QgRuypIYD0J"
    "ueEBbggRoAI9+CFuQW4tAeAmmDCYIJgQywCIEIggiDDm8QDm4AKpQeEGmeEBbrsFoAI9+/HmuwEN"
    "7Pf+sP4NAKdYp6eat/xw8vey/n63ywCIEObx///gBqdYp6eKtwRwoAE9+uAWDQPy97L+frcN04/h"
    "j+Cf4Z/gu2a7Zbsj5/6sALtGuzLhDrtDuy+7I7sa5/6tALs9uyngBeby/gHgdbtSoAU9/bs/uyy7"
    "x6ACPfe7OrvDn+CP4LtKnuC7SI7gu0YNCI/gn+C7Qo7gu0Ce4Ls+ywCO4Ls7n+C7OY/gywC7Np/g"
    "uzSe4MsA4AeO4bsvn+C7LUrg94Ce4LspjuCP4csAjuC7JJ/guyKe4LsgywCIEOBxXIdcF0qI4Bi7"
    "4aABPfqO4A1UiBCO4eBx4Aef4LsOXBdK4PeAnuC7CaABPfeP4Y7gDUSIMObzAAENAogw4HOnWKen"
    "KDFIMD37mDDLALux5vesALvUu8BIcD35DQe7qeb3rgC7zLu4SHA9+coAluG7xruyygCW4bvCu64N"
    "oogQiCCIMIhA5vMA5uAC9EMCAPQzAwD0IwQA4ASpgyhEfBQoQQg1p1inp5njmfO7CQgSGCCgBD33"
    "mECYMJggmBDLAIgQiHDm8aqq5/6qAMoAPuTm8VVV5/5VALt+5vGqquf+oAC7eZhwmBC7fLtmywCI"
    "EIggiDCIQIhQ5vMA5uAC9EMCAPQzAwD0IwQA4AT0gwUACYG5gwgx5/42ALnjCDEpgbsKueMIMQgR"
    "GCCgBD35ygBw4ZhQDcCIEKdYp6e7BGbx/z+p4Q27iBCIIFwiZvL8ABwhaBNwIfbyAP4Nr4gQiCCI"
    "MIhw5vMA5ubyIADm8aqq5/6qALsw5vFVVef+VQC7K+bxqqrn/oAAuybm8aqq5/6qALsh5vFVVef+"
    "VQC7HObxqqrn/hAAuxfKAJzi5/6AALsDmHDqACDjiCCIcCbyGADx/mf/gAC7t2f+gABB7z37mHCY"
    "IMsAiBC7tmbx/z+54Q0FiBC7sGbx/z+4cZgQywCIEIggiDCIQIhw5vMA5ubyIADm8aqq5/6qALvl"
    "5vFVVef+VQC74Obxqqrn/oAAu9vm8aqq5/6qALvW5vFVVef+VQC70eAC9EMCAPQzAwD0IwQA5/4w"
    "ALvH5vT//6dYp6fKAJ7jR/7/AC0CKEE995hwmECYMJggmBDLAIgQiCCIMIhAiFCIYIhw5vYA5uAC"
    "9EYCAPQ2AwD0JgQA4AT0hgUA9HYGAPRmBwDgBadYp6fKAJ7j4Q8cFQBXCBEYIEATPfVAJD3z5vYA"
    "5uE+uebn/rIA5OYBAPHr5OYCAPHq5OYDAMoAcOGYcJhgmFCYQJgwmCCYEMsA"
)

# Loader-EEPROM.bin: EEPROM dump/batched-write loader (patched), 1182 bytes, sha256 6735f6a37051c3f2
_EEPROM_LOADER_B64 = (
    "paUU4AAAPOAAAELgAACe5AAAAkcTAAABAAIAAwAEAAUABgAHAAgACQAKAAsADAANAA4AD4APoA/A"
    "DwAQRS99ZJvDvohP2Obw/ufKAFrhSGEtbEhiLSXm8QDm9EEBAEf0IwAtIEf0NgAtIEf0ogAtIUf0"
    "owAtJkf0pAAtJ0f0pQAtL0f0pgAtMEf0pwAtMEf0qAAtM0f0qQAtNkf0qgAtPLtHDdPKAJzjDdDK"
    "ACzju0kNzLtHygC84uf+VQC7ebdIt7fn/lUAu3QNwLs7ygC84uAC9EECAPbytP7n/qoADfO7MMoA"
    "CuQN47ssu30N4LspygAe48oA1uIN2rsjygAe48oA5uIN1LsdygAe4+AE9IECAMoABuMNy7sUygAe"
    "4+AE9IECAMoAEuMNwqnhCeENy4gQiCDm8QDm4RS5Qef0fwANCIgQiCDm8QDm4RS5Qef0dgDkQQEA"
    "uxwNGIgQiCCIMObxAOa7KOAC8U7xbrnhCBG7KkhgPQm54QFuCBGgAj34IW5Bbi0B4CaYMJggmBDL"
    "AIgQiCCIMObxAObgAqlB4QaZ4QFuuwWgAj378ea7AQ3s9/6w/g0Ap1inp5q3/HDy97L+frfLAIgQ"
    "5vH//+AGp1inp4q3BHCgAT364BYNA/L3sv5+tw3Tj+GP4J/hn+C7ZrtluyPn/qwAu0a7MuEOu0O7"
    "L7sjuxrn/q0Auz27KeAF5vL+AeB1u1KgBT39uz+7LLvHoAI997s6u8Of4I/gu0qe4LtIjuC7Rg0I"
    "j+Cf4LtCjuC7QJ7guz7LAI7guzuf4Ls5j+DLALs2n+C7NJ7gywDgB47huy+f4LstSuD3gJ7guymO"
    "4I/hywCO4Lskn+C7Ip7guyDLAIgQ4HFch1wXSojgGLvhoAE9+o7gDWeIEI7h4HHgB5/guw5cF0rg"
    "94Ce4LsJoAE994/hjuANV4gw5vMAAQ0CiDDgc6dYp6coMUgwPfuYMMsAu7Hm96wAu9S7wEhwPfkN"
    "B7up5veuALvMu7hIcD35ygC24bvGu7LKALbhu8K7rg2iSEAtA7vlKEEN+8sASEAtA7vnKEEN+8sA"
    "j+GP4J/hn+C7yrvJywCIEIggiDCIQObzAObgAvRDAgD0MwMA9CMEAOAEqYMoRHwUKEEINadYp6eZ"
    "45nzuwkIEhggoAQ995hAmDCYIJgQywCIEIhw5vGqquf+qgDKAITk5vFVVef+VQC7fubxqqrn/qAA"
    "u3mYcJgQu3y7ZssAiBCIIIgwiECIUObzAObgAvRDAgD0MwMA9CMEAOAE9IMFAAmBuYMIMef+NgC5"
    "4wgxKYG7CrnjCDEIERggoAQ9+coAkOGYUA3AiBCnWKenuwRm8f8/qeENu4gQiCBcImby/AAcIWgT"
    "cCH28gD+Da+IEIggiDCIcObzAObm8iAA5vGqquf+qgC7MObxVVXn/lUAuyvm8aqq5/6AALsm5vGq"
    "quf+qgC7IebxVVXn/lUAuxzm8aqq5/4QALsXygC84uf+gAC7A5hw6gBm44ggiHAm8hgA8f5n/4AA"
    "u7dn/oAAQe89+5hwmCDLAIgQu7Zm8f8/ueENBYgQu7Bm8f8/uHGYEMsA"
)

# Loader-boot-stage1.bin: boot-mode RAM program (sent raw), 32 bytes, sha256 4e897f43d06cbd57
_BOOT_STAGE1_B64 = (
    "5lilAJq3/nDm8AD4freat/5wpACy/obwffk9+OoAAPg="
)

# Loader-boot-monitor.bin: boot-mode RAM program (sent raw), 382 bytes, sha256 2cb845dffe2c89a7
_BOOT_MONITOR_B64 = (
    "frd+tufxsAC7PbtD8bBH+yAAPQTn8bEAuzUNMEf7IQA9C+fxsQC7Lrs8u0ioNvEWuynxF7snDSJH"
    "+yIAPQjn8bEAuyC7Lrs1uzm4Ng0XR/sjAD1D5/GxALsV4Qq7IrspoAM9A/Eauw4NCbsoqRZRobsJ"
    "Dfbn8b4AuwUNx+fxsgC7AQ3D9/Gw/pq3/nB+t362ywCnWKenmrf+cH638/Cy/ssAu/fxILv18TC7"
    "8/FA4QXLALvv8WC77fFwywDwYfByfOZcJ3B29vcE/vBhCBEYIGb2/z929gCAywBH+yQAPRnn8bEA"
    "u8/hCuELu9u74qADPQXxGrvHSbA9vQ3Au8rxgFGou9y5hqmWQZg9AQ3w4RsN7kf7JQA9HufxsQC7"
    "s7vB8vYQ/gb2EADgB7uzuQYIYYD3Pfu7C/L2EP4G9hAA4AepFrugCGGA9z37DZj89+z37PHLAEf7"
    "JgA9BufxsQC7krVKtbUNi0f7JwA9BefxsQC7ibdIt7fn8b8Au4QNgg=="
)

# Loader-boot-flash.bin: boot-mode RAM program (sent raw), 408 bytes, sha256 bdbd5ef54115af8e
_BOOT_FLASH_B64 = (
    "7PDs8ezy7PPs9Oz17Pbs90iA6iBi9kiB6iCy9kiC6iAc90iG6iBs9+Af/Pf89vz1/PT88/zy/PH8"
    "8MsA5vOqCub1qgDcArhT5vNUBeb1VQDcArhTywDgBub18ADcArhWywBI0D0D4A/qACr28CnKAFb2"
    "ygA89ubzqgrm9aAA3AK4U6hc3Am4W9wKqEvwZFBlmvYBcA353AqoS0BFPQQIsgjCKNIN3vApygBW"
    "9uAf6gAq9vApygBW9soAPPbm86oK5vWAANwCuFPKADz25vUwANwCuFvgBuAHp1inpwhhGHBG9wAI"
    "PQTm/zEA6gAq9twKqEua9PFw8DzcCqhDRvT//z0HQDstAigyDffgD+oAKvbwKcoAVvbgH+oAKvbw"
    "KcoAVvbKADz25vOqCub1gADcArhTygA89ubzqgrm9RAA3AK4U+AG4AenWKenCGEYcEb3AAg9BOb/"
    "MQDqACr24APcCqhDmvTwcOAP6gAq9vApygBW9soAPPbm86oK5vWQANwCuFPwO1wT3AqoQ/ApygBW"
    "9vCU4A/qACr2"
)


def boot_stage1() -> bytes:
    """32-byte C167 BSL stage-1 (the BSL copies exactly 32 bytes to 0xFA40)."""
    data = _decode(_BOOT_STAGE1_B64)
    if len(data) != 32:
        raise RuntimeError("boot stage-1 is %d bytes, expected 32" % len(data))
    return data


def boot_monitor() -> bytes:
    """Boot-mode monitor (stage-2), streamed right after stage-1's ready marker."""
    return _decode(_BOOT_MONITOR_B64)


def boot_flash_helper() -> bytes:
    """Boot-mode flash helper (program / chip-erase / autoselect), loaded at 0xF600."""
    return _decode(_BOOT_FLASH_B64)


def _decode(s: str) -> bytes:
    return base64.b64decode(s)


def flash_loader() -> bytes:
    """Edc15FlashLoader.bin, 1016 bytes, sent raw via requestDownload 0x40E000/0x420."""
    data = _decode(_FLASH_LOADER_B64)
    if len(data) != 1016:
        raise RuntimeError("Edc15FlashLoader is %d bytes, expected 1016" % len(data))
    return data


def sector_loader() -> bytes:
    """Loader-sector-erase.bin, checksum-patched for the boot ROM."""
    return patch_loader(_decode(_SECTOR_LOADER_B64))


def eeprom_loader() -> bytes:
    """Loader-EEPROM.bin, checksum-patched for the boot ROM."""
    return patch_loader(_decode(_EEPROM_LOADER_B64))


# ---- boot-ROM checksum correction (KWHack Edc15VM.GetLoaderFromResource) ----
def _get_word(buf, ix):
    return buf[ix] | (buf[ix + 1] << 8)


def _ror(v, n):
    n &= 0xF
    return ((v >> n) | (v << (16 - n))) & 0xFFFF


def _rol(v, n):
    n &= 0xF
    v = ((v << n) | (v >> (16 - n))) & 0xFFFF
    carry = 0 if ((v & 1) == 0 or n == 0) else 1
    return v, carry


def loader_checksum(buf, r6=0xEFCD, r1=0x8631):
    """EDC15 sub_1584 32-bit rolling checksum (returns (r6, r1))."""
    r3, r0 = 0, len(buf)
    while True:
        r1 ^= _get_word(buf, r3); r3 += 2
        r1, c = _rol(r1, r6)
        r6 = (r6 - _get_word(buf, r3) - c) & 0xFFFF; r3 += 2
        r6 ^= r1
        if r3 >= r0:
            break
        r1 = (r1 - _get_word(buf, r3) - 1) & 0xFFFF; r3 += 2
        r1 = (r1 + 0xDAAD) & 0xFFFF
        r6 ^= _get_word(buf, r3); r3 += 2
        r6 = _ror(r6, r1)
        if r3 >= r0:
            break
    return r6, r1


def patch_loader(raw: bytes) -> bytes:
    """Pad to a multiple of 8 (leaving 4 trailing bytes), patch the end pointer
    at 0x0E/0x0F and append the correction so the checksum is EFCD8631."""
    n = len(raw) + 4
    n = (n + 7) // 8 * 8
    buf = bytearray(n)
    buf[:len(raw)] = raw
    end = (0xE000 + n) & 0xFFFF
    buf[0x0E] = end & 0xFF
    buf[0x0F] = end >> 8
    r6, r1 = loader_checksum(bytes(buf[:-4]))
    pad_h = (0xDF9B ^ r6) & 0xFFFF
    pad_l = (r1 - 0xAB85) & 0xFFFF
    buf[-4:] = bytes([pad_l & 0xFF, pad_l >> 8, pad_h & 0xFF, pad_h >> 8])
    return bytes(buf)
