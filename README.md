# 🚀 AI Database Query Assistant

[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-FF4B4B.svg)](https://streamlit.io)
[![JSON-RPC 2.0](https://img.shields.io/badge/API-JSON--RPC%202.0-blueviolet)](https://www.jsonrpc.org/specification)
[![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://www.docker.com)
[![CI/CD](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions)

**Natural language → SQL system with visualizations and a vector store of past queries.**

> **Note:** the vector store (Chroma) keeps past question-and-SQL pairs and shows similar ones next to each answer. It is not used in SQL generation: the prompt sent to the model contains only the database schema and the question.

[**Live demo video:** 
](https://github.com/user-attachments/assets/bca6fb38-120b-4673-a3a0-d50051027b97
)

- **JSON-RPC API**: a FastAPI JSON-RPC endpoint (`/mcp`), not a full MCP implementation.
- **End-to-end tool creation** (LLM + vector DB + API + Docker + CI/CD)
- **Structured outputs**: the model returns SQL through OpenAI structured outputs. Latency and SQL validity have not been measured yet; no figures are claimed until an evaluation backs them.

## Quick Start
```bash
git clone https://github.com/randomdeliciousness/AI-Database-Query-Assistant.git
cd AI-Database-Query-Assistant
pip install -r requirements.txt
# Download Chinook DB if missing: curl -L -o chinook.db https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite
cp .streamlit/secrets.example.toml .streamlit/secrets.toml  # then put your key in it
# The FastAPI server (mcp_server.py) reads OPENAI_API_KEY from the environment instead.
streamlit run streamlit_app.py
```

Environment variables take precedence over .streamlit/secrets.toml.

**Limitations:** the app and API query only the bundled Chinook sample database. An OpenAI API key is required, and every question is a model call. Generated SQL is checked for safety, not for correctness. The read-only SQL guard is defense in depth (41 of 41 red-team cases in `tests/test_sql_guard.py` are blocked; CI enforces this on every commit), not a guarantee. Results stop at a row cap (10,000 rows by default) and the app says when that happens.
