import logging

import opus_auth


def configure() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx narrates every request with its whole address, and the engines this
    # talks to take their API keys in the query string
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    opus_auth.quiet_access_log()
