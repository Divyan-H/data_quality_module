"""Exceptions shared by the readers and the job runner."""


class FileUnreadable(Exception):
    """A file (or a conversion of it) cannot be read: a `file_unreadable` block finding for that file only."""


class JobFailed(Exception):
    """The job cannot produce a trustworthy output at all (spec 11.4). `code` is the `error.code`."""

    def __init__(self, code: str, message: str, retryable: bool = False, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}

    def __reduce__(self):              # picklable, so a failure in a worker process reaches the job
        return JobFailed, (self.code, self.message, self.retryable, self.details)


class Cancelled(Exception):
    """The platform cancelled the job; partial output stays orphaned under its prefix (spec 11.6)."""
