import sys
import time
import uuid
from collections.abc import Mapping

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from audit_log import AuditConfigError, load_audit_config
from config import load_model_config, load_openai_api_key, secrets_section
from enhanced_assistant import EnhancedQueryAssistant, is_truncated
from sql_guard import load_guard_config

OUTCOME_ONLY_CAPTION = (
    "Audit log: outcomes only. "
    "Set ADQ_AUDIT_HMAC_KEY to also log hashed questions and masked SQL."
)
MISSING_KEY_MESSAGE = (
    "Set OPENAI_API_KEY or [openai] api_key in .streamlit/secrets.toml"
)


def load_secrets() -> Mapping:
    """st.secrets as a plain mapping, or {} when no secrets file exists."""
    try:
        return st.secrets.to_dict()
    except FileNotFoundError:  # includes StreamlitSecretNotFoundError
        return {}


def truncation_notice(df: pd.DataFrame) -> str | None:
    """Notice shown when the result was cut at the row cap, else None.

    N is the configured row cap (df.attrs["row_cap"], set from GuardConfig),
    not the number of rows in df.
    """
    if not is_truncated(df):
        return None
    cap = df.attrs["row_cap"]
    return (
        f"Results truncated at the row cap: showing the first {cap} rows only. "
        "Totals and charts computed from these rows may be incomplete."
    )


def create_visualization(df: pd.DataFrame) -> go.Figure | None:
    """Create a visualization based on the data."""
    if len(df) == 0:
        return None
    numeric_cols = df.select_dtypes(include=["float64", "int64"]).columns
    other_cols = df.select_dtypes(exclude=["float64", "int64"]).columns
    if len(numeric_cols) > 0 and len(other_cols) > 0:
        fig = px.bar(
            df,
            x=other_cols[0],
            y=numeric_cols[0],
            title=f"{numeric_cols[0]} by {other_cols[0]}",
        )
        return fig
    elif len(numeric_cols) >= 2:
        fig = px.scatter(
            df,
            x=numeric_cols[0],
            y=numeric_cols[1],
            title=f"{numeric_cols[1]} vs {numeric_cols[0]}",
        )
        return fig
    return None


def _use_sample(sample: str) -> None:
    """Button callback: put the sample question in the query box."""
    st.session_state.query = sample


def startup_error_text(exc: BaseException) -> str:
    """Startup failure line: the exception's class name only.

    The message is never shown or printed, since it can hold paths, SQL,
    keys or cell values.
    """
    return f"Startup failed: {type(exc).__name__}"


def _init_assistant() -> None:
    """Load config, open the DB and build the assistant into session state."""
    secrets = load_secrets()
    openai_key = load_openai_api_key(secrets_section(secrets, "openai"))
    if not openai_key:
        st.error(MISSING_KEY_MESSAGE)
        st.stop()
    try:
        audit_config = load_audit_config(
            "chinook.db", secrets_section(secrets, "audit")
        )
    except AuditConfigError as e:
        st.error(str(e))
        st.stop()
    st.session_state.assistant = EnhancedQueryAssistant(
        "chinook.db",
        openai_key,
        guard_config=load_guard_config(secrets_section(secrets, "sql_guard")),
        model_config=load_model_config(secrets_section(secrets, "model")),
        audit_config=audit_config,
    )


def main():
    st.title("🚀 AI-Powered Database Query Assistant")
    st.caption("Natural Language → SQL → Insights")

    # Initialize assistant. Any other startup error shows and prints only its
    # class name. st.stop(), KeyboardInterrupt and SystemExit are
    # BaseException, not Exception, so they pass through.
    if "assistant" not in st.session_state:
        try:
            _init_assistant()
        except Exception as e:
            text = startup_error_text(e)
            st.error(text)
            print(text, file=sys.stderr)
            st.stop()
    if st.session_state.assistant.audit.config.mode == "outcome_only":
        st.caption(OUTCOME_ONLY_CAPTION)

    # Audit session label: a random uuid4 per browser session, not derived
    # from the user.
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())

    # Query input
    query = st.text_input(
        "Enter your question about the music store data:",
        placeholder="e.g., Show me the top 5 customers by total purchase amount",
        key="query",
    )

    # Sample queries
    with st.expander("📋 Sample Queries (click to run)"):
        cols = st.columns(2)
        for i, sample in enumerate(st.session_state.assistant.SAMPLE_QUERIES):
            cols[i % 2].button(
                sample, key=f"sample_{i}", on_click=_use_sample, args=(sample,)
            )

    if query:
        try:
            with st.spinner("🔍 Generating SQL and running query..."):
                start = time.time()
                results, similar = st.session_state.assistant.execute_query(
                    query, session_id=st.session_state.session_id
                )
                latency = round((time.time() - start) * 1000, 2)

            st.success(f"✅ Query completed in {latency} ms")
            st.subheader("📊 Query Results")
            notice = truncation_notice(results)
            if notice:
                st.warning(notice, icon="⚠️")
            st.dataframe(results, use_container_width=True)

            fig = create_visualization(results)
            if fig:
                st.plotly_chart(fig, use_container_width=True)

            if similar:
                st.subheader("🔍 Similar Previous Queries")
                for sq in similar:
                    st.write(
                        f"• {sq['natural_query']} "
                        f"(latency: {sq.get('latency_ms', 'N/A')} ms)"
                    )
        except AuditConfigError as e:
            # The DB file changed and is no longer chinook.db: refused exactly
            # like a non-chinook DB at startup.
            st.error(str(e))
            st.stop()
        except Exception as e:
            st.error(f"❌ Error: {str(e)}")

    with st.expander("ℹ️ About the Database"):
        st.write(
            "Chinook sample database (digital media store). "
            "Ask anything about sales, artists, customers, etc."
        )


if __name__ == "__main__":
    import time  # for latency

    main()
