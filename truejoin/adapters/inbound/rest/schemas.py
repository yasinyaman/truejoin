"""Request bodies for the REST API."""

from pydantic import BaseModel, Field, field_validator


class SQLRequest(BaseModel):
    sql: str


class SearchRequest(BaseModel):
    table: str
    filter_column: str
    filter_value: str
    limit: int = 20


class AggregateRequest(BaseModel):
    table: str
    group_by: str
    agg_column: str
    agg_func: str


class CreateViewRequest(BaseModel):
    name: str
    sql: str
    refresh: str = "manual"


class LoadTableRequest(BaseModel):
    """Optional body of ``POST /tables/{table}/load``.

    Without it the whole table is loaded (and refused when it is too big);
    with it, only the matching rows and columns are fetched.
    """

    where: str | None = Field(
        default=None,
        description=(
            "Filter pushed to the source: an AND of simple conditions "
            "comparing a column to a literal."
        ),
    )
    columns: list[str] | None = Field(
        default=None, description="Columns to fetch instead of all of them"
    )


class AssistantTurn(BaseModel):
    """An earlier question of a conversation and the answer it got."""

    question: str = Field(max_length=4000)
    answer: str = Field(max_length=8000)


class AssistantRequest(BaseModel):
    """Body of ``POST /assistant``: a question, and what was said before it.

    The server keeps no conversation: ``history`` is the caller's own record of
    it, sent again with each question, and only its newest part is used.
    ``conversation`` is the caller's name for it, kept in the audit trail so
    that the questions of one conversation can be read together.
    """

    question: str = Field(min_length=1, max_length=4000)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=50)
    conversation: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,64}$")

    @field_validator("question")
    @classmethod
    def _says_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the question is empty")
        return value.strip()
