#!/usr/bin/env python3
"""NetScaler Imaging Tool — creates disk images from a NetScaler appliance via SSH."""

import argparse
import logging
import os
import subprocess
import sys
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path


DISKS = ["da0", "md0"]
DD_BS = "10M"
# The original command strips 6 leading bytes and 6 trailing bytes:
#   tail -c +7  → skip first 6 bytes (output from byte 7 onward)
#   head -c -6  → drop last 6 bytes
TAIL_SKIP = 7   # tail -c +7
HEAD_TRIM = 6    # head -c -6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create disk images from a Citrix NetScaler appliance via SSH.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --host 10.0.0.1\n"
            "  %(prog)s --host ns.example.com --cert ~/.ssh/id_rsa -o /backups\n"
            "  %(prog)s --host 10.0.0.1 --disks da0 --user admin --port 2222\n"
        ),
    )
    parser.add_argument("--host", required=True, help="NetScaler IP address or hostname")
    parser.add_argument("--user", "-u", default="nsroot", help="SSH username (default: nsroot)")
    parser.add_argument("--port", "-p", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--cert", "-c", metavar="KEY", help="Path to SSH private key file")
    parser.add_argument(
        "--disks", "-d",
        nargs="+",
        choices=DISKS + ["both"],
        default=["both"],
        help="Disks to image (default: both). Choices: da0, md0, both",
    )
    parser.add_argument(
        "-o", "--output",
        default=".",
        help="Output directory for image and log files (default: current directory)",
    )
    return parser.parse_args()


def resolve_disks(disks: list[str]) -> list[str]:
    if "both" in disks:
        return list(DISKS)
    return sorted(set(disks))


def setup_logging(output_dir: Path) -> logging.Logger:
    log_file = output_dir / f"netscaler_imager_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger("netscaler_imager")
    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info("Log file: %s", log_file)
    return logger


def build_ssh_base(user: str, host: str, port: int, cert: str | None) -> list[str]:
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port),
    ]
    if cert:
        cmd += ["-i", cert]
    cmd.append(f"{user}@{host}")
    return cmd


def get_disk_size(ssh_base: list[str], disk: str, logger: logging.Logger) -> int | None:
    """SSH into the NetScaler and return the raw byte size of /dev/<disk>."""
    # diskinfo prints the size in bytes on FreeBSD
    cmd = ssh_base + [f"shell diskinfo -v /dev/{disk}"]
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        logger.debug("diskinfo output:\n%s", result.stdout)
        if result.returncode != 0:
            logger.warning("diskinfo failed (rc=%d): %s", result.returncode, result.stderr.strip())
            # Fallback: try to parse the mediasize from dmesg or other means
            return _get_disk_size_fallback(ssh_base, disk, logger)

        # diskinfo -v output contains a line like:
        #   512             # sectorsize
        #   20064256000     # mediasize in bytes
        # We look for "mediasize in bytes"
        for line in result.stdout.splitlines():
            if "mediasize in bytes" in line.lower():
                match = re.search(r"(\d+)", line)
                if match:
                    return int(match.group(1))

        # If the verbose format isn't available, plain diskinfo prints:
        #   /dev/da0    512    20064256000    39172375    ...
        parts = result.stdout.split()
        if len(parts) >= 3:
            try:
                return int(parts[2])
            except ValueError:
                pass

        logger.warning("Could not parse disk size from diskinfo output")
        return _get_disk_size_fallback(ssh_base, disk, logger)

    except subprocess.TimeoutExpired:
        logger.warning("Timed out getting disk size for %s", disk)
        return None
    except Exception as exc:
        logger.warning("Error getting disk size for %s: %s", disk, exc)
        return None


