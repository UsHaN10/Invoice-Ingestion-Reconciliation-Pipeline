#!/usr/bin/env python3
"""
Feeder script to stream invoices into the Ingestion API.
Supports piping from generate_invoices.py or reading from an NDJSON file.

Usage:
    # Generate 50 invoices and submit them to the API:
    python generate_invoices.py --count 50 | python submit_invoices.py

    # Or specify count directly (runs generator internally):
    python submit_invoices.py --count 20 --url http://localhost:8000/api/invoices/
"""
import argparse
import json
import sys
import time
import requests
from generate_invoices import generate


def submit_invoice(url: str, invoice_data: dict):
    try:
        response = requests.post(url, json=invoice_data, timeout=15.0)
        return response.status_code, response.json()
    except Exception as e:
        return 0, {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Submit invoices to the Ingestion API.")
    parser.add_argument("--url", default="http://localhost:8000/api/invoices/", help="Ingestion API URL")
    parser.add_argument("--count", type=int, default=None, help="Number of synthetic invoices to generate and send")
    parser.add_argument("--seed", type=int, default=42, help="Seed for invoice generator (default: 42)")
    parser.add_argument("--file", type=str, default=None, help="Path to NDJSON file with invoices (or '-' for stdin)")
    args = parser.parse_args()

    url = args.url
    print(f"Connecting to Ingestion API: {url}...")

    # Determine stream source
    if args.count is not None:
        invoice_stream = generate(args.count, args.seed)
    elif args.file and args.file != "-":
        with open(args.file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        invoice_stream = iter(lines)
    else:
        # Read from stdin
        invoice_stream = (json.loads(line) for line in sys.stdin if line.strip())

    submitted = 0
    accepted = 0
    duplicates = 0
    errors = 0

    print("Beginning invoice submission...")
    start_time = time.time()

    for inv in invoice_stream:
        status_code, resp = submit_invoice(url, inv)
        submitted += 1

        if status_code == 202:
            accepted += 1
            print(f"[{submitted}] Ingested {inv.get('invoice_id')} -> Queued")
        elif status_code == 200 and resp.get("is_duplicate"):
            duplicates += 1
            print(f"[{submitted}] Ingested {inv.get('invoice_id')} -> Duplicate Detected")
        else:
            errors += 1
            print(f"[{submitted}] Ingested {inv.get('invoice_id')} -> Status {status_code}: {resp}")

    elapsed = time.time() - start_time
    print("-" * 50)
    print(f"Submission complete in {elapsed:.2f}s:")
    print(f"  Total sent:      {submitted}")
    print(f"  Accepted/Queued: {accepted}")
    print(f"  Duplicates:      {duplicates}")
    print(f"  Errors/Rejected: {errors}")


if __name__ == "__main__":
    main()
