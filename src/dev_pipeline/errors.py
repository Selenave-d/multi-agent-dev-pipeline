class PipelineError(Exception):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, message: str, *, code: str = "pipeline_error") -> None:
        super().__init__(message)
        self.code = code


class ValidationError(PipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="validation_error")


class StageExecutionError(PipelineError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Stage '{stage}' failed: {message}", code="stage_execution_error")
        self.stage = stage
