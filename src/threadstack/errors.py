class StoreError(Exception):
    """Base class for domain errors raised by exchange storage adapters."""


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class AuthenticationError(StoreError):
    pass


class PermissionDeniedError(StoreError):
    pass
