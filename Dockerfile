FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DAA_MODE=demo
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY demo ./demo
RUN useradd --create-home app && mkdir /data && chown app:app /data
USER app
ENV DAA_DATA_DIR=/data
EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "document_automation.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
