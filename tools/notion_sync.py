#!/usr/bin/env python3
"""Sync the application tracker with the owner's Notion database.

    python3 tools/notion_sync.py check      # verify token + data source
    python3 tools/notion_sync.py pull       # Notion Stage -> tracker, PDFs -> Notion
    python3 tools/notion_sync.py push       # every tracker row with documents -> Notion

The board does both on its own (push on publish, pull at start and every ten
minutes); this entry point is for slash commands, cron, and backfills. Field
ownership and the config file are documented in `tools/board/notion.py`.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board import docs, notion  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("check", "pull", "push"))
    args = ap.parse_args(argv)
    cfg = notion.load_config()
    if cfg is None:
        print("Notion sync is not configured: create %s with "
              '{"token": "<integration secret>", "data_source_id": "<id>"}'
              % notion.config_path())
        return 1
    try:
        if args.command == "check":
            print("ok - %d rows in the Notion data source" % len(notion.query_all(cfg)))
        elif args.command == "pull":
            result = notion.pull()
            print("pulled %(pages)d rows; filled fields on %(derived)d; "
                  "attached PDFs to %(documents)d; updated %(tracker)d tracker rows"
                  % result)
        else:
            if not docs.TRACKER.exists():
                print("no job_search_tracker.csv yet - nothing to push")
                return 0
            with open(docs.TRACKER, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                if not (row.get("cv_file") or row.get("cover_letter_file")):
                    continue
                record = {"job_url": row.get("source") or "", "company": row["company"],
                          "role": row["role"],
                          "targets": {"cv": row.get("cv_file"),
                                      "cover": row.get("cover_letter_file")}}
                print("%-9s %s - %s" % (notion.push(record), row["company"], row["role"]))
    except notion.NotionError as exc:
        print("notion sync failed: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
