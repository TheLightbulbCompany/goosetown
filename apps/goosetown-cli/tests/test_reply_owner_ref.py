"""#83 — `goosetown act reply_owner --ref=<id> <text>` must parse.

The act subparser used to only have a positional `args nargs="*"`, so argparse
intercepted `--ref=...` as an unknown optional and errored ("unrecognized
arguments: --ref=..."), making the documented owner-DM threading syntax unusable.
A real `--ref` optional now captures it into `args.ref`.
"""

from __future__ import annotations

from goosetown.cli import _build_parser


def test_act_reply_owner_ref_parses():
    args = _build_parser().parse_args(["act", "reply_owner", "--ref=abc123", "hello", "there"])
    assert args.command == "act"
    assert args.action == "reply_owner"
    assert args.ref == "abc123"
    assert args.args == ["hello", "there"]


def test_act_reply_owner_without_ref():
    args = _build_parser().parse_args(["act", "reply_owner", "just", "text"])
    assert args.ref is None
    assert args.args == ["just", "text"]


def test_act_move_unaffected():
    args = _build_parser().parse_args(["act", "move", "library"])
    assert args.action == "move"
    assert args.args == ["library"]
    assert args.ref is None
