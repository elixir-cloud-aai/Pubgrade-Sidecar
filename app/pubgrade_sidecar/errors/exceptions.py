from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    UnsupportedMediaTypeProblem,
)
from werkzeug.exceptions import (
    BadRequest, Conflict, Forbidden, MethodNotAllowed, NotFound, Unauthorized,
    InternalServerError,
)


class DockerImageUnavailable(NotFound):
    """Raised when object with given image is not found."""
    pass


class ImageRepositoryMismatch(Conflict):
    """Raised when a notification is for another image repository."""
    pass


class ImageRegistryUnsupported(Conflict):
    """Raised when a notification is for an image that isn't on Docker Hub."""
    pass


exceptions = {
    Exception: {
        "msg": "An unexpected error occurred.",
        "status_code": "500",
    },
    BadRequest: {
        "msg": "The request is invalid.",
        "status_code": "400",
    },
    BadRequestProblem: {
        "msg": "The request is invalid.",
        "status_code": "400",
    },
    ExtraParameterProblem: {
        "msg": "The request is invalid.",
        "status_code": "400",
    },
    UnsupportedMediaTypeProblem: {
        "msg": "The media type is not supported.",
        "status_code": "415",
    },
    MethodNotAllowed: {
        "msg": "The method is not allowed.",
        "status_code": "405",
    },
    Forbidden: {
        "msg": "The sidecar may not access this deployment.",
        "status_code": "403",
    },
    ImageRegistryUnsupported: {
        "msg": "Only images on Docker Hub can be rolled out.",
        "status_code": "409",
    },
    ImageRepositoryMismatch: {
        "msg": "The deployment uses another image repository.",
        "status_code": "409",
    },
    Unauthorized: {
        "msg": "The request is unauthorized.",
        "status_code": "401",
    },
    InternalServerError: {
        "msg": "An unexpected error occurred",
        "status_code": "500",
    },

    NotFound: {
        "msg": "The requested resource wasn't found.",
        "status_code": "404",
    },
    DockerImageUnavailable: {
        "msg": "The requested Image is not found.",
        "status_code": "409",
    },
}