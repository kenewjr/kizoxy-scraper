FROM python:3.11-slim

# System dependencies for Firefox / Playwright / Camoufox
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdbus-glib-1-2 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgcc1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libxt6 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd -r scraper && useradd -r -g scraper -d /app -s /sbin/nologin scraper

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
RUN pip install --no-cache-dir .

COPY app/ ./app/

RUN chown -R scraper:scraper /app
USER scraper

# Download Camoufox browser binaries AS the user that will actually run
# the server — fetching before the USER switch (as root) put the binaries
# in root's cache dir, which the non-root runtime user can't see.
ENV GITHUB_TOKEN=""
RUN python -m camoufox fetch

EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
