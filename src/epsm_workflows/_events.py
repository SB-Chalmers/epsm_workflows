"""Emit EPSM event fields through the application's existing Python handlers."""

import logging


def event(
    logger,
    name,
    message,
    *,
    context=None,
    details=None,
    level=logging.INFO,
    exc_info=False,
):
    logger.log(
        level,
        message,
        extra={
            "epsm_event": name,
            "epsm_context": context or {},
            "epsm_details": details or {},
        },
        exc_info=exc_info,
    )
