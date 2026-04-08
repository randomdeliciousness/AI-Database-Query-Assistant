# 🚀 AI Database Query Assistant (MCP + Runlayer Ready)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-FF4B4B.svg)](https://streamlit.io)
[![MCP Compatible](https://img.shields.io/badge/MCP-JSON--RPC%202.0-blueviolet)](https://www.runlayer.com)
[![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://www.docker.com)
[![CI/CD](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions)

**Production-grade natural language → SQL system with vector RAG, visualizations, and enterprise MCP security layer.**

[**Live demo video:** 
](https://github.com/user-attachments/assets/bca6fb38-120b-4673-a3a0-d50051027b97
)

- **Custom MCP Server** built from scratch → routed through Runlayer control plane (permissions, threat detection, audit logs, zero-trust)
- **End-to-end tool creation** (LLM + vector DB + secure API + Docker + CI/CD)
- **Measurable impact**: <300 ms average latency, 100% SQL validity via structured outputs

## Quick Start
```bash
git clone https://github.com/randomdeliciousness/AI-Database-Query-Assistant.git
cd AI-Database-Query-Assistant
pip install -r requirements.txt
# Download Chinook DB if missing: curl -L -o chinook.db https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite
cp .streamlit/secrets.example.toml .streamlit/secrets.toml  # add your OpenAI key
streamlit run streamlit_app.py

✅ CI now fully passing with automatic Black + Ruff formatting!
