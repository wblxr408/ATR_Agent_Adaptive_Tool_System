class ATRError(Exception):
    """A stable, safe client-visible failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

