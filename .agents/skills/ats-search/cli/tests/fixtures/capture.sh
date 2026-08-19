#!/usr/bin/env bash
# Refresh the ats-search fixtures against the live endpoints.
#
# A vendor schema change should be a deliberate one-command refresh with a dated
# diff, not an archaeology exercise. Run this, eyeball `git diff`, then minimize
# the new payloads back down (2-4 postings each) keeping the cases the tests need:
#   * one posting that passes every filter
#   * one multi-location posting
#   * one with NO posting date
#   * one remote posting whose geography is a region, not a country
#   * one German-language posting
# and re-write each file's `_capture` block with today's date.
#
# Volume: 8 requests. Keep it that way - these are other people's servers.
#
# NOTE: api.smartrecruiters.com/robots.txt disallows every agent but LinkedInBot.
# The SmartRecruiters lines below are commented out for that reason; uncomment
# them only if you have decided to run that vendor as personal use.
set -euo pipefail
cd "$(dirname "$0")"
UA="ats-search-cli/1.0"
D=$(date +%Y%m%d)

curl -sS -A "$UA" "https://boards-api.greenhouse.io/v1/boards/parloa/jobs?content=true" \
  -o "greenhouse-parloa-$D.json"
curl -sS -A "$UA" "https://api.ashbyhq.com/posting-api/job-board/deepjudge" \
  -o "ashby-deepjudge-$D.json"
curl -sS -A "$UA" "https://merantix.jobs.personio.de/xml" \
  -o "personio-merantix-$D.xml"
curl -sS -A "$UA" "https://api.lever.co/v0/postings/sonarsource?mode=json" \
  -o "lever-sonarsource-$D.json"

# curl -sS -A "$UA" "https://api.smartrecruiters.com/v1/companies/nexthink/postings?limit=2&offset=0" \
#   -o "smartrecruiters-nexthink-p1-$D.json"
# curl -sS -A "$UA" "https://api.smartrecruiters.com/v1/companies/zzz-not-a-real-company-xyz/postings?limit=2&offset=0" \
#   -o "smartrecruiters-garbage-$D.json"

echo "captured with date suffix $D - now minimize, update _capture, and update map.json"
