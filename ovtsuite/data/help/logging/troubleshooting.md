---
title: Troubleshooting logging
order: 80
---
# Troubleshooting logging

## Connecting

| Problem | What to try |
|---|---|
| The port or device box says nothing was found | Plug the cable in and press **Refresh**. On CAN only J2534 devices and an OpenPort 2.0 are listed; on K-line, serial/FTDI cables and J2534 devices. See [Cables and interfaces](../cables.md). |
| "Select a K-line cable or J2534 device for ..." | No cable is selected in that tab. |
| "Could not connect: ..." | Is the ignition on, and is the right controller chosen in **ECU**? On K-line, try **Disable Fast Init** (or untick it, for an EDC16) and **Low** speed. On CAN, check the bit rate (500000 on these cars). |
| An **EDC15** on a J2534 device does not connect, and the message mentions FIVE_BAUD_MOD | That device cannot do the 5-baud init a KW1281 ECU needs (it waits for a reply the EDC15 never sends). Use a KKL (FTDI/CH340) cable for this ECU. |
| An **EDC15** answers "...KWP2000 mode..." | It replied in KWP2000 — which on an EDC15 only does flash programming and generic OBD — not KW1281. Switch the ignition off for a few seconds and connect again. If it keeps doing it, the ECU is stuck in its boot loader: recover it with the flasher. |
| The ECU will not hold Medium or High | The logger falls back to 10400 and says so; that cable or ECU cannot hold the faster rate. Logging still works, more slowly. |
| The session keeps dropping | Each dropout is retried for 10 seconds. Check the cable and its connection; on K-line, try a lower **Speed**. |
| **Disconnect** takes several seconds | The logger closes the session within about two seconds. If the cable's driver does not let go within about 8 seconds, it closes the J2534 bridge (or cancels the K-line cable's read) itself; a J2534 driver loaded directly cannot be interrupted, and is left to finish in the background. **Stop** never does this — it leaves the connection open. |
| The transmission does not answer as **ZF6HP** | A 6HP28 does not serve the 6HP19's measuring blocks — use **ZF6HP-UDS**. |

## Cells and names

| Problem | What to try |
|---|---|
| Cells read "Group N / Cell M" | No file with a measuring-block map for this controller is open. Open the car's own file in a tab (OVT Logger: **Open project or binary...**). The hint under the cell table says which file is naming the cells. |
| The names look wrong | The open file is not the car's own software version, or — with two tabs open in the main window — check the hint names the file you expect. |
| A preset hint starts "Not in any block this ECU serves:" | The values listed are in no block this calibration offers (or only in blocks 100 and up, which presets leave out). The rest of the preset still logs. |
| A preset hint starts "No cells:" | Presets are worked out from the car's own file: open it and pick the preset again. |
| **Preset:** is greyed out (EDC15, ZF6HP-UDS, Aisin TRx) | There are no presets for that controller; the hint under it says why. Tick cells by hand. |

## While logging

| Problem | What to try |
|---|---|
| A whole column is "N/A" | That group is not served by this control unit, even though the file lists it. Browse it while connected: "Group N: not available on this ECU." confirms it. Pick the value from another group. |
| Very few rows per second | Every group costs one request per row. Use fewer groups (a preset picks the fewest), raise **Speed** on K-line, and give the transmission no more than two or three groups. |
| A value in the log reads like `(177 128 12)` | The app had no formula for that cell when it was logged. The raw bytes are kept and decoded on opening once a formula exists. |
| The laptop went to sleep mid-drive | The logger keeps the computer awake while connected; check that a power plan or lid-close action is not forcing sleep. |

## Reviewing logs

| Problem | What to try |
|---|---|
| "Cannot read ...: not a KWHack data log" | The file is neither the native nor the VCDS CSV layout. |
| A column is not on the chart | Only plain numbers are drawn; text values (and N/A) are not. A text cell that spells a number — the 6HP19's engaged gear, "3" — counts as a number (also in older logs, where it was written as `"3 "`); a letter such as "R" leaves a gap. |
| The tab says "Reading ..." for a while | A long log is still being read; the chart and table fill in when it is done. |

See also the general [Troubleshooting](../troubleshooting.md) page.
