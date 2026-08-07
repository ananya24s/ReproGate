# syntax=docker/dockerfile:1

# --- builder ---------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines VITE_* variables at build time, so the API origin is a build
# argument rather than a runtime environment variable.
ARG VITE_API_BASE_URL
ARG VITE_API_VERSION_PREFIX=/api/v1
ARG VITE_RUN_POLL_INTERVAL_MS=2000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_API_VERSION_PREFIX=$VITE_API_VERSION_PREFIX \
    VITE_RUN_POLL_INTERVAL_MS=$VITE_RUN_POLL_INTERVAL_MS

RUN npm run build

# --- runtime ---------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://localhost:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
