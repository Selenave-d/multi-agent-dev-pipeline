class PipelineError(Exception):
    """Base error carrying a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "pipeline_error",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ValidationError(PipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="validation_error", retryable=False)


class StageExecutionError(PipelineError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Stage '{stage}' failed: {message}", code="stage_execution_error")
        self.stage = stage
