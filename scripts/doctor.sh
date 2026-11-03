#!/usr/bin/env bash
# make doctor: check (and print versions of) every tool a developer needs.
# Exit code 1 if a REQUIRED tool is missing; warnings do not fail.
set -uo pipefail

MIN_NODE_MAJOR=22      # Vite 8 needs ^20.19 or >=22.12; Vitest 5 and ESLint 10 need >=22.12. Use 24 LTS.
MIN_NODE_MINOR=12
MIN_DOCKER_BYTES=9500000000   # "10 GB" in Docker Desktop is ~10.4e9 bytes; warn below ~9.5e9

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'; else G=; R=; Y=; N=; fi
fail=0; warn=0
ok()   { printf "  %s✓%s %-14s %s\n" "$G" "$N" "$1" "$2"; }
bad()  { printf "  %s✗%s %-14s %s\n    fix: %s\n" "$R" "$N" "$1" "$2" "$3"; fail=1; }
wrn()  { printf "  %s!%s %-14s %s\n    fix: %s\n" "$Y" "$N" "$1" "$2" "$3"; warn=1; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "CircuitBreaker prerequisite check"

# --- Docker + Compose -------------------------------------------------------
if have docker; then
  ok "docker" "$(docker --version 2>&1 | head -1)"
  if docker compose version >/dev/null 2>&1; then
    ok "compose" "$(docker compose version 2>&1 | head -1)"
  else
    bad "compose" "Docker Compose v2 plugin not found" "update Docker Desktop (https://docs.docker.com/get-docker/)"
  fi
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)
  if [ -z "$mem" ]; then
    bad "docker daemon" "not running" "start Docker Desktop and wait until it says 'Running'"
  elif [ "$mem" -lt "$MIN_DOCKER_BYTES" ]; then
    wrn "docker memory" "$(awk -v b="$mem" 'BEGIN{printf "%.1f GiB", b/1073741824}') allocated (want ~10 GB)" \
        "Docker Desktop > Settings > Resources > Memory limit: 10 GB"
  else
    ok "docker memory" "$(awk -v b="$mem" 'BEGIN{printf "%.1f GiB", b/1073741824}') allocated"
  fi
else
  bad "docker" "not installed" "install Docker Desktop: https://docs.docker.com/get-docker/"
fi

# --- Python + uv ------------------------------------------------------------
if have uv; then
  ok "uv" "$(uv --version)"
  if py=$(uv python find 3.12 2>/dev/null); then
    ok "python 3.12" "$("$py" --version 2>&1) ($py)"
  else
    bad "python 3.12" "not found" "uv python install 3.12"
  fi
else
  bad "uv" "not installed" "brew install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
fi

# --- Node + npm -------------------------------------------------------------
if have node; then
  nv=$(node --version | sed 's/^v//'); nmaj=${nv%%.*}; rest=${nv#*.}; nmin=${rest%%.*}
  if [ "$nmaj" -gt "$MIN_NODE_MAJOR" ] || { [ "$nmaj" -eq "$MIN_NODE_MAJOR" ] && [ "$nmin" -ge "$MIN_NODE_MINOR" ]; }; then
    ok "node" "v$nv"
  else
    hint="brew install node@24 && brew link --overwrite --force node@24   (or use nvm: nvm install 24)"
    if [ -x /opt/homebrew/opt/node@24/bin/node ]; then
      hint="node@24 is installed but not first on PATH: brew link --overwrite --force node@24"
    fi
    bad "node" "v$nv is too old (need >= 22.12; 24 LTS recommended)" "$hint"
  fi
  have npm && ok "npm" "$(npm --version)" || bad "npm" "not found" "comes with Node"
else
  bad "node" "not installed" "brew install node@24 && brew link --overwrite --force node@24"
fi

# --- Kubernetes tools (only installed and checked in Phase 1) ---------------
have kind    && ok "kind"    "$(kind version 2>&1 | head -1)"        || bad "kind"    "not installed" "brew install kind"
if have kubectl; then ok "kubectl" "$(kubectl version --client 2>&1 | head -1)"; else bad "kubectl" "not installed" "brew install kubectl"; fi
have helm    && ok "helm"    "$(helm version --short 2>&1 | head -1)" || bad "helm"    "not installed" "brew install helm"

# --- Developer tooling ------------------------------------------------------
have make       && ok "make"       "$(make --version 2>&1 | head -1)"       || bad "make"       "not installed" "xcode-select --install"
have git        && ok "git"        "$(git --version)"                        || bad "git"        "not installed" "xcode-select --install"
have gitleaks   && ok "gitleaks"   "$(gitleaks version 2>&1 | head -1)"     || bad "gitleaks"   "not installed" "brew install gitleaks"
have pre-commit && ok "pre-commit" "$(pre-commit --version 2>&1 | head -1)" || bad "pre-commit" "not installed" "brew install pre-commit"

echo
if [ "$fail" -ne 0 ]; then
  echo "${R}Some required tools are missing.${N} Fix the items marked ✗ and run 'make doctor' again."
  exit 1
fi
[ "$warn" -ne 0 ] && echo "${Y}All required tools found, with warnings above.${N}" || echo "${G}All good.${N} Next: make setup && make up"
exit 0
