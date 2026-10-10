# DHP on Docker — supervisor + transport server as a service.
FROM python:3.12-slim

WORKDIR /app

# install the package (and MCP extras for dhp-mcp)
COPY pyproject.toml README.md LICENSE ./
COPY dhp/ ./dhp/
RUN pip install --no-cache-dir ".[mcp]" && \
    rm -rf /root/.cache

# directories: /data = DHP store, /logs = transport logs
VOLUME ["/data", "/logs"]
EXPOSE 8471

# default: run the supervisor. Override CMD for transport server:
#   docker run ... dhp dhp-transport /logs --port 8471
CMD ["dhp-supervisor", "/data", "sup-docker", "86400"]
