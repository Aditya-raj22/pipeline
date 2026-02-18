"""Export service - generates Excel output."""
import re
import pandas as pd
from datetime import datetime
from pathlib import Path
from openpyxl.utils import get_column_letter
from models.schema import UserSchema


# Regex for illegal Excel characters
ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def sanitize_for_excel(value):
    """Remove illegal characters that Excel can't handle."""
    if isinstance(value, str):
        # Remove control characters
        value = ILLEGAL_CHARS_RE.sub('', value)
        # Replace common problematic Unicode chars
        value = value.replace('\u03b1', 'alpha')  # α
        value = value.replace('\u03b2', 'beta')   # β
        value = value.replace('\u03b3', 'gamma')  # γ
    return value


def export_to_excel(
    assets: list[dict],
    output_path: str = "pipeline_output.xlsx",
    schema: UserSchema = None,
) -> str:
    """
    Export assets to Excel file.

    Args:
        assets: List of enriched asset dicts
        output_path: Output file path
        schema: User schema for column ordering

    Returns:
        Path to created file
    """
    if schema is None:
        schema = UserSchema.default()

    if not assets:
        print("No assets to export")
        return None

    # Define column order: schema fields + sources
    base_columns = schema.column_order()
    all_columns = base_columns + ["Sources"]

    # Create DataFrame
    df = pd.DataFrame(assets)

    # Ensure all columns exist
    for col in all_columns:
        if col not in df.columns:
            df[col] = "Undisclosed"

    # Reorder columns (only include columns that exist)
    existing_cols = [c for c in all_columns if c in df.columns]
    df = df[existing_cols]

    # Fill NaN with "Undisclosed"
    df = df.fillna("Undisclosed")

    # Replace empty strings with "Undisclosed"
    df = df.replace("", "Undisclosed")

    # Sanitize for Excel (remove illegal characters)
    df = df.apply(lambda col: col.map(sanitize_for_excel) if col.dtype == object else col)

    # Export to Excel with formatting
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Pipeline')

        # Auto-adjust column widths
        worksheet = writer.sheets['Pipeline']
        for idx, col in enumerate(df.columns):
            max_length = max(
                df[col].astype(str).map(len).max(),
                len(col)
            )
            # Cap at 50 chars
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[get_column_letter(idx + 1)].width = adjusted_width

    print(f"Exported {len(df)} assets to {output_path}")
    return str(output_path)


def export_summary(
    results: dict,
    output_path: str = "pipeline_summary.txt",
) -> str:
    """
    Export a text summary of the pipeline run.

    Args:
        results: Dict with company -> assets mapping
        output_path: Output file path

    Returns:
        Path to created file
    """
    lines = [
        "=" * 60,
        "PIPELINE SOURCING SUMMARY",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
    ]

    total_assets = 0
    for company, assets in results.items():
        lines.append(f"\n{company}")
        lines.append("-" * len(company))
        lines.append(f"Assets found: {len(assets)}")
        total_assets += len(assets)

        if assets:
            # Group by phase
            phases = {}
            for asset in assets:
                phase = asset.get("Phase", "Unknown")
                phases[phase] = phases.get(phase, 0) + 1

            lines.append("By phase:")
            for phase, count in sorted(phases.items()):
                lines.append(f"  {phase}: {count}")

    lines.append(f"\n{'=' * 60}")
    lines.append(f"TOTAL ASSETS: {total_assets}")
    lines.append("=" * 60)

    content = "\n".join(lines)

    with open(output_path, "w") as f:
        f.write(content)

    print(f"Summary saved to {output_path}")
    return output_path
