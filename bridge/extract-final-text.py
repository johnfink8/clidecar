#!/usr/bin/env python3
"""CLI wrapper: print a transcript's closing answer as JSON {"text", "already_sent"}.

The Stop hook (hook-final.py) calls transcript.extract_closing directly; this wrapper
keeps a standalone, scriptable entry point for tests and manual inspection. Exits
non-zero (loud) if the transcript can't be parsed — a silent empty forward would hide
a broken bridge.
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import transcript as t


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: extract-final-text.py <transcript.jsonl|->")
    turn = t.current_turn(t.load_rows(sys.argv[1]))
    text, already_sent = t.extract_closing(turn)
    print(json.dumps({"text": text, "already_sent": already_sent}))


if __name__ == "__main__":
    main()
