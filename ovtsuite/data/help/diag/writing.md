---
title: Writing to a module
order: 30
---
# Writing to a module

Login, adaptation, coding, basic settings and actuator tests all run on the
session you opened with **Connect**: log in once and the functions that need
it work until you disconnect. Every write follows the same pattern: the
current value is read first, the change is shown and confirmed, written, read
back, and recorded in the vehicle's journal with the result.

**Not yet tested on a car.** Treat every write as experimental.

## Login and security access

Many adaptations and codings are refused until the module has seen a login.

- **Login** (KW1281 and KWP2000) takes the five-digit code -- the usual
  ones are in the module's definitions, and some are per car (the SKC).
- **Security access** (KWP2000 `27`, and UDS) answers the module's seed with
  the key its definitions name. A module whose key the suite does not know
  says so.

The state line under the module name shows *logged in* once it succeeded.

## Adaptation

Enter a channel and press **Read**: the current value, and its meaning when
the definitions describe the channel. **Test** sends a new value for the
module to use without saving it (watch the effect in a live group);
**Save** writes it, with your workshop code. The journal keeps the old and
new value, so a change can be undone later.

## Coding

**Short coding** (the five- or seven-digit number) is written with the
workshop code; the module re-identifies afterwards and the new coding is
read back. The definitions tab explains the digits where a pack describes
them.

**Long coding** (the byte string of later modules) is edited byte by byte or
bit by bit, with the bits labelled where the pack knows them. The new coding
must be the same length as the current one; a module that refuses the write
is left as it was.

## Basic settings

A basic setting is a measuring group that the module *acts on* while it is
displayed -- throttle body adaptation, headlight range, injector matching.
**Start** enters the group; the values refresh until **Stop**. Read the
module's own instructions for the conditions (engine off, warm, idling).

## Actuator tests

**Next** steps through the module's actuator sequence, one output at a time,
and shows which one is active; **Stop** ends the sequence. Keep hands and
tools clear of what moves.
