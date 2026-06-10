"""#83 — `goosetown act reply_owner --ref=<id> <text>` must work.

The act subparser used to have a `nargs="*"` positional, so argparse intercepted
`--ref=...` as an unknown optional and errored ("unrecognized arguments"). It now
uses `nargs=REMAINDER` to capture everything after the action verbatim, and the
reply_owner handler parses a leading `--ref=` via `_parse_owner_ref`.
"""

from __future__ import annotations

from goosetown.cli import _build_parser
from goosetown.core import _parse_owner_ref


def test_remainder_captures_ref_and_text():
    args = _build_parser().parse_args(["act", "reply_owner", "--ref=abc123", "hello", "there"])
    assert args.command == "act"
    assert args.action == "reply_owner"
    # REMAINDER passes --ref through as opaque args for the handler to parse.
    assert args.args == ["--ref=abc123", "hello", "there"]


def test_remainder_plain_text():
    args = _build_parser().parse_args(["act", "reply_owner", "just", "text"])
    assert args.args == ["just", "text"]


def test_remainder_move_unaffected():
    args = _build_parser().parse_args(["act", "move", "library"])
    assert args.action == "move"
    assert args.args == ["library"]


def test_parse_owner_ref_extracts():
    ref, rest = _parse_owner_ref(["--ref=abc123", "hello", "there"])
    assert ref == "abc123"
    assert rest == ["hello", "there"]


def test_parse_owner_ref_none_when_absent():
    ref, rest = _parse_owner_ref(["hello", "there"])
    assert ref is None
    assert rest == ["hello", "there"]
