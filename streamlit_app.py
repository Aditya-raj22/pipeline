"""Streamlit UI for batch pipeline sourcing."""
import asyncio
import subprocess
import time
import io
import os
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Pipeline Sourcer", layout="wide")

# --- Install Playwright browser on first run (needed for cloud) ---
@st.cache_resource
def install_playwright():
    subprocess.run(["playwright", "install", "chromium"], capture_output=True)

install_playwright()

# --- Session state defaults ---
for key, default in {
    "results": {},
    "running": False,
    "stop_flag": False,
    "current": "",
    "log": [],
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# --- Sidebar ---
with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("OpenAI API Key", type="password",
                            value=st.session_state.get("api_key", ""),
                            help="Required. Stored in your browser session only.")
    if api_key:
        st.session_state["api_key"] = api_key

    st.divider()
    uploaded = st.file_uploader("Upload Excel", type=["xlsx", "xls", "csv"])
    enrich = st.checkbox("Enrich from drug pages", value=False,
                         help="Search web for each asset to fill missing data (~2x slower)")

st.title("Pipeline Sourcer")

# --- Validate API key ---
if not st.session_state.get("api_key"):
    st.info("Enter your OpenAI API key in the sidebar to get started.")
    st.stop()

# --- Inject API key into pipeline modules ---
def _set_api_key(key: str):
    os.environ["OPENAI_API_KEY"] = key
    from openai import AsyncOpenAI
    import services.extraction as ext_mod
    import services.drug_pages as dp_mod
    ext_mod.client = AsyncOpenAI(api_key=key)
    dp_mod.client = AsyncOpenAI(api_key=key)

_set_api_key(st.session_state["api_key"])

from main import process_company
from models.schema import UserSchema
from utils.fetch import close_browser

# --- Parse upload ---
df_input = None
if uploaded:
    if uploaded.name.endswith(".csv"):
        df_input = pd.read_csv(uploaded)
    else:
        df_input = pd.read_excel(uploaded)

    col_map = {c: c.strip().title() for c in df_input.columns}
    df_input = df_input.rename(columns=col_map)

    if "Company" not in df_input.columns:
        st.error("Excel must have a 'Company' column.")
        df_input = None
    else:
        df_input = df_input.dropna(subset=["Company"])
        st.sidebar.success(f"{len(df_input)} companies loaded")
        with st.expander("Preview uploaded data", expanded=False):
            st.dataframe(df_input.head(10), use_container_width=True)

# --- Controls ---
col1, col2 = st.columns([1, 1])
with col1:
    run_btn = st.button("Run", disabled=st.session_state.running or df_input is None,
                         type="primary", use_container_width=True)
with col2:
    stop_btn = st.button("Stop", disabled=not st.session_state.running,
                          use_container_width=True)

if stop_btn:
    st.session_state.stop_flag = True

# --- Processing ---
if run_btn and df_input is not None:
    st.session_state.running = True
    st.session_state.stop_flag = False
    st.session_state.results = {}
    st.session_state.log = []

    schema = UserSchema.default()
    total = len(df_input)
    has_url = "Url" in df_input.columns

    progress_bar = st.progress(0)
    status_text = st.empty()
    log_area = st.empty()
    summary_table = st.empty()

    start_time = time.time()

    async def run_all():
        for i, row in enumerate(df_input.itertuples()):
            if st.session_state.stop_flag:
                st.session_state.log.append("Stopped by user.")
                break

            company = str(row.Company).strip()
            url = str(row.Url).strip() if has_url and pd.notna(getattr(row, "Url", None)) else None

            pct = i / total
            elapsed = time.time() - start_time
            per_company = elapsed / max(i, 1)
            remaining = per_company * (total - i)
            mins, secs = divmod(int(remaining), 60)
            hrs, mins = divmod(mins, 60)
            eta = f"{hrs}h {mins:02d}m" if hrs else f"{mins}m {secs:02d}s"

            progress_bar.progress(pct, text=f"{i}/{total} ({pct:.0%}) — ETA: {eta}")
            status_text.text(f"Processing: {company}...")
            st.session_state.current = company

            try:
                assets = await process_company(company, schema, drug_pages=enrich, url=url)
            except Exception as e:
                assets = []
                st.session_state.log.append(f"[{company}] Error: {e}")

            st.session_state.results[company] = assets
            n_clinical = sum(1 for a in assets if a.get("Phase", "").startswith("Phase") or a.get("Phase", "") in ("Filed", "Approved"))
            st.session_state.log.append(f"[{company}] {len(assets)} assets ({n_clinical} clinical)")

            rows = []
            for c, a_list in st.session_state.results.items():
                nc = sum(1 for a in a_list if a.get("Phase", "").startswith("Phase") or a.get("Phase", "") in ("Filed", "Approved"))
                rows.append({"Company": c, "Total": len(a_list), "Clinical": nc, "Status": "Done"})
            summary_table.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            log_area.text_area("Log", "\n".join(st.session_state.log[-20:]), height=150)

        await close_browser()
        progress_bar.progress(1.0, text=f"{total}/{total} (100%)")
        status_text.text("Complete!" if not st.session_state.stop_flag else "Stopped.")

    asyncio.run(run_all())
    st.session_state.running = False
    st.session_state.current = ""
    st.rerun()

# --- Results display ---
if st.session_state.results:
    st.divider()
    st.subheader("Results")

    rows = []
    for c, a_list in st.session_state.results.items():
        nc = sum(1 for a in a_list if a.get("Phase", "").startswith("Phase") or a.get("Phase", "") in ("Filed", "Approved"))
        rows.append({"Company": c, "Total": len(a_list), "Clinical": nc})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    companies = list(st.session_state.results.keys())
    selected = st.selectbox("Company detail preview", companies)
    if selected and st.session_state.results[selected]:
        st.dataframe(pd.DataFrame(st.session_state.results[selected]), use_container_width=True, hide_index=True)
    elif selected:
        st.info("No assets found for this company.")

    st.divider()
    all_assets = [a for assets in st.session_state.results.values() for a in assets]

    if all_assets:
        col_dl1, col_dl2 = st.columns(2)

        with col_dl1:
            buf = io.BytesIO()
            schema = UserSchema.default()
            base_columns = schema.column_order() + ["Sources"]
            df_out = pd.DataFrame(all_assets)
            for col in base_columns:
                if col not in df_out.columns:
                    df_out[col] = "Undisclosed"
            df_out = df_out[[c for c in base_columns if c in df_out.columns]]
            df_out = df_out.fillna("Undisclosed").replace("", "Undisclosed")
            df_out.to_excel(buf, index=False, engine="openpyxl")
            st.download_button("Download Excel", buf.getvalue(),
                               file_name="pipeline_output.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               use_container_width=True)

        with col_dl2:
            summary_lines = [f"Pipeline Sourcing Summary\n{'='*40}\n"]
            for c, a_list in st.session_state.results.items():
                summary_lines.append(f"\n{c}: {len(a_list)} assets")
                if a_list:
                    phases = {}
                    for a in a_list:
                        p = a.get("Phase", "Unknown")
                        phases[p] = phases.get(p, 0) + 1
                    for p, cnt in sorted(phases.items()):
                        summary_lines.append(f"  {p}: {cnt}")
            summary_lines.append(f"\nTotal: {len(all_assets)} assets")
            st.download_button("Download Summary", "\n".join(summary_lines),
                               file_name="pipeline_summary.txt",
                               mime="text/plain",
                               use_container_width=True)

    if st.session_state.log:
        with st.expander("Processing log"):
            st.text("\n".join(st.session_state.log))
