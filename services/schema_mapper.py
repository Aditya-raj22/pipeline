"""Schema mapping service - maps extracted assets to user-defined schema."""
from models.extracted import ExtractedAsset
from models.schema import UserSchema


# Field name mapping from ExtractedAsset to common aliases
FIELD_ALIASES = {
    "therapeutic_area": ["area", "therapy area", "disease area", "therapeutic area"],
    "modality": ["platform", "technology", "drug type", "modality"],
    "phase": ["stage", "development phase", "clinical stage", "phase"],
    "asset_name": ["drug", "compound", "candidate", "program", "asset name", "asset"],
    "description": ["summary", "mechanism", "moa", "description"],
    "therapeutic_target": ["target", "molecular target", "therapeutic target"],
    "indication": ["disease", "condition", "indication"],
    "company": ["sponsor", "developer", "company"],
}


def _normalize(s: str) -> str:
    """Normalize string for matching."""
    return s.lower().replace("_", " ").replace("-", " ").strip()


def _find_field_match(schema_field_name: str, aliases: list[str]) -> str | None:
    """
    Find matching ExtractedAsset field for a schema field.

    Returns the ExtractedAsset attribute name or None.
    """
    normalized_name = _normalize(schema_field_name)
    normalized_aliases = [_normalize(a) for a in aliases]

    # Check each ExtractedAsset field
    for asset_field, asset_aliases in FIELD_ALIASES.items():
        asset_aliases_norm = [_normalize(a) for a in asset_aliases]

        # Check if schema field matches asset field or its aliases
        if normalized_name in asset_aliases_norm:
            return asset_field

        # Check if any schema alias matches
        for alias in normalized_aliases:
            if alias in asset_aliases_norm:
                return asset_field

    return None


def map_asset_to_schema(asset: ExtractedAsset, schema: UserSchema) -> dict:
    """
    Map a single ExtractedAsset to user schema.

    Returns dict with schema field names as keys.
    """
    result = {}
    asset_dict = asset.model_dump()

    for field in schema.fields:
        # Find matching asset field
        asset_field = _find_field_match(field.name, field.aliases)

        if asset_field and asset_field in asset_dict:
            value = asset_dict[asset_field]
            # Handle empty/None values
            if value is None or value == "" or value == "Undisclosed":
                result[field.name] = field.default
            else:
                result[field.name] = value
        else:
            result[field.name] = field.default

    return result


def map_assets_to_schema(
    assets: list[ExtractedAsset],
    schema: UserSchema = None,
) -> list[dict]:
    """
    Map list of ExtractedAssets to user schema.

    Args:
        assets: List of extracted assets
        schema: User schema (defaults to standard schema)

    Returns:
        List of dicts with schema field names
    """
    if schema is None:
        schema = UserSchema.default()

    return [map_asset_to_schema(asset, schema) for asset in assets]


def normalize_phase(phase: str) -> str:
    """
    Normalize phase values to standard format.

    Handles variations like:
    - "Clinical Development (Phase 1)" -> "Phase 1"
    - "Phase I" -> "Phase 1"
    - "P1" -> "Phase 1"
    """
    if not phase:
        return "Undisclosed"

    phase_lower = phase.lower().strip()

    # Handle "Undisclosed" variations
    if phase_lower in ["", "undisclosed", "unknown", "n/a", "na", "tbd"]:
        return "Undisclosed"

    # Map roman numerals
    roman_map = {
        "i": "1", "ii": "2", "iii": "3", "iv": "4",
        "1/2": "1/2", "2/3": "2/3",
    }

    # Extract phase number/stage
    import re

    # Pattern: "Phase X" or "Phase X/Y"
    match = re.search(r'phase\s*([1-3iv]+/?[1-3iv]*)', phase_lower)
    if match:
        num = match.group(1)
        for roman, arabic in roman_map.items():
            num = num.replace(roman, arabic)
        return f"Phase {num.upper()}"

    # Handle specific patterns
    patterns = {
        r'preclinical|pre-clinical|pre clinical': 'Preclinical',
        r'discovery': 'Discovery',
        r'ind.?enabling|ind enabling': 'IND enabling study',
        r'filed|nda|bla': 'Filed',
        r'approved|marketed': 'Approved',
        r'phase\s*1.*completed|p1.*completed': 'Phase 1 completed',
        r'platform': 'Platform',
    }

    for pattern, normalized in patterns.items():
        if re.search(pattern, phase_lower):
            return normalized

    # Return original if no match (preserve non-standard phases)
    return phase


def apply_normalizations(mapped: list[dict]) -> list[dict]:
    """Apply field normalizations to mapped data."""
    for row in mapped:
        # Normalize phase if present
        if "Phase" in row:
            row["Phase"] = normalize_phase(row["Phase"])

    return mapped


def map_and_normalize(
    assets: list[ExtractedAsset],
    schema: UserSchema = None,
) -> list[dict]:
    """
    Map assets to schema and apply normalizations.

    This is the main entry point for schema mapping.
    """
    mapped = map_assets_to_schema(assets, schema)
    normalized = apply_normalizations(mapped)
    return normalized
