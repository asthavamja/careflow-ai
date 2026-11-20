from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

RequestType = Literal[
    "new_registration", "book_appointment", "reschedule",
    "cancel", "documents", "unclear",
]


class ExtractedDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: Optional[str]
    last_name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    date_of_birth: Optional[str]      # exactly as written; Validation parses it later
    named_department: Optional[str]   # only a specialty the patient named
    preferred_date_time: Optional[str]
    appointment_code: Optional[str]


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_type: RequestType
    details: ExtractedDetails
    urgent_flag: bool
    urgent_reason: Optional[str]
    confidence: float = Field(ge=0, le=1)
    missing_fields: list[str]

    @model_validator(mode="after")
    def urgent_needs_reason(self):
        if self.urgent_flag and not self.urgent_reason:
            raise ValueError("urgent_flag is true but urgent_reason is empty")
        return self