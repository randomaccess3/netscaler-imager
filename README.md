# NetScaler Imager

A Python tool for creating disk images from Citrix NetScaler appliances via SSH.

## Features

- Images `/dev/da0` and `/dev/md0` (or select individually)
- In-place progress bar with percentage and transfer size
- Supports password or certificate-based SSH authentication
- Timestamped log file for each run
- No credentials stored in the script

## Requirements

- Python 3.10+
- SSH client available on PATH
- Network access to the NetScaler management interface

## Usage

```bash
# Image both disks (SSH will prompt for password)
python netscaler_imager.py --host 10.0.0.1

# Use an SSH key and specify output directory
python netscaler_imager.py --host ns.example.com --cert ~/.ssh/id_rsa -o /backups

# Image only da0 with a custom user and port
python netscaler_imager.py --host 10.0.0.1 --disks da0 --user admin --port 2222
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--host` | NetScaler IP or hostname | *(required)* |
| `--user`, `-u` | SSH username | `nsroot` |
| `--port`, `-p` | SSH port | `22` |
| `--cert`, `-c` | Path to SSH private key | *(password prompt)* |
| `--disks`, `-d` | Disks to image: `da0`, `md0`, or `both` | `both` |
| `-o`, `--output` | Output directory | `.` (current dir) |

## Output

The tool produces:

- `da0.img` / `md0.img` — raw disk images (with 6-byte header and trailer stripped)
- `netscaler_imager_YYYYMMDD_HHMMSS.log` — detailed log of the session

## How It Works

1. Connects via SSH to query disk sizes for progress tracking
2. Streams each disk using `dd` over SSH
3. Strips the 6-byte header and 6-byte trailer from the raw stream
4. Writes the image to the local output directory
5. Displays a live progress bar during transfer

## Security

- No passwords or keys are stored in the script
- SSH key can be provided via `--cert`; otherwise SSH prompts interactively
- `.gitignore` excludes keys, logs, and image files from version control
