#!/bin/sh
# Copyright 2026 Thallium Solutions di Busconi Alessandro.
# SPDX-License-Identifier: Apache-2.0
set -eu

NAME="${LAM_JOBS_TEST_CONTAINER:-lam-jobs-test-postgres}"
IMAGE="postgres:16-alpine"
PASSWORD="lamjobs"
DATABASE="lamjobs"
STARTED=""

usage() {
    cat <<'EOF'
Usage: sh test-services.sh [--detach | --stop | --url]

Starts a disposable PostgreSQL 16 container (postgres:16-alpine) on a free
local port for the @lam/jobs integration tests and prints the URL to export:

  export LAM_JOBS_TEST_DATABASE_URL=postgres://postgres:lamjobs@127.0.0.1:<port>/lamjobs?sslmode=disable

  (no option)  run in the foreground; the container stops on Ctrl-C/exit
  --detach     start (or reuse) the container, print the export line, exit;
               use it as: eval "$(sh test-services.sh --detach)"
  --url        print the export line of the running container
  --stop       stop the container started with --detach

The container name defaults to lam-jobs-test-postgres
(override with LAM_JOBS_TEST_CONTAINER). An already running container with
that name is reused and left running.
EOF
}

MODE=foreground
case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    --detach) MODE=detach ;;
    --stop) MODE=stop ;;
    --url) MODE=url ;;
    "") ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed or not on PATH" >&2
    exit 1
fi

container_running() {
    [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = "true" ]
}

container_port() {
    docker port "$NAME" 5432/tcp 2>/dev/null | head -n 1 | sed 's/.*://'
}

free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

export_line() {
    echo "export LAM_JOBS_TEST_DATABASE_URL='postgres://postgres:$PASSWORD@127.0.0.1:$1/$DATABASE?sslmode=disable'"
}

wait_ready() {
    tries=0
    until docker exec "$NAME" pg_isready -U postgres -d "$DATABASE" -h 127.0.0.1 >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -gt 60 ]; then
            echo "error: PostgreSQL in $NAME did not become ready" >&2
            exit 1
        fi
        sleep 0.5
    done
}

if [ "$MODE" = stop ]; then
    if container_running; then
        docker stop "$NAME" >/dev/null
        echo "[test-services] stopped $NAME" >&2
    else
        echo "[test-services] $NAME is not running" >&2
    fi
    exit 0
fi

if [ "$MODE" = url ]; then
    if ! container_running; then
        echo "error: $NAME is not running" >&2
        exit 1
    fi
    export_line "$(container_port)"
    exit 0
fi

if container_running; then
    echo "[test-services] $NAME already running; leaving it untouched" >&2
else
    if docker container inspect "$NAME" >/dev/null 2>&1; then
        echo "error: container $NAME exists but is stopped; remove or start it first" >&2
        exit 1
    fi
    PORT=$(free_port)
    echo "[test-services] starting $NAME ($IMAGE) on 127.0.0.1:$PORT" >&2
    docker run --rm -d --name "$NAME" \
        -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB="$DATABASE" \
        -p "127.0.0.1:$PORT:5432" "$IMAGE" \
        -c max_connections=200 -c fsync=off -c synchronous_commit=off >/dev/null
    if [ "$MODE" = foreground ]; then
        STARTED="$NAME"
    fi
fi

cleanup() {
    if [ -n "$STARTED" ]; then
        echo
        echo "[test-services] stopping $STARTED"
        docker stop "$STARTED" >/dev/null 2>&1 || true
        STARTED=""
    fi
}

stop() {
    cleanup
    trap - EXIT INT TERM
    exit 0
}

trap cleanup EXIT
trap stop INT TERM

wait_ready
# The entrypoint restarts the server once after initialisation.
sleep 1
wait_ready
LINE=$(export_line "$(container_port)")

if [ "$MODE" = detach ]; then
    trap - EXIT INT TERM
    echo "$LINE"
    exit 0
fi

cat <<EOF

[test-services] PostgreSQL is ready. In another terminal run:

  $LINE
  lamc lib run test

[test-services] Press Ctrl-C here to stop the container.
EOF

while :; do
    sleep 3600 &
    wait $!
done
