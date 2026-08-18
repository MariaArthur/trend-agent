"""
app.py
------
Streamlit UI for the market trend research agent. Two tabs:

1. Research Chat - talk to the agent, it tool-calls real web search +
   sentiment scoring and cites its sources, with a visible "researching..."
   indicator so the mechanism is transparent.
2. Watchlist Dashboard - manage monitored topics and see their real
   trend-over-time history (sentiment + source count charts) built from
   repeated scans, either run manually here or via scan_watchlist.py on a
   real schedule.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agent import run_agent
from tools import list_watchlist, get_watchlist_history, run_watchlist_scan, add_to_watchlist, remove_from_watchlist

st.set_page_config(page_title="Market Trend Research Agent", page_icon="📈", layout="centered")

st.title("📈 Market Trend Research Agent")
st.caption(
    "An agentic AI research assistant that pulls real web + news data, scores real sentiment, "
    "and tracks trends over time — not a chatbot answering from memory."
)

tab_chat, tab_watchlist = st.tabs(["💬 Research Chat", "📋 Watchlist Dashboard"])

# ---------------------------------------------------------------------------
# Tab 1: Research Chat
# ---------------------------------------------------------------------------
with tab_chat:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "display_history" not in st.session_state:
        st.session_state.display_history = []

    with st.expander("Try asking..."):
        st.markdown(
            "- What's trending in electric bikes right now?\n"
            "- Compare sentiment on Tesla vs Rivian\n"
            "- Add 'AI coding assistants' to my watchlist\n"
            "- What's the latest on the CHIPS Act? *(quick factual lookup)*\n"
            "- Refresh my watchlist now"
        )

    for entry in st.session_state.display_history:
        with st.chat_message(entry["role"]):
            if entry["role"] == "assistant" and entry.get("tool_calls"):
                for tc in entry["tool_calls"]:
                    st.markdown(f"🔎 *Researching with `{tc['name']}`...*")
            st.markdown(entry["text"])

    user_input = st.chat_input("Ask about a trend, brand, or market...")

    if user_input:
        st.session_state.display_history.append({"role": "user", "text": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        st.session_state.messages.append({"role": "user", "content": user_input})

        tool_calls_this_turn = []
        tool_call_placeholder = st.empty()

        def on_tool_call(name, args):
            tool_calls_this_turn.append({"name": name, "args": args})
            lines = "\n".join(f"🔎 *Researching with `{tc['name']}`...*" for tc in tool_calls_this_turn)
            tool_call_placeholder.markdown(lines)

        with st.chat_message("assistant"):
            with st.spinner("Researching..."):
                try:
                    final_text, updated_messages = run_agent(st.session_state.messages, on_tool_call=on_tool_call)
                    st.session_state.messages = updated_messages
                except RuntimeError as e:
                    final_text = f"⚠️ Configuration error: {e}"

            st.markdown(final_text)

        st.session_state.display_history.append(
            {"role": "assistant", "text": final_text, "tool_calls": tool_calls_this_turn}
        )

    if st.button("🔄 Reset conversation"):
        st.session_state.messages = []
        st.session_state.display_history = []
        st.rerun()

# ---------------------------------------------------------------------------
# Tab 2: Watchlist Dashboard
# ---------------------------------------------------------------------------
with tab_watchlist:
    st.subheader("Monitored topics")

    col1, col2 = st.columns([3, 1])
    with col1:
        new_topic = st.text_input("Add a topic to monitor", placeholder="e.g. electric bikes")
    with col2:
        st.write("")
        st.write("")
        if st.button("➕ Add", use_container_width=True) and new_topic:
            add_to_watchlist(new_topic)
            st.rerun()

    if st.button("🔁 Run scan now (all topics)"):
        with st.spinner("Scanning all watchlisted topics..."):
            result = run_watchlist_scan()
        if result["scanned"] == 0:
            st.info("Watchlist is empty - add a topic above first.")
        else:
            st.success(f"Scanned {result['scanned']} topic(s).")
        st.rerun()

    data = list_watchlist()

    if data["count"] == 0:
        st.info(
            "No topics on the watchlist yet. Add one above, or ask the chat agent to "
            "'add X to my watchlist'."
        )
    else:
        for entry in data["watchlist"]:
            topic = entry["topic"]
            latest = entry["latest_snapshot"]

            with st.container(border=True):
                header_col, remove_col = st.columns([5, 1])
                with header_col:
                    st.markdown(f"### {topic}")
                with remove_col:
                    if st.button("🗑️", key=f"remove_{topic}"):
                        remove_from_watchlist(topic)
                        st.rerun()

                if not latest:
                    st.caption("No scans yet — click 'Run scan now' above.")
                    continue

                m1, m2, m3 = st.columns(3)
                m1.metric("Sentiment", latest["sentiment_label"].title(), f"{latest['avg_sentiment']:+.2f}")
                m2.metric("Sources", latest["total_sources"])
                m3.metric("Confidence", latest["confidence"].title())

                history = get_watchlist_history(topic)
                if history["found"] and len(history["snapshots"]) > 1:
                    df = pd.DataFrame(history["snapshots"])
                    df["run_at"] = pd.to_datetime(df["run_at"])

                    fig = go.Figure()
                    fig.add_trace(
                        go.Scatter(x=df["run_at"], y=df["avg_sentiment"], mode="lines+markers", name="Sentiment")
                    )
                    fig.update_layout(
                        height=220,
                        margin=dict(l=10, r=10, t=10, b=10),
                        yaxis_title="Sentiment (-1 to +1)",
                        showlegend=False,
                    )
                    st.plotly_chart(fig, use_container_width=True, key=f"chart_{topic}")
                else:
                    st.caption("Run a few scans over time to build a trend chart.")

    st.divider()
    st.caption(
        "💡 For real ongoing monitoring, `run_watchlist_scan()` is also exposed as a standalone "
        "script (`scan_watchlist.py`) that a real cron job or GitHub Actions schedule can call "
        "automatically — see the README."
    )
