#!/usr/bin/env bash
# Unit tests for every service, each in its own uv environment.
#
#   ./run-tests.sh                      all services
#   ./run-tests.sh query -k chat        one service, extra arguments go to pytest
#   ./run-tests.sh ingest -m "not integration"   skip the tests needing docling-serve
#
# The unit tests need no running services, network, .env or data/: Ollama and
# Qdrant are faked, and PDFs are generated on the fly. The ingest tests marked
# "integration" run the real OCR route against docling-serve when it's up, and
# skip themselves when it isn't.
set -euo pipefail

cd "$(dirname "$0")/services"

SERVICES=(ingest embed query)
if [[ $# -gt 0 && -d "$1/tests" ]]; then
    SERVICES=("$1")
    shift
fi

failed=()
for service in "${SERVICES[@]}"; do
    printf '\n\033[1m== %s\033[0m\n' "$service"
    # uv run includes the dev dependency group (pytest) by default.
    if ! (cd "$service" && uv run --quiet pytest "$@"); then
        failed+=("$service")
    fi
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All tests passed (${SERVICES[*]})."
