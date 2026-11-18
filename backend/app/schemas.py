from pydantic import BaseModel, Field
from typing import Optional

class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=128)

class LoginRequest(BaseModel):
    email: str
    password: str

class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    conversation_id: Optional[str] = None

class FeedbackRequest(BaseModel):
    message_id: int
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=2000)
