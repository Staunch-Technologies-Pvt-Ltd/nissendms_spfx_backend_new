"""Domain exceptions mapped to HTTP status codes by the API layer."""


class BadRequest(Exception):
    status = 400


class Conflict(Exception):
    status = 409


class NotFound(Exception):
    status = 404


class InternalServerError(Exception):
    status = 500


class DuplicateFile(Conflict):
    pass

