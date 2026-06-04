# AGENTS.md

## Project Overview

This project provides a small Flask API for running perfSONAR-related network measurements.

- Main file: `perfsonar.py`
- PM2 config: `ecosystem.config.js`
- Default API port: `5013`
- Default pScheduler server: `https://192.168.200.222/pscheduler`

The API is intended to run on `192.168.200.222`, where perfSONAR/pScheduler and `iperf3` are installed.

## Runtime Model

The service exposes:

- `GET /api/health`
- `GET /api/metrics?source=...&destination=...&metric=...`
- `GET /docs`
- `GET /swagger.json`

Supported metrics:

- `latency`
- `packet_loss`
- `throughput`

Implementation details:

- `latency` and `packet_loss` use the pScheduler REST API.
- `throughput` uses direct `iperf3` JSON output.
- When deployed on `192.168.200.222`, throughput should use `IPERF3_RUNNER_HOST=localhost`.
- Throughput duration is intentionally short: `PT5S`.

## Environment Variables

Important variables:

```bash
PORT=5013
PSCHEDULER_API_URL=https://192.168.200.222/pscheduler
PSCHEDULER_VERIFY_TLS=false
PERFSONAR_NODES=192.168.200.222,iperf3.narit.or.th
PERFSONAR_TIMEOUT=90
PERFSONAR_THROUGHPUT_DURATION=PT5S
PERFSONAR_THROUGHPUT_MODE=iperf3_ssh
IPERF3_RUNNER_HOST=localhost
```

If running the API somewhere other than `192.168.200.222`, throughput requires SSH access to the runner host:

```bash
IPERF3_RUNNER_HOST=192.168.200.222
IPERF3_RUNNER_USER=<ssh-user>
IPERF3_SSH_KEY=/path/to/private/key
IPERF3_SSH_PORT=22
```

## Setup

From the project directory:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Run manually:

```bash
PORT=5013 ./.venv/bin/python perfsonar.py
```

Run with PM2:

```bash
pm2 start ecosystem.config.js
pm2 status
pm2 logs narit-perfsonar-api
```

After editing `ecosystem.config.js`, restart with updated env:

```bash
pm2 restart narit-perfsonar-api --update-env
```

## Test Commands

Health:

```bash
curl "http://localhost:5013/api/health"
```

Throughput:

```bash
curl "http://localhost:5013/api/metrics?source=192.168.200.222&destination=iperf3.narit.or.th&metric=throughput"
```

Latency:

```bash
curl "http://localhost:5013/api/metrics?source=192.168.200.222&destination=203.158.145.146&metric=latency"
```

Packet loss:

```bash
curl "http://localhost:5013/api/metrics?source=192.168.200.222&destination=203.158.145.146&metric=packet_loss"
```

Local syntax check:

```bash
./.venv/bin/python -m py_compile perfsonar.py
```

## Notes And Gotchas

- Do not reintroduce mock mode unless explicitly requested.
- Public perfSONAR nodes may allow `latency`/`packet_loss` but reject or fail `throughput`.
- `throughput` to ordinary iperf3 servers such as `iperf3.narit.or.th` should use direct `iperf3`, not pScheduler throughput.
- pScheduler throughput expects the destination to participate as a pScheduler node. If the destination is only an iperf3 server, pScheduler throughput will usually fail.
- Ubuntu 26.04 is not a good local target for installing `perfsonar-testpoint` because current perfSONAR packages depend on PostgreSQL 12-16, while Ubuntu 26.04 uses PostgreSQL 18.
- Keep throughput tests short and conservative. The current configured duration is `PT5S`.

