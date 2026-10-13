# Add-ons

Files the app can use but that are **not shipped in any installer, module or
package**. Each is offered as an optional download by the Windows installers.

| File | What | Used by |
|---|---|---|
| `edc16_bootdb.bin` | EDC16 boot-sector templates (Bosch boot code) for the flasher's *virtual boot read* | `ovtsuite/bootdb.py` |
| FTDI's CDM driver (.cab) | FTDI's USB driver (VCP + D2XX) for FTDI K-line cables, downloaded from Microsoft's Update Catalog and installed with `pnputil` (asks for administrator permission). Offered, ticked, only when the driver is missing. | `ovtsuite/flash/ftdi.py` (D2XX route) |

## Publishing one

1. Upload the file somewhere reachable over HTTPS.
2. Put that URL in `addons.json` (`edc16_bootdb.url`).
3. Build. `build.ps1` hashes the file here (SHA-256) and passes the URL and hash to
   the installers; the download is refused if the hosted file does not match.

With no URL the installers simply do not offer it.

### FTDI's driver

`ftdi_driver` in `addons.json` names the driver package as Microsoft's Update
Catalog publishes it (search the catalog for "FTDI - USB - <version>"; the
download link is a `.cab` on `catalog.s.download.windowsupdate.com`), so there
is nothing to upload. It is the same Microsoft-signed package Windows Update
installs. FTDI's own zip on ftdichip.com cannot be used: its Cloudflare check
answers 403 to anything that is not a browser.

The installer needs only the file's SHA-256: pin it in `ftdi_driver.sha256`, or
drop the file into this folder (`*.cab` and `*.zip` are git-ignored) and let
`build.ps1` hash it. Pin the new hash whenever you change the url.

## Where the app looks

`$OVTSUITE_BOOTDB` (a file or folder), then the user's config folder's
`addons` (`%APPDATA%\OVT-Suite\addons`, `~/.config/ovtsuite/addons`), then the
installation's `addons` folder beside the executable (where the installer puts
it), then this folder when running from source. Linux packages do not download
anything: copy the file into `~/.config/ovtsuite/addons`.
