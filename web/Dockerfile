# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
# Multi-stage: Node builds the static files; the runtime image is only nginx (unprivileged, uid 101).
# Every image is pinned by digest (coding standards section 1, "Base images").

FROM node:24-alpine@sha256:116b800c3754b440ee0aa3ed2f20f5bf3d99a554bfd9adeea09ea8017f6e85f5 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# nginx's mainline branch (the one nginx recommends): the 1.30 stable line had CRITICAL CVEs fixed only in mainline
# (Trivy, deployment doc section 4). apk upgrade takes Alpine's security fixes newer than the base image.
FROM nginxinc/nginx-unprivileged:1.31.7-alpine3.24@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e AS runtime
USER root
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 docker/config.sh /docker-entrypoint.d/40-circuitbreaker-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-qO-", "http://127.0.0.1:8080/healthz"]
