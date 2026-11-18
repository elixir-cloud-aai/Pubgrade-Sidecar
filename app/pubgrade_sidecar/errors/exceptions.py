"""Errors of the API and the responses that FOCA returns for them."""

# `UnsupportedMediaTypeProblem` has no counterpart in `foca.errors` yet
from connexion.exceptions import UnsupportedMediaTypeProblem
from foca.errors import (
    BadRequest,
    Conflict,
    ExtraParameterProblem,
    Forbidden,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    RequestValidationProblem,
    Unauthorized,
)


class ImageUnavailable(NotFound):
    """Raised when the image of a notification isn't in its registry.

    Only raised in the background, while a delivery waits for the image, so
    never returned to clients.
    """

    pass


class ImageRepositoryMismatch(Conflict):
    """Raised when a notification is for another image repository."""

    pass


class DowngradeRefused(Conflict):
    """Raised when a notification isn't an upgrade.

    Only with `REFUSE_DOWNGRADES`: its tag isn't a higher version than the
    current tag of each container it would change, or than the tag of a
    delivery that still waits, or one of these tags isn't a version.
    """

    pass


# Explanation of a refused downgrade, in the response and in the status
# report of a delivery that the check before its patch refuses
DOWNGRADE_DETAIL = "The tag isn't an upgrade of the deployment's; nothing changed."


class TagNotAllowed(Conflict):
    """Raised when the tag of a notification doesn't match `ALLOWED_TAGS`."""

    pass


# Problem details (RFC 9457) of the exceptions, by class (`exceptions.mapping`
# in the configuration); exceptions that aren't listed get those of their
# closest listed base class. The classes of `foca.errors` also cover the
# Werkzeug classes they derive from, e.g., Flask's `NotFound` for unknown
# paths
exceptions = {
    Exception: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "An unexpected error occurred.",
    },
    BadRequest: {
        "title": "Bad Request",
        "status": 400,
        "detail": "The request is invalid.",
    },
    RequestValidationProblem: {
        "title": "Bad Request",
        "status": 400,
        "detail": "The request is invalid.",
    },
    ExtraParameterProblem: {
        "title": "Bad Request",
        "status": 400,
        "detail": "The request is invalid.",
    },
    UnsupportedMediaTypeProblem: {
        "title": "Unsupported Media Type",
        "status": 415,
        "detail": "The media type is not supported.",
    },
    MethodNotAllowed: {
        "title": "Method Not Allowed",
        "status": 405,
        "detail": "The method is not allowed.",
    },
    Forbidden: {
        "title": "Forbidden",
        "status": 403,
        "detail": "The sidecar may not access this deployment.",
    },
    ImageRepositoryMismatch: {
        "title": "Conflict",
        "status": 409,
        "detail": "The deployment uses another image repository.",
    },
    DowngradeRefused: {
        "title": "Conflict",
        "status": 409,
        "detail": DOWNGRADE_DETAIL,
    },
    TagNotAllowed: {
        "title": "Conflict",
        "status": 409,
        "detail": "The sidecar's settings don't allow this tag; nothing changed.",
    },
    Unauthorized: {
        "title": "Unauthorized",
        "status": 401,
        "detail": "The request is unauthorized.",
    },
    InternalServerError: {
        "title": "Internal Server Error",
        "status": 500,
        "detail": "An unexpected error occurred.",
    },
    PayloadTooLarge: {
        "title": "Content Too Large",
        "status": 413,
        "detail": "The request body is larger than the configured limit.",
    },
    NotFound: {
        "title": "Not Found",
        "status": 404,
        "detail": "The requested resource wasn't found.",
    },
}
