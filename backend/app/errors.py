from typing import Any, Dict, Optional


class JobHunterError(RuntimeError):
    """A classified, safe application error with a machine-readable stage."""

    def __init__(
        self,
        error_type: str,
        message: str,
        status_code: int = 500,
        stage: str = "jobhunter_backend",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.safe_message = message
        self.status_code = status_code
        self.stage = stage
        self.details = details or {}

    def safe_payload(self) -> Dict[str, Any]:
        return {
            "type": self.error_type,
            "message": self.safe_message,
            "stage": self.stage,
            **self.details,
        }
