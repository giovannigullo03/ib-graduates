#!/usr/bin/env bash
# Process one batch of the "unmatched graduates" enrichment pass, then exit.
# Safe to run on a timer: if the token budget is exhausted the run just fails
# and the next invocation picks up where the ledger left off.
#
#   crontab -e   then add (every 4 hours):
#   0 */4 * * *  /home/franco/Documents/Balseiro/Website/scripts/enrich_next_batch.sh >> /home/franco/Documents/Balseiro/Website/data/enrichment/cron.log 2>&1
#
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

LEDGER="data/enrichment/ledger.json"
if [ ! -f "$LEDGER" ]; then echo "no ledger at $LEDGER — nothing to do"; exit 0; fi

# stop the timer's work once nothing is pending
PENDING=$(python3 -c "import json;print(sum(1 for p in json.load(open('$LEDGER'))['people'] if p['status']=='pending'))")
echo "$(date -Is)  pending=$PENDING"
if [ "$PENDING" -eq 0 ]; then echo "queue drained — done"; exit 0; fi

claude -p "Follow scripts/enrich_unmatched.md and process exactly ONE batch, then stop." \
  --permission-mode acceptEdits \
  --allowedTools "Bash,Read,Edit,Write,WebSearch,WebFetch" \
  --add-dir "$REPO"
