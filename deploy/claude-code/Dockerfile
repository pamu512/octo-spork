# Multi-stage: compilers and Bun caches stay out of the final image (smaller, faster pulls).
# Final stage: oven/bun + ripgrep/git + production node_modules only.
#
# Build default (last stage):  docker build -t claude-agent:opt deploy/claude-code
# Explicit runtime stage:     docker build --target runtime -t claude-agent:opt deploy/claude-code

FROM oven/bun:alpine AS deps
RUN apk add --no-cache build-base git

WORKDIR /app
COPY package.json bun.lock ./
COPY src ./src
RUN bun install --frozen-lockfile

FROM oven/bun:alpine AS runtime
RUN apk add --no-cache ripgrep git

WORKDIR /app
COPY package.json bun.lock ./
COPY src ./src
COPY --from=deps /app/node_modules ./node_modules

ENV NODE_ENV=production \
    CLAUDE_CONFIG_DIR=/app/.local/claude_config

ENTRYPOINT ["bun", "run", "src/main.tsx"]
