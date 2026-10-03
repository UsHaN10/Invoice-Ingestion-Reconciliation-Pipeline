#!/usr/bin/env python3
"""
Single-command local development runner for the Reconciliation Pipeline.
Starts Redis, Mock ERP, Celery Worker, and Django API concurrently.
Cleanly shuts down all services on Ctrl+C.

Usage:
    python start_system.py
"""
import os
import signal
import subprocess
import sys
import time


def main():
    print("=" * 65)
    print("  Starting Invoice Reconciliation Pipeline Services (Local Mode)")
    print("=" * 65)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    processes = []

    try:
        # 1. Run migrations and SQLite WAL setup
        print("\n[1/4] Ensuring database migrations and WAL mode...")
        subprocess.run([sys.executable, "manage.py", "migrate"], cwd=base_dir, check=True)

        # 2. Start standalone Redis server
        print("\n[2/4] Starting Redis Server on 127.0.0.1:6379...")
        redis_proc = subprocess.Popen(
            [sys.executable, "run_local_redis.py"],
            cwd=base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        processes.append(("Redis", redis_proc))
        time.sleep(1.0)

        # 3. Start Mock ERP
        print("\n[3/4] Starting Mock ERP on http://127.0.0.1:9100...")
        erp_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mock_erp:app", "--host", "127.0.0.1", "--port", "9100"],
            cwd=base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        processes.append(("Mock ERP", erp_proc))
        time.sleep(1.5)

        # 4. Start Celery Worker
        print("\n[4/4] Starting Celery Worker (reconciliation queue)...")
        celery_proc = subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "core", "worker", "-l", "info", "-P", "solo"],
            cwd=base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        processes.append(("Celery Worker", celery_proc))
        time.sleep(2.0)

        # 5. Start Django API (foreground)
        print("\n" + "=" * 65)
        print("  All Services Are Running Successfully!")
        print("  - Django API:      http://127.0.0.1:8000")
        print("  - Mock ERP:        http://127.0.0.1:9100")
        print("  - Redis Server:    127.0.0.1:6379")
        print("  - Celery Worker:   Active")
        print("  - Health Check:    http://127.0.0.1:8000/healthz")
        print("  - Reporting API:   http://127.0.0.1:8000/api/reporting/summary/")
        print("  - Metrics:         http://127.0.0.1:8000/metrics")
        print("=" * 65)
        print("Press Ctrl+C at any time to gracefully stop all services.\n")

        # Run Django runserver in the current process
        subprocess.run(
            [sys.executable, "manage.py", "runserver", "127.0.0.1:8000"],
            cwd=base_dir
        )

    except KeyboardInterrupt:
        print("\nStopping all services...")
    finally:
        for name, proc in processes:
            print(f"Stopping {name} (PID {proc.pid})...")
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
        print("All services stopped.")


if __name__ == "__main__":
    main()
