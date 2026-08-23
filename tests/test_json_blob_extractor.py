"""Brace-balanced JSON blob extraction for verbose LLM/vision replies."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signal_parser import _extract_json_blob  # noqa: E402


def test_flat_object():
    assert _extract_json_blob('junk {"a": 1} trailing') == '{"a": 1}'


def test_nested_object_takes_first_top_level():
    raw = 'Sure: {"entry": 1.5, "meta": {"x": {"y": 2}}, "tps": [1, 2]} and {"b": 2}'
    blob = _extract_json_blob(raw)
    assert blob == '{"entry": 1.5, "meta": {"x": {"y": 2}}, "tps": [1, 2]}'
    assert json.loads(blob)["meta"]["x"]["y"] == 2


def test_braces_inside_strings_are_ignored():
    raw = 'x {"note": "a } b { c", "v": 3} y'
    assert json.loads(_extract_json_blob(raw))["v"] == 3


def test_escaped_quotes_inside_strings():
    raw = r'{"s": "say \"}\" ok", "v": 1}'
    assert json.loads(_extract_json_blob(raw))["v"] == 1


def test_no_blob_or_unbalanced_returns_none():
    assert _extract_json_blob("no json here") is None
    assert _extract_json_blob('{"a": {"b": 1}') is None
    assert _extract_json_blob("") is None
