import streamlit as st
import pandas as pd
import time
from enhanced_assistant import EnhancedQueryAssistant
import plotly.express as px
from typing import Optional, Union


def create_visualization(df: pd.DataFrame) -> Optional[Union[px.bar, px.scatter]]:
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


def main():
    st.title("🚀 AI-Powered Database Query Assistant")
    st.caption("MCP + Runlayer Ready | Natural Language → SQL → Insights")

    # Sidebar MCP toggle
    use_mcp = st.sidebar.checkbox("🛡️ Use MCP Layer (Runlayer mode)", value=False)

    # Initialize assistant
    if (
        "assistant" not in st.session_state
        or st.session_state.get("use_mcp") != use_mcp
    ):
        openai_key = st.secrets["openai"]["api_key"]
        st.session_state.assistant = EnhancedQueryAssistant(
            "chinook.db", openai_key, use_mcp=use_mcp
        )
        st.session_state.use_mcp = use_mcp

    # Query input
    query = st.text_input(
        "Enter your question about the music store data:",
        placeholder="e.g., Show me the top 5 customers by total purchase amount",
    )

    # Sample queries
    with st.expander("📋 Sample Queries (click to run)"):
        cols = st.columns(2)
        for i, sample in enumerate(st.session_state.assistant.SAMPLE_QUERIES):
            if cols[i % 2].button(sample, key=f"sample_{i}"):
                query = sample
                st.rerun()

    if query:
        try:
            with st.spinner("🔍 Processing with LLM + Vector RAG..."):
                start = time.time()
                results, similar = st.session_state.assistant.execute_query(query)
                latency = round((time.time() - start) * 1000, 2)

            st.success(f"✅ Query completed in {latency} ms")
            st.subheader("📊 Query Results")
            st.dataframe(results, use_container_width=True)

            fig = create_visualization(results)
            if fig:
                st.plotly_chart(fig, use_container_width=True)

            if similar:
                st.subheader("🔍 Similar Previous Queries")
                for sq in similar:
                    st.write(
                        f"• {sq['natural_query']} (latency: {sq.get('latency_ms', 'N/A')} ms)"
                    )
        except Exception as e:
            st.error(f"❌ Error: {str(e)}")

    with st.expander("ℹ️ About the Database"):
        st.write(
            "Chinook sample database (digital media store). Ask anything about sales, artists, customers, etc."
        )


if __name__ == "__main__":
    import time  # for latency

    main()
