import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    channel: Literal["web_form", "email", "api"]
    raw_text: str = Field(min_length=1, max_length=2000)
    contact_email: Optional[str] = Field(default=None, max_length=254)

    @field_validator("contact_email")
    @classmethod
    def valid_contact_email(cls, value):
        if value is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("contact_email must be a valid email address")
        return value


class MoreInfoIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)


class RequestOut(BaseModel):
    request_id: int
    status: str

class ReplyIn(BaseModel):
    choice: int = Field(ge=1, le=9)

class DecideIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reviewer_id: int
    decision: Literal["same_person", "different_person", "choose_department",
                      "approve_registration",
                      "approve_without_referral", "request_referral",
                      "more_info_needed", "reject"]
    patient_id: Optional[int] = None
    department_id: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=500)

class AckIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reviewer_id: int
    note: Optional[str] = Field(default=None, max_length=500)


class WaitlistAcceptIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    offer_code: str = Field(min_length=1, max_length=32)


class WaitlistJoinIn(BaseModel):
    preferred_from: Optional[datetime] = None
    preferred_to: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_preferred_window(self):
        for value in (self.preferred_from, self.preferred_to):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Preferred waitlist times must include a timezone")
        if (
            self.preferred_from is not None
            and self.preferred_to is not None
            and self.preferred_to <= self.preferred_from
        ):
            raise ValueError("preferred_to must be later than preferred_from")
        return self


class DocumentSubmitIn(BaseModel):
    requirement_id: int = Field(gt=0)


class DocumentReviewIn(BaseModel):
    reviewer_id: int = Field(gt=0)
    decision: Literal["accepted", "rejected"]