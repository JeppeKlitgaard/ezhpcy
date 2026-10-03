class CliUsageError(Exception):
    """A usage error found in a command body.

    `main()` renders it like Cyclopts' own parse errors and exits with code 2.
    `message` is Rich markup.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
