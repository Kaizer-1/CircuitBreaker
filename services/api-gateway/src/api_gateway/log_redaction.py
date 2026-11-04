"""Keep the WebSocket token out of the logs (api/README.md: "redacts it from access logs").

uvicorn logs every WebSocket handshake with its full path, query string included (on
`uvicorn.error`, even with `--no-access-log`), and that query carries `?access_token=<JWT>`.
This filter sits on the root handler, so it rewrites every record from every logger before it
is written.
"""

import logging
import re

_TOKEN = re.compile(r"(access_token=)[^&\s\"']+")


def redact(text: str) -> str:
    return _TOKEN.sub(r"\1[redacted]", text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "access_token=" in message:
            record.msg, record.args = redact(message), None
        return True


def install() -> None:
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactTokens) for f in handler.filters):
            handler.addFilter(RedactTokens())
