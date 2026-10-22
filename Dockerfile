FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DAA_MODE=demo
COPY pyproject.toml README.md constraints-py312.txt ./
COPY src ./src
RUN python -m pip --disable-pip-version-check install --no-cache-dir pip==26.2 \
    && python -m pip --disable-pip-version-check install --no-cache-dir -c constraints-py312.txt . \
    && python -m pip check
COPY demo ./demo
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home app \
    && mkdir -p /var/lib/daa/data && chown -R app:app /var/lib/daa
USER 1000:1000
ENV DAA_DATA_DIR=/var/lib/daa/data
EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "document_automation.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
