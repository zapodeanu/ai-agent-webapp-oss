from typing import Literal, TypedDict


Role = Literal["user", "assistant"]


class ChatTurn(TypedDict):
    role: Role
    text: str
