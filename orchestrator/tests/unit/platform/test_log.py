"""PLAT-006: `opengrid.platform.log` -- one JSON object per line, idempotent handler installation,
exception info included."""

from __future__ import annotations

import json
import logging

from opengrid.platform.log import JsonFormatter, configure_logging


def test_json_formatter_renders_expected_fields():
    logger = logging.getLogger("test-json-formatter")
    record = logger.makeRecord("test-json-formatter", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test-json-formatter"
    assert "ts" in payload


def test_json_formatter_includes_extra_fields():
    logger = logging.getLogger("test-json-formatter-extra")
    record = logger.makeRecord(
        "test-json-formatter-extra", logging.INFO, __file__, 1, "msg", (), None, extra={"bank_id": "b1"}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["bank_id"] == "b1"


def test_json_formatter_includes_exc_info():
    try:
        raise ValueError("boom")
    except ValueError:
        logger = logging.getLogger("test-json-formatter-exc")
        record = logger.makeRecord(
            "test-json-formatter-exc", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info()
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exc_info"]


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    before = len([h for h in root.handlers if isinstance(h.formatter, JsonFormatter)])
    configure_logging("proc-a")
    configure_logging("proc-a")
    after = len([h for h in root.handlers if isinstance(h.formatter, JsonFormatter)])
    assert after == max(before, 1)
    assert after - before <= 1


def test_configure_logging_returns_named_logger():
    logger = configure_logging("my-process")
    assert logger.name == "my-process"
