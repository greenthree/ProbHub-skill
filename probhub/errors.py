class ProbHubError(Exception):
    """User-facing ProbHub error."""

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