def _get_disk_size_fallback(ssh_base: list[str], disk: str, logger: logging.Logger) -> int | None:
    """Fallback: use sysctl or other methods to estimate disk size."""
    cmd = ssh_base + [f"shell sysctl kern.geom.debugflags 2>/dev/null; "
                      f"ls -l /dev/{disk} 2>/dev/null; "
                      f"stat -f %z /dev/{disk} 2>/dev/null"]
    logger.debug("Fallback size query: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        logger.debug("Fallback output:\n%s", result.stdout)
        # Try to find any large number that looks like a byte count
        for line in result.stdout.splitlines():
            match = re.search(r"(\d{9,})", line)  # 9+ digits ≈ ≥1 GB
            if match:
                return int(match.group(1))
    except Exception as exc:
        logger.debug("Fallback size query failed: %s", exc)
    return None


def progress_monitor(
    img_path: Path,
    total_bytes: int | None,
    disk: str,
    logger: logging.Logger,
    stop_event: threading.Event,
):
    """Background thread that prints an in-place progress bar while the image is being written."""
    BAR_WIDTH = 30

    while not stop_event.is_set():
        stop_event.wait(2)
        if stop_event.is_set():
            break
        try:
            current = img_path.stat().st_size
        except FileNotFoundError:
            continue

        if total_bytes and total_bytes > 0:
            pct = min(current / total_bytes, 1.0)
            filled = int(BAR_WIDTH * pct)
            bar = "█" * filled + " " * (BAR_WIDTH - filled)
            line = (
                f"\r  [{disk}] [{bar}] {pct * 100:5.1f}%  "
                f"{_human_bytes(current)} / {_human_bytes(total_bytes)}"
            )
        else:
            # Unknown total — show a spinner-style bar
            line = f"\r  [{disk}] {_human_bytes(current)} written ..."

        sys.stdout.write(line)
        sys.stdout.flush()
        logger.debug("[%s] %s / %s", disk, _human_bytes(current),
                     _human_bytes(total_bytes) if total_bytes else "unknown")

    # Clear the line and move to next line when done
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def image_disk(
    ssh_base: list[str],
    disk: str,
    output_dir: Path,
    total_bytes: int | None,
    logger: logging.Logger,
) -> Path:
    """Stream a disk image from the NetScaler and save it locally."""
    img_path = output_dir / f"{disk}.img"
    remote_cmd = f"shell dd if=/dev/{disk} bs={DD_BS}"
    cmd = ssh_base + [remote_cmd]

    logger.info("Starting image of /dev/%s → %s", disk, img_path)
    logger.debug("Command: %s | tail -c +%d | head -c -%d > %s",
                 " ".join(cmd), TAIL_SKIP, HEAD_TRIM, img_path)

    stop_event = threading.Event()
    monitor = threading.Thread(
        target=progress_monitor,
        args=(img_path, total_bytes, disk, logger, stop_event),
        daemon=True,
    )

    start_time = time.monotonic()
    monitor.start()

    try:
        with open(img_path, "wb") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # We need to replicate:  tail -c +7 | head -c -6
            # tail -c +7  → skip first 6 bytes
            # head -c -6  → drop last 6 bytes
            #
            # Since we're streaming, we can skip the first 6 bytes easily.
            # For the trailing 6 bytes, we hold a buffer and flush with a delay.
            skip_remaining = TAIL_SKIP - 1  # bytes to discard from start (6)
            tail_buf = b""  # holds the last HEAD_TRIM bytes
            chunk_size = 1024 * 1024  # 1 MB read chunks

            while True:
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break

                # Skip leading bytes
                if skip_remaining > 0:
                    if len(chunk) <= skip_remaining:
                        skip_remaining -= len(chunk)
                        continue
                    chunk = chunk[skip_remaining:]
                    skip_remaining = 0

                # Buffer the trailing bytes: append new data, write all but last HEAD_TRIM
                tail_buf += chunk
                if len(tail_buf) > HEAD_TRIM:
                    to_write = tail_buf[:-HEAD_TRIM]
                    tail_buf = tail_buf[-HEAD_TRIM:]
                    f.write(to_write)

            # Don't write tail_buf — those are the last 6 bytes we want to drop

            proc.wait()
            stderr_out = proc.stderr.read().decode(errors="replace").strip()
            if stderr_out:
                logger.debug("dd stderr: %s", stderr_out)

    finally:
        stop_event.set()
        monitor.join(timeout=2)

    elapsed = time.monotonic() - start_time
    final_size = img_path.stat().st_size
    logger.info(
        "Completed %s — %s in %.1f s",
        disk,
        _human_bytes(final_size),
        elapsed,
    )

    if proc.returncode != 0:
        logger.warning("SSH/dd process exited with code %d for %s", proc.returncode, disk)

    return img_path


def main():
    args = parse_args()
    disks = resolve_disks(args.disks)
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(output_dir)
    logger.info("=" * 60)
    logger.info("NetScaler Imager")
    logger.info("=" * 60)
    logger.info("Host:   %s", args.host)
    logger.info("User:   %s", args.user)
    logger.info("Port:   %d", args.port)
    logger.info("Cert:   %s", args.cert or "(password auth)")
    logger.info("Disks:  %s", ", ".join(disks))
    logger.info("Output: %s", output_dir)
    logger.info("-" * 60)

    ssh_base = build_ssh_base(args.user, args.host, args.port, args.cert)

    # Phase 1: query disk sizes
    disk_sizes: dict[str, int | None] = {}
    for disk in disks:
        logger.info("Querying size of /dev/%s ...", disk)
        size = get_disk_size(ssh_base, disk, logger)
        disk_sizes[disk] = size
        if size:
            logger.info("  /dev/%s = %s", disk, _human_bytes(size))
        else:
            logger.warning("  Could not determine size of /dev/%s — progress will be estimated", disk)

    # Phase 2: image each disk
    created_images: list[Path] = []
    for disk in disks:
        try:
            img = image_disk(ssh_base, disk, output_dir, disk_sizes.get(disk), logger)
            created_images.append(img)
        except KeyboardInterrupt:
            logger.error("Interrupted by user during %s imaging", disk)
            sys.exit(1)
        except Exception as exc:
            logger.error("Failed to image %s: %s", disk, exc, exc_info=True)

    # Summary
    logger.info("-" * 60)
    logger.info("Summary:")
    for img in created_images:
        logger.info("  %s  (%s)", img, _human_bytes(img.stat().st_size))
    if len(created_images) == len(disks):
        logger.info("All disks imaged successfully.")
    else:
        logger.warning("Some disks failed — check the log for details.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
