# syntax=docker/dockerfile:1
# Multi-stage: Node builds the static files; the runtime image is only nginx (unprivileged, uid 101).

FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# nginx's mainline branch (the one nginx recommends): the 1.30 stable line had CRITICAL CVEs fixed only in mainline
# (Trivy, deployment doc section 4). apk upgrade takes Alpine's security fixes newer than the base image.
FROM nginxinc/nginx-unprivileged:1.31.7-alpine3.24 AS runtime
USER root
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 docker/config.sh /docker-entrypoint.d/40-circuitbreaker-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-qO-", "http://127.0.0.1:8080/healthz"]
