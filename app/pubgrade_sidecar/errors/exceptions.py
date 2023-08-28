from werkzeug.exceptions import (
    NotFound, Unauthorized, InternalServerError,
)


class DockerImageUnavailable(NotFound):
    """Raised when object with given image is not found."""
    pass


exceptions = {
    Exception: {
        "msg": "An unexpected error occurred.",
        "status_code": "500",
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