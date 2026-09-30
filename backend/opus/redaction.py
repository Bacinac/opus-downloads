"""What an error may carry out of this process. The libraries an engine fails
inside name the whole address they asked, and the engines take their keys in the
query string, the form body or a header echoed back — so every error text that
reaches a page, a run or a consuming module passes through here first."""

import re

MASK = "***"

_URL = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)(?:[^\s/@'\"<>]+@)?([^\s?#'\"<>]*)(\?[^\s#'\"<>]*)?"
)
_WORDS = (
    r"password|passwd|pass|pwd|api[_-]?key|apikey|x-api-key|token|access_token|"
    r"refresh_token|user_auth_token|authorization|secret|app_secret|arl|sig|"
    r"request_sig|signature|cookie|sid|key"
)
_PAIR = re.compile(rf"(?i)(\b(?:{_WORDS})\b[\"']?\s*[=:]\s*[\"']?)([^\s&\"',;}}\]]+)")
_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def redacted(text: str) -> str:
    text = _URL.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{'?' + MASK if m.group(3) else ''}", text)
    text = _SCHEME.sub(lambda m: f"{m.group(1)} {MASK}", text)
    return _PAIR.sub(lambda m: f"{m.group(1)}{MASK}", text)
