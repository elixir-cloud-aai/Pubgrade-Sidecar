"""Errors of the API and the responses that FOCA returns for them."""

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    UnsupportedMediaTypeProblem,
)
from werkzeug.exceptions import (
    BadRequest,
    Conflict,
    Forbidden,
    InternalServerError,
    MethodNotAllowed,
    NotFound,
    Unauthorized,
)


class DockerImageUnavailable(NotFound):
    """Raised when the image of a notification isn't on Docker Hub.

    Only raised in the background, while a delivery waits for the image, so
    never returned to clients.
    """

    pass


class ImageRepositoryMismatch(Conflict):
    """Raised when a notification is for another image repository."""

    pass


class ImageRegistryUnsupported(Conflict):
    """Raised when a notification is for an image that isn't on Docker Hub."""

    pass


# Problem details (RFC 9457) of the exceptions, by class (`exceptions.mapping`
# in the configuration); exceptions that aren't listed get those of their
# closest listed base class
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
    BadRequestProblem: {
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
    ImageRegistryUnsupported: {
        "title": "Conflict",
        "status": 409,
        "detail": "Only images on Docker Hub can be rolled out.",
    },
    ImageRepositoryMismatch: {
        "title": "Conflict",
        "status": 409,
        "detail": "The deployment uses another image repository.",
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
    NotFound: {
        "title": "Not Found",
        "status": 404,
        "detail": "The requested resource wasn't found.",
    },
}
