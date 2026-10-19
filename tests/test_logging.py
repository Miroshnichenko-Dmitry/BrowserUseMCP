import logging

from browser_mcp.logging_setup import JsonFormatter, TextFormatter


def make_record() -> logging.LogRecord:
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "Started server", (), None)
    record.color_message = "\x1b[36mStarted server\x1b[0m"
    record.task_id = "abc"
    return record


def test_text_formatter_drops_color_message_keeps_extras():
    line = TextFormatter().format(make_record())
    assert "color_message" not in line
    assert "\x1b" not in line
    assert "task_id=abc" in line


def test_json_formatter_drops_color_message():
    line = JsonFormatter().format(make_record())
    assert "color_message" not in line
    assert '"task_id": "abc"' in line
