#!/usr/bin/env bash
set -euo pipefail
docker compose up -d
docker compose logs -f gateway
