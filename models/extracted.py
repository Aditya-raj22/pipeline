"""Models for extracted pipeline assets."""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class LLMAsset(BaseModel):
    """Asset schema for LLM extraction (no metadata fields)."""
    model_config = ConfigDict(extra="forbid")

    therapeutic_area: str = Field("", description="e.g., Oncology, Neurology")
    modality: str = Field("", description="e.g., Bispecific Antibody, GalNAc-asiRNA (subcutaneous)")
    phase: str = Field("", description="e.g., Phase 1, IND enabling study, Discovery")
    asset_name: str = Field(..., description="Drug/compound code or name")
    description: str = Field("", description="Mechanism or summary")
    therapeutic_target: str = Field("", description="e.g., VEGF/DLL4, PD-L1/4-1BB")
    indication: str = Field("", description="Disease/condition")


class PipelineResponse(BaseModel):
    """Response wrapper for LLM extraction."""
    model_config = ConfigDict(extra="forbid")

    assets: list[LLMAsset]


class ExtractedAsset(BaseModel):
    """Full asset with metadata (for internal use)."""
    therapeutic_area: str = ""
    modality: str = ""
    phase: str = ""
    asset_name: str = ""
    description: str = ""
    therapeutic_target: str = ""
    indication: str = ""
    company: str = ""

    # Metadata
    source_url: str = ""
    extraction_method: Literal["text", "vision", "hybrid"] = "text"

    @classmethod
    def from_llm(cls, llm_asset: LLMAsset, **metadata) -> "ExtractedAsset":
        """Create from LLM response with metadata."""
        return cls(
            therapeutic_area=llm_asset.therapeutic_area,
            modality=llm_asset.modality,
            phase=llm_asset.phase,
            asset_name=llm_asset.asset_name,
            description=llm_asset.description,
            therapeutic_target=llm_asset.therapeutic_target,
            indication=llm_asset.indication,
            **metadata,
        )
