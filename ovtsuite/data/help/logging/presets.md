---
title: Presets and how to drive them
order: 30
context: UserPresetDialog
---
# Presets and how to drive them

A preset answers one question. It ticks the fewest measuring blocks of the
open calibration that carry the values the question needs, adds the values
that sharpen the answer when they fit, and shows how to drive for it in the
hint under **Preset:**. The engine presets are the logs that
**Tools > Tune from logs...** analyses; the transmission presets are for
questions about shifting, lockup and pressure.

The list in **Preset:** depends on the **ECU**:

| ECU | Presets offered |
|---|---|
| **EDC16** | the engine presets below |
| **EDC15** | the engine presets whose values its blocks carry — typically **Boost control**, **Full-load fuelling**, **Cruise efficiency** and, on unit-injector (PD) engines, **Injection timing** and **Warm-up running** — then the [EDC15 presets](#edc15-presets) below. EDC15 values are recognised by the ECU's own message numbers (engine speed, injected quantity, air mass, boost, pedal, temperatures...), which are the same on every EDC15. A preset that needs a value none of the file's blocks carries (exhaust temperature, say) is not listed. |
| **ZF6HP** | the transmission presets below |
| **ZF6HP-UDS**, **Aisin TRx** | none built in — the hint says why; tick the cells by hand, and [save them as a preset of your own](#your-own-presets) to pick them again. Until you have one, the **Preset:** box is greyed out. |

## How a preset picks blocks

1. It looks up, in the calibration that names the cells, which blocks carry
   each value it needs.
2. It picks the fewest blocks that cover all of them, then — if they still fit
   — blocks for the wanted extras. Each preset has a block limit (six for most
   engine presets, two or three for the others), because every extra block
   costs sample rate.
3. On an EDC16 it only uses blocks 1–127. The calibration's block table
   has rows up to 255, but the ECU sends the rows from 128 up as the second
   half of a lower block — row 133 arrives as cells 5–8 of block 6 — and
   answers "N/A" if they are asked for directly. So those values are planned
   through the lower block, and a block can contribute up to eight values.

The hint starts with the blocks chosen ("Blocks 1, 10, 11."). When a needed
value is in no usable block it starts with "Not in any block this ECU
serves:" followed by the names of the missing values (for example "Boost
(specified)").

> Presets follow the calibration's own block layout, so open the car's own
> file before choosing one. The same preset can pick different blocks on two
> software versions.

Your own presets follow the built-in ones, under **Your presets** — see
[Your own presets](#your-own-presets).

### Optimised blocks

The blocks a calibration comes with spread a preset's values over several of
them, and each one read costs sample rate. **Tools > Build logging blocks...**
(or **Optimise...** under **Preset:**) lays the file's blocks out so the preset
reads as few as possible -- often one or two. Once the file is written to the
ECU, the preset reads those blocks, after checking once that the ECU has
them. See [Optimised logging blocks](optimised-blocks.md).

## Your own presets

Tick the cells you want, then press **Save as...** beside **Preset:** and
give the set a name. It is listed at the end of the box under **Your
presets** and is picked like a built-in one; it is kept for the next session.

- **Per controller.** A preset belongs to the **ECU** it was saved for, as the
  built-in lists do: an EDC16 preset is not offered for an EDC15 or the
  transmission. Each controller's names must be different (capitals do not
  count); an empty name is not accepted.
- **What it keeps.** Each cell's block and position, and the signal it
  carried. On a calibration that keeps that signal in another block, the cell
  is found there and the hint says how many moved. A cell no block the ECU
  serves carries any more is left out, and the hint starts with "Not in any
  block this ECU serves:" and its name, as for a built-in preset. When
  nothing names the controller's cells, they are logged by block and cell.
  If the selection held **End of injection (calculated)**, the preset adds it
  again whenever its two cells are there.
- **Edit...** changes the name of the preset chosen and, with **Take the
  cells selected now** ticked, replaces its cells with the ones ticked in the
  logger now. **Rename...** changes only the name. **Delete** asks first and
  cannot be undone; the cells stay ticked.
- **Built-in presets cannot be changed.** Edit..., Rename... and Delete are
  greyed out for them. To change one, pick it, adjust the ticked cells and
  **Save as...** a copy of your own. Saving under a name you already use asks
  before replacing that preset.
- Picking one of your presets adds its name to the log file name, as a
  built-in preset adds its own.

## Engine presets (EDC16)

| Preset | Needs | Drive like this |
|---|---|---|
| **Boost control (target + actuator pilot)** | engine speed, injected quantity, boost actual and specified, boost actuator duty | Coolant at 70 °C or more. Tiptronic, hold 3rd: three full-throttle pulls from 1300 to 4500 rpm, then the same in 4th. Add 2–3 minutes of steady part throttle at 1800–3000 rpm. |
| **Full-load fuelling and active limiter** | engine speed, injected quantity, air mass, pedal | Coolant at 70 °C or more. Hold 3rd: full throttle from 1300 rpm to the limiter, twice; repeat in 4th. Let the intercooler cool between runs. |
| **Injection timing (start + duration)** | engine speed, injected quantity, start of injection, injection duration, and **End of injection (calculated)** | The same pulls as the fuelling log, plus a few seconds at steady 2000, 2500 and 3000 rpm part throttle. |
| **Exhaust temperature (pre-turbine sensor)** | engine speed, injected quantity, pre-turbine temperature | Coolant at 70 °C or more, 4th gear: a full-throttle pull from 1500 rpm to the limiter, then full throttle up a long grade if you have one. |
| **Warm-up running (coolant below 70 deg C)** | engine speed, injected quantity, coolant, start of injection, duration, and End of injection (calculated) | Start from cold and log without stopping until coolant passes 75 °C: normal driving, steady part throttle at 1500–2500 rpm and a few firm accelerations in 3rd in each coolant band. |
| **Cold take-off (active limiter, boost control, intake air)** | engine speed, injected quantity, coolant, driver's-wish / protection / smoke-limit torque, boost actual and specified, boost actuator duty, intake air temperature | Log from a cold start (coolant under 40 °C) until it passes 70. In D, several take-offs from a full stop, some at half pedal and some firm, with a few seconds at idle in gear before each; then the same take-offs once warm for comparison. Four blocks (1, 7, 8 and 11 on the LQ). |
| **Gearbox torque intervention (ECU side)** | gearbox torque reduction, torque increase (MSR), the gearbox-intervention and drag-torque status flags | Coolant at 70 °C or more. A dozen closed-throttle coast-downs through every gear with the foot fully off, then half a dozen kickdowns from steady cruise in 4th and 5th, with a few seconds of steady driving between events. Limited to three blocks. |
| **Cruise efficiency (before/after comparison)** | engine speed, injected quantity, vehicle speed | Coolant at 70 °C or more, flat road: hold 60, 80, 100 and 120 km/h for at least 30 s each. Use the same route and direction for the before and after logs. |
| **Injector delivery (new injectors or nozzles, unit-injector engines)** | engine speed, injected quantity, air mass, lambda (the ECU's own probe, block 40) | For **Tools > Injector setup > From logs**. Coolant at 70 °C or more: two minutes of idle in neutral with everything off; steady 60, 80, 100 and 120 km/h in a fixed gear, 30 s each; steady part throttle at 2000, 2500 and 3000 rpm; then two full-throttle pulls in 3rd and 4th from 1300 rpm. Log the same idle and steady speeds before the new hardware goes in if you can: those points need no lambda reading. |

**End of injection (calculated)** is not read from the ECU. Whenever a preset
logs both the start of injection and the injection duration, the logger adds
this column and works it out for every row: duration − start, in degrees
**after** TDC (negative = before TDC). The cell list shows it as **calc**. It
is saved in the log like any other column, and uses the same convention as
Tune from logs and [End of injection](../tools/end-of-injection.md).

Ticking cells by hand works the same way: as soon as the selection holds both
a start and a duration cell, the calculated column is added (**Remove** takes
it out; unticking either cell drops it). On an **EDC15** unit-injector (PD)
ECU those are the start-of-delivery and delivery-duration setpoints, block 4
cells 2 and 3 on most software. The start comes in as degrees with *BTDC* or
*ATDC* after it; a start after TDC counts as negative.

## EDC15 presets

Offered after the engine presets above, each only when the open file's
blocks carry what it needs: the smooth-running and start-of-injection
presets need a distributor-pump (VE) engine, and a unit-injector (PD)
engine gets **Injection timing** and **Warm-up running** from the list above
instead. On a 038906012FD, for example, the list holds eleven presets.

| Preset | Needs | Drive like this |
|---|---|---|
| **Fuel limiters (driver demand, torque and smoke limits)** | engine speed, injected quantity, driver-demand, torque-limit and smoke-limit quantities | Coolant at 70 °C or more. Full-throttle pulls in 3rd and 4th from 1300 rpm to the limiter. The injected quantity follows the lowest of the three, so the log shows which one holds it. |
| **Injection timing (start of injection, specified and actual)** | engine speed, injected quantity, start of injection specified and actual | Coolant at 70 °C or more. Idle a few seconds, steady part throttle at 2000, 2500 and 3000 rpm, then two full-throttle pulls. Also logs the timing valve duty when it fits. |
| **EGR and air mass (specified and actual)** | engine speed, air mass specified and actual, EGR duty | Coolant at 70 °C or more. A minute at idle, then steady part throttle at 1500–2500 rpm in 4th and 5th, and a few gentle accelerations. |
| **Cold start and glow plugs** | engine speed, coolant, glow status, pre-glow time | Start logging cold with the ignition on, before cranking, and keep going through the start and the first minutes of idle. |
| **Idle smoothness (per-cylinder corrections)** | engine speed, the four smooth-running quantities | Warm engine idling in neutral: a minute with everything off, then a minute with the A/C and lights on. |
| **Temperatures (coolant, fuel, intake air)** | coolant, fuel and intake-air temperature | From a cold start through normal driving until coolant has been at 90 °C for a few minutes. |
| **Cruise control and road speed** | vehicle speed, cruise-control switches and status | Set cruise at 80 km/h, step it up and down, cancel it with the brake and the clutch, and resume. |
| **Injector delivery, before / after (unit-injector engines)** | engine speed, injected quantity, injection duration | For **Tools > Injector setup > From logs** on a PD engine. Coolant at 70 °C or more: two minutes of idle in neutral with everything off, then steady 60, 80, 100 and 120 km/h in a fixed gear, 30 s each, same road and direction. Log the same points before the change (old injectors) and after it. |
| **Injection pump calibration (quantity adjuster voltage)** | engine speed, injected quantity, the adjuster voltage (actual); its setpoint when a block shows it | VE pump engines, for **Tools > Injector setup > From logs**. Coolant at 80 °C or more: two minutes of idle with everything off, the same steady speeds as a log from before a pump or nozzle change if you have one, then two full-throttle pulls in 3rd from 1500 rpm to the limiter. |

## Transmission presets (ZF6HP — 6HP19)

![A ZF 6HP19 on CAN with the shift-map discovery preset applied](../images/log-tcu-preset.png)

The transmission answers only a few blocks a second, so these presets use two
or three blocks at most. Unless the hint says otherwise, log with the fluid at
60 °C or more.

| Preset | Needs | Drive like this |
|---|---|---|
| **Shift points (engaged gear + output speed)** | engaged gear, output shaft speed, pedal | Hold the pedal genuinely still — 20, 30, 40, then 60 % — and let the car accelerate up through the gears. Two runs per pedal position, then coast to a stop in gear for the downshifts. A shift taken while lifting measures nothing. |
| **Shift map discovery (which schedule a program reads)** | turbine speed, output shaft speed, shift program | For probe calibrations with flat shift lines: one unhurried pull per selector position, D then S, each far enough to reach 4th. Pedal position does not matter. If the program number changes mid-pull, the run is void. |
| **Driver-type downshift offset (pedal-triggered downshifts)** | turbine speed, output shaft speed, brake, driving mode | For downshift-offset probes. Stay in D, no paddles, never touch the brake during an event. From a steady 55–75 km/h in 5th, squeeze to about a third pedal and hold, a dozen times; then drive briskly for a minute and repeat in 4th at 45–55 km/h. Three blocks. |
| **Torque intervention during shifts (asked vs delivered)** | engine torque, turbine speed, output shaft speed | A dozen part-throttle upshifts at 25, 35 and 50 % pedal, a dozen closed-throttle coast-downs, then half a dozen kickdowns from steady 4th and 5th, with steady driving between events. |
| **Pull-away gear (which gear it actually starts in)** | engaged gear, turbine speed, output shaft speed | From cold, in D: half a dozen standing starts at different pedal openings, including two gentle ones, with a full stop between each. |
| **Shift program selection (which schedule is live)** | shift program, dynamic shift program, pedal | Start cold and log for at least fifteen minutes without stopping: gentle town driving for five, then normal, then a few firm accelerations. |
| **Converter lockup and slip** | converter clutch status, converter slip, pedal | In each of 3rd, 4th and 5th, hold a speed just under lockup, ease on until it locks, then back off until it releases; then a few part-throttle accelerations. |
| **Shift pressure and solenoid currents** | pressure control valve 1 and 2 currents, engaged gear | Moderate accelerations through every upshift, then kickdowns from steady cruise. Watch the valve currents across each shift. |

The hint in the logger has the full driving notes for each preset; the table
above is a summary.

## Tips

- Leave a few seconds of steady driving between events, so each one has a
  baseline before it.
- Use **Pause** while you turn round or wait at lights rather than stopping:
  the session stays open and no rows are written.
- Log before and after a change with the same preset, on the same road.

Next: [Recording and the live chart](recording.md).
