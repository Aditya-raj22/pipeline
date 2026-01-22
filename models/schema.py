"""User-defined schema models."""
import json
from typing import Literal
from pathlib import Path
from pydantic import BaseModel, Field


class UserSchemaField(BaseModel):
    """Single field in user schema."""
    name: str
    type: Literal["text", "phase", "date", "url", "list"] = "text"
    required: bool = False
    aliases: list[str] = Field(default_factory=list)
    default: str = "Undisclosed"


class UserSchema(BaseModel):
    """User-defined schema for output."""
    fields: list[UserSchemaField]

    @classmethod
    def from_json(cls, path: str | Path) -> "UserSchema":
        """Load schema from JSON file."""
        with open(path) as f:
            return cls.model_validate(json.load(f))

    @classmethod
    def default(cls) -> "UserSchema":
        """Return interim default schema."""
        return cls(fields=[
            UserSchemaField(
                name="Therapeutic Area",
                aliases=["area", "therapy area", "disease area"]
            ),
            UserSchemaField(
                name="Modality",
                aliases=["platform", "technology", "drug type"]
            ),
            UserSchemaField(
                name="Phase",
                type="phase",
                aliases=["stage", "development phase", "clinical stage"]
            ),
            UserSchemaField(
                name="Asset Name",
                required=True,
                aliases=["drug", "compound", "candidate", "program"]
            ),
            UserSchemaField(
                name="Description",
                aliases=["summary", "mechanism", "moa"]
            ),
            UserSchemaField(
                name="Therapeutic Target",
                aliases=["target", "molecular target"]
            ),
            UserSchemaField(
                name="Indication",
                aliases=["disease", "condition"]
            ),
            UserSchemaField(
                name="Company",
                aliases=["sponsor", "developer"]
            ),
        ])

    def column_order(self) -> list[str]:
        """Return column names in order."""
        return [f.name for f in self.fields]
