# Contributing

Contributions are welcome: bug reports, fixes and new features for the base
app.

## Licence of contributions

OVT-Suite is licensed under the [Mozilla Public License 2.0](LICENSE). By
submitting a contribution you agree that it is licensed under the MPL-2.0
too ("inbound = outbound"), and you certify the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):
that you wrote it, or otherwise have the right to submit it under that
licence.

Sign off every commit to say so:

    git commit -s

which adds a line like `Signed-off-by: Your Name <you@example.com>`.

Do not submit code, data or files you may not share: control-unit binaries,
A2L/DAMOS files, factory definitions, or code copied from projects whose
licence does not allow it.

## Source files

Every source file starts with its SPDX licence header.
`python tools/export_public.py headers` adds it where it is missing.

## Tests

Run the tests for what you changed, by file (see `AGENTS.md`):

    python -m pytest tests/test_search.py -q
