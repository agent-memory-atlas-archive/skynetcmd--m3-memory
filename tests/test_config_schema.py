"""The generated JSON Schema agrees with the loader (one registry, two readers).

A file the loader accepts must validate against the schema, and the schema must
reject what the loader rejects, for the shapes the schema can express.
"""
from __future__ import annotations

import json
import os

import jsonschema  # via mcp (a core dependency: jsonschema>=4.20.0), so never skipped
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _schema():
    with open(os.path.join(_ROOT, "docs", "schema", "m3_config.schema.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_a_file_the_loader_accepts_validates():
    jsonschema.validate({"schema_version": 1, "db": {"backend": "postgres"},
                         "secrets": {"file": "~/s.toml"}}, _schema())


@pytest.mark.parametrize("doc", [
    {"db": {"backend": "postgre"}},           # not one of
    {"db": {"bakend": "sqlite"}},             # unknown key
    {"secrets": {"file": 7}},                 # wrong type
    {"M3_PRIMARY_PG_URL": "postgresql://x"},  # a secret at the top level
])
def test_the_schema_rejects_what_the_loader_rejects(doc):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, _schema())
