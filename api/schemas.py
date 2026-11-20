from typing import Literal,Optional

from pydantic import BaseModel, ConfigDict, Field


class RequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    channel: Literal["web_form", "email", "api"]
    raw_text: str = Field(min_length=1, max_length=2000)


class RequestOut(BaseModel):
    request_id: int
    status: str

class ReplyIn(BaseModel):
    choice: int = Field(ge=1, le=9)

class DecideIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reviewer_id: int
    decision: Literal["same_person", "different_person", "choose_department",
                      "approve_without_referral", "request_referral",
                      "more_info_needed", "reject"]
    patient_id: Optional[int] = None
    department_id: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=500)