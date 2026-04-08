FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501 8000
CMD ["sh", "-c", "streamlit run streamlit_app.py & uvicorn mcp_server:app --host 0.0.0.0 --port 8000"]
