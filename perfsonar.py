from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_swagger_ui import get_swaggerui_blueprint
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

app = Flask(__name__)
# อนุญาตให้ทุกโดเมน (รวมถึงหน้าเว็บที่น้องๆ กำลังเขียน) ดึงข้อมูลได้
CORS(app) 

SWAGGER_URL = '/docs'
API_URL = '/swagger.json'

swagger_ui_blueprint = get_swaggerui_blueprint(
    SWAGGER_URL,
    API_URL,
    config={
        'app_name': 'perfSONAR API'
    }
)
app.register_blueprint(swagger_ui_blueprint, url_prefix=SWAGGER_URL)

# รายชื่อโหนดเริ่มต้นสำหรับทดสอบกับ perfSONAR/pScheduler จริง
DEFAULT_NODES = ['iperf3.narit.or.th', '192.168.16.92', '192.168.200.222']
NODES = [
    node.strip()
    for node in os.environ.get('PERFSONAR_NODES', ','.join(DEFAULT_NODES)).split(',')
    if node.strip()
]
METRIC_TYPES = ["throughput", "latency", "packet_loss"]
PERFSONAR_TIMEOUT = int(os.environ.get('PERFSONAR_TIMEOUT', '90'))
PSCHEDULER_API_URL = os.environ.get(
    'PSCHEDULER_API_URL',
    'https://192.168.200.222/pscheduler'
).rstrip('/')
PSCHEDULER_VERIFY_TLS = os.environ.get('PSCHEDULER_VERIFY_TLS', 'false').strip().lower() == 'true'
THROUGHPUT_DURATION = os.environ.get('PERFSONAR_THROUGHPUT_DURATION', 'PT5S')
THROUGHPUT_MAX_SECONDS = int(os.environ.get('PERFSONAR_THROUGHPUT_MAX_SECONDS', '10'))
THROUGHPUT_TOOL = os.environ.get('PERFSONAR_THROUGHPUT_TOOL', 'iperf3').strip()
THROUGHPUT_MODE = os.environ.get('PERFSONAR_THROUGHPUT_MODE', 'iperf3_ssh').strip().lower()
IPERF3_RUNNER_HOST = os.environ.get('IPERF3_RUNNER_HOST', '').strip()
IPERF3_RUNNER_USER = os.environ.get('IPERF3_RUNNER_USER', '').strip()
IPERF3_SSH_KEY = os.environ.get('IPERF3_SSH_KEY', '').strip()
IPERF3_SSH_PORT = os.environ.get('IPERF3_SSH_PORT', '22').strip()

try:
    HOST_MAP = json.loads(os.environ.get('PERFSONAR_HOST_MAP', '{}'))
except json.JSONDecodeError:
    HOST_MAP = {}


def resolve_host(node_name):
    return HOST_MAP.get(node_name, node_name)


def pscheduler_api_host():
    return urllib.parse.urlparse(PSCHEDULER_API_URL).hostname


def http_json(url, method='GET', payload=None):
    body = None
    headers = {}

    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    context = None if PSCHEDULER_VERIFY_TLS else ssl._create_unverified_context()
    request_object = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request_object, timeout=PERFSONAR_TIMEOUT, context=context) as response:
            response_body = response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        error_body = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f"pScheduler API returned HTTP {error.code}: {error_body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach pScheduler API: {error.reason}") from error

    if not response_body:
        return None

    return json.loads(response_body)


def walk_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_iso_duration_seconds(value):
    if not isinstance(value, str) or not value.startswith('PT') or not value.endswith('S'):
        return None

    try:
        return float(value[2:-1])
    except ValueError:
        return None


def parse_iso_duration_milliseconds(value):
    seconds = parse_iso_duration_seconds(value)
    if seconds is None:
        return None
    return round(seconds * 1000, 3)


def iso_duration_to_int_seconds(value, default=10):
    parsed = parse_iso_duration_seconds(value)
    if parsed is None:
        return default
    return max(1, int(round(parsed)))


def parse_throughput_duration(value):
    if value is None or str(value).strip() == "":
        return iso_duration_to_int_seconds(THROUGHPUT_DURATION, default=5)

    raw_value = str(value).strip()
    if raw_value.upper().startswith('PT'):
        seconds = iso_duration_to_int_seconds(raw_value, default=0)
    else:
        try:
            seconds = int(round(float(raw_value)))
        except ValueError as error:
            raise ValueError("duration must be a number of seconds or an ISO duration like PT10S") from error

    if seconds < 1:
        raise ValueError("duration must be at least 1 second")
    if seconds > THROUGHPUT_MAX_SECONDS:
        raise ValueError(f"duration must be {THROUGHPUT_MAX_SECONDS} seconds or less")
    return seconds


def human_bytes(num_bytes):
    units = ["Bytes", "KBytes", "MBytes", "GBytes", "TBytes"]
    value = float(num_bytes or 0)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            if unit == "Bytes":
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024


def human_bitrate(bits_per_second):
    units = ["bits/sec", "Kbits/sec", "Mbits/sec", "Gbits/sec", "Tbits/sec"]
    value = float(bits_per_second or 0)
    for unit in units:
        if abs(value) < 1000 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1000


def iperf3_summary(summary):
    if not isinstance(summary, dict):
        summary = {}
    return {
        "seconds": round(float(summary.get("seconds") or 0), 2),
        "bytes": summary.get("bytes"),
        "transfer": human_bytes(summary.get("bytes")),
        "bits_per_second": summary.get("bits_per_second"),
        "bitrate": human_bitrate(summary.get("bits_per_second")),
        "retransmits": summary.get("retransmits")
    }


def iperf3_intervals(raw_result):
    intervals = []
    for interval in raw_result.get("intervals", []):
        summary = interval.get("sum", {})
        intervals.append({
            "start": round(float(summary.get("start") or 0), 2),
            "end": round(float(summary.get("end") or 0), 2),
            "seconds": round(float(summary.get("seconds") or 0), 2),
            "bytes": summary.get("bytes"),
            "transfer": human_bytes(summary.get("bytes")),
            "bits_per_second": summary.get("bits_per_second"),
            "bitrate": human_bitrate(summary.get("bits_per_second")),
            "retransmits": summary.get("retransmits"),
            "cwnd": human_bytes(summary.get("snd_cwnd")) if summary.get("snd_cwnd") is not None else None
        })
    return intervals


def iperf3_text_output(raw_result, destination_host):
    start = raw_result.get("start", {})
    connection = (start.get("connected") or [{}])[0]
    end = raw_result.get("end", {})
    sender = iperf3_summary(end.get("sum_sent"))
    receiver = iperf3_summary(end.get("sum_received"))
    intervals = iperf3_intervals(raw_result)
    local_host = connection.get("local_host", "local")
    local_port = connection.get("local_port", "")
    remote_host = connection.get("remote_host", destination_host)
    remote_port = connection.get("remote_port", 5201)

    lines = [
        f"Connecting to host {destination_host}, port {remote_port}",
        f"[  5] local {local_host} port {local_port} connected to {remote_host} port {remote_port}",
        "[ ID] Interval           Transfer     Bitrate         Retr  Cwnd",
    ]

    for interval in intervals:
        retr = interval["retransmits"] if interval["retransmits"] is not None else ""
        cwnd = interval["cwnd"] or ""
        lines.append(
            f"[  5]   {interval['start']:.2f}-{interval['end']:.2f} sec  "
            f"{interval['transfer']:>11}  {interval['bitrate']:>14}  {retr:>4}  {cwnd}"
        )

    lines.extend([
        "- - - - - - - - - - - - - - - - - - - - - - - - -",
        "[ ID] Interval           Transfer     Bitrate         Retr",
        (
            f"[  5]   0.00-{sender['seconds']:.2f} sec  "
            f"{sender['transfer']:>11}  {sender['bitrate']:>14}  "
            f"{sender['retransmits'] if sender['retransmits'] is not None else '':>4}            sender"
        ),
        (
            f"[  5]   0.00-{receiver['seconds']:.2f} sec  "
            f"{receiver['transfer']:>11}  {receiver['bitrate']:>14}                  receiver"
        ),
        "",
        "iperf Done."
    ])
    return "\n".join(lines)


def find_numeric_value(payload, metric_type):
    merged_result = payload.get("result-merged", {}) if isinstance(payload, dict) else {}

    if metric_type == "latency":
        mean_seconds = parse_iso_duration_seconds(merged_result.get("mean"))
        if mean_seconds is not None:
            return round(mean_seconds * 1000, 2)

    if metric_type == "packet_loss" and isinstance(merged_result.get("loss"), (int, float)):
        return round(float(merged_result["loss"]), 2)

    candidates = {
        "throughput": ["throughput", "bits_per_second", "bps", "bandwidth", "mbps", "gbps"],
        "latency": ["latency", "rtt", "mean", "average", "median", "minimum"],
        "packet_loss": ["packet_loss", "packet-loss", "loss", "lost_percent", "loss_percent"],
    }[metric_type]

    for key, value in walk_json(payload):
        key_lower = str(key).lower()
        if any(candidate in key_lower for candidate in candidates) and isinstance(value, (int, float)):
            if metric_type == "throughput" and key_lower in ["bits_per_second", "bps"]:
                return round(value / 1_000_000_000, 2)
            return round(float(value), 2)

    return None


def first_present(payload, keys):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def duration_field_milliseconds(payload, keys):
    value = first_present(payload, keys)
    milliseconds = parse_iso_duration_milliseconds(value)
    if milliseconds is not None:
        return milliseconds
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    return None


def compact_pscheduler_run(raw_result, task_url):
    return {
        "task_url": task_url,
        "run_url": f"{task_url}/runs/first",
        "state": raw_result.get("state"),
        "state_display": raw_result.get("state-display"),
        "start_time": raw_result.get("start-time") or raw_result.get("start"),
        "end_time": raw_result.get("end-time") or raw_result.get("end"),
        "duration": raw_result.get("duration"),
        "errors": raw_result.get("errors"),
        "participant": raw_result.get("participant"),
        "participants": raw_result.get("participants"),
        "tool": raw_result.get("tool")
    }


def rtt_measurement_details(raw_result, metric_type):
    merged_result = raw_result.get("result-merged", {}) if isinstance(raw_result, dict) else {}
    result_full = raw_result.get("result-full") if isinstance(raw_result, dict) else None
    loss = first_present(merged_result, ["loss", "loss-percent", "loss_percent", "lost_percent"])

    details = {
        "test_type": "rtt",
        "unit": "ms" if metric_type == "latency" else "percent",
        "loss_percent": round(float(loss), 3) if isinstance(loss, (int, float)) else None,
        "latency_ms": {
            "mean": duration_field_milliseconds(merged_result, ["mean", "average", "avg"]),
            "minimum": duration_field_milliseconds(merged_result, ["minimum", "min"]),
            "maximum": duration_field_milliseconds(merged_result, ["maximum", "max"]),
            "median": duration_field_milliseconds(merged_result, ["median"]),
            "standard_deviation": duration_field_milliseconds(merged_result, ["stddev", "standard-deviation"])
        },
        "ttl": first_present(merged_result, ["ttl", "hops"]),
        "sent": first_present(merged_result, ["sent", "packets-sent", "packets_sent"]),
        "received": first_present(merged_result, ["received", "packets-received", "packets_received"]),
        "lost": first_present(merged_result, ["lost", "packets-lost", "packets_lost"]),
        "succeeded": first_present(merged_result, ["succeeded", "success"]),
        "failed": first_present(merged_result, ["failed"]),
        "diagnostics": first_present(merged_result, ["diags", "diagnostics"]),
        "errors": first_present(merged_result, ["errors"]),
        "raw_result_merged": merged_result
    }

    if result_full is not None:
        details["raw_result_full"] = result_full

    return details


def iperf3_command(source_host, destination_host, duration_seconds):
    duration_seconds = str(duration_seconds)
    iperf3_args = ["iperf3", "-J", "-c", destination_host, "-t", duration_seconds]
    runner_host = IPERF3_RUNNER_HOST or source_host

    if runner_host in ["localhost", "127.0.0.1", "::1", pscheduler_api_host()]:
        return iperf3_args

    ssh_target = f"{IPERF3_RUNNER_USER}@{runner_host}" if IPERF3_RUNNER_USER else runner_host
    command = [
        "ssh",
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={min(PERFSONAR_TIMEOUT, 10)}",
        "-p", IPERF3_SSH_PORT,
    ]

    if IPERF3_SSH_KEY:
        command.extend(["-i", IPERF3_SSH_KEY])

    command.extend([
        ssh_target,
        *iperf3_args
    ])
    return command


def get_iperf3_metric(source, destination, duration_seconds):
    source_host = resolve_host(source)
    destination_host = resolve_host(destination)
    completed = subprocess.run(
        iperf3_command(source_host, destination_host, duration_seconds),
        capture_output=True,
        text=True,
        timeout=PERFSONAR_TIMEOUT,
        check=False
    )

    if completed.returncode != 0:
        error_message = completed.stderr.strip() or completed.stdout.strip() or "iperf3 failed"
        raise RuntimeError(error_message)

    try:
        raw_result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("iperf3 did not return valid JSON.") from error

    end_result = raw_result.get("end", {})
    summary = end_result.get("sum_received") or end_result.get("sum_sent") or {}
    bits_per_second = summary.get("bits_per_second")
    if not isinstance(bits_per_second, (int, float)):
        raise RuntimeError("Could not find bits_per_second in iperf3 result.")

    timestamp = int(time.time())
    return [{
        "timestamp": timestamp,
        "time_label": time.strftime('%H:%M', time.localtime(timestamp)),
        "value": round(bits_per_second / 1_000_000_000, 2),
        "source_host": source_host,
        "destination_host": destination_host,
        "runner": IPERF3_RUNNER_HOST or source_host,
        "tool": "iperf3",
        "duration_seconds": duration_seconds,
        "command": f"iperf3 -c {destination_host} -t {duration_seconds}",
        "sender": iperf3_summary(end_result.get("sum_sent")),
        "receiver": iperf3_summary(end_result.get("sum_received")),
        "intervals": iperf3_intervals(raw_result),
        "iperf3_text": iperf3_text_output(raw_result, destination_host)
    }]


def pscheduler_test_spec(metric_type, destination_host):
    if metric_type == "throughput":
        return {
            "type": "throughput",
            "spec": {
                "schema": 1,
                "dest": destination_host,
                "duration": THROUGHPUT_DURATION
            }
        }
    if metric_type in ["latency", "packet_loss"]:
        return {
            "type": "rtt",
            "spec": {
                "schema": 1,
                "dest": destination_host
            }
        }
    raise ValueError("Invalid metric type")


def create_pscheduler_task(metric_type, destination_host):
    task = {
        "schema": 1,
        "test": pscheduler_test_spec(metric_type, destination_host),
        "schedule": {},
        "archives": [
            {
                "archiver": "bitbucket",
                "data": {},
                "ttl": "PT1M",
                "transform": {"script": "."}
            }
        ]
    }

    if metric_type == "throughput" and THROUGHPUT_TOOL:
        task["tool"] = THROUGHPUT_TOOL

    return http_json(f"{PSCHEDULER_API_URL}/tasks", method='POST', payload=task)


def wait_for_first_run(task_url):
    deadline = time.time() + PERFSONAR_TIMEOUT
    run_url = f"{task_url}/runs/first"

    while time.time() < deadline:
        try:
            run = http_json(run_url)
        except RuntimeError as error:
            if "HTTP 404" not in str(error):
                raise
            time.sleep(1)
            continue

        if run.get("state") == "finished":
            return run
        if run.get("state") in ["failed", "nonstart", "non-starter", "missed"]:
            raise RuntimeError(f"pScheduler run failed: {run.get('errors') or run.get('state-display')}")

        time.sleep(1)

    raise RuntimeError("Timed out waiting for pScheduler run to finish.")


def get_real_metric(source, destination, metric_type, throughput_duration_seconds=None):
    if metric_type == "throughput" and THROUGHPUT_MODE == "iperf3_ssh":
        duration_seconds = throughput_duration_seconds or parse_throughput_duration(None)
        return get_iperf3_metric(source, destination, duration_seconds)

    destination_host = resolve_host(destination)
    task_url = create_pscheduler_task(metric_type, destination_host)
    raw_result = wait_for_first_run(task_url)

    metric_value = find_numeric_value(raw_result, metric_type)
    if metric_value is None:
        raise RuntimeError("Could not find a numeric metric value in pScheduler result.")

    timestamp = int(time.time())
    pscheduler_details = compact_pscheduler_run(raw_result, task_url)
    measurement_details = rtt_measurement_details(raw_result, metric_type)
    return [{
        "timestamp": timestamp,
        "time_label": time.strftime('%H:%M', time.localtime(timestamp)),
        "value": metric_value,
        "unit": "ms" if metric_type == "latency" else "percent",
        "source_host": resolve_host(source),
        "destination_host": destination_host,
        "task_url": task_url,
        "runner_url": PSCHEDULER_API_URL,
        "pscheduler": pscheduler_details,
        "measurement": measurement_details
    }]

@app.route('/swagger.json', methods=['GET'])
def swagger_json():
    return jsonify({
        "openapi": "3.0.3",
        "info": {
            "title": "perfSONAR API",
            "version": "1.0.0",
            "description": "API สำหรับทดสอบ perfSONAR metrics จริง เช่น throughput, latency และ packet loss ผ่าน pScheduler"
        },
        "servers": [
            {
                "url": "/",
                "description": "Current server"
            }
        ],
        "paths": {
            "/api/metrics": {
                "get": {
                    "summary": "Get perfSONAR metrics",
                    "description": "ดึงข้อมูล metric จริงระหว่าง source และ destination ผ่าน pScheduler",
                    "parameters": [
                        {
                            "name": "source",
                            "in": "query",
                            "description": "ต้นทางของการวัด",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "default": DEFAULT_NODES[0],
                                "enum": NODES
                            }
                        },
                        {
                            "name": "destination",
                            "in": "query",
                            "description": "ปลายทางของการวัด",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "default": DEFAULT_NODES[1],
                                "enum": NODES
                            }
                        },
                        {
                            "name": "metric",
                            "in": "query",
                            "description": "ประเภท metric ที่ต้องการดู",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "default": "throughput",
                                "enum": METRIC_TYPES
                            }
                        },
                        {
                            "name": "duration",
                            "in": "query",
                            "description": "จำนวนวินาทีสำหรับ throughput เท่านั้น เช่น 10 จะเท่ากับ iperf3 -t 10",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": THROUGHPUT_MAX_SECONDS,
                                "default": iso_duration_to_int_seconds(THROUGHPUT_DURATION, default=5)
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Metric history response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/MetricsResponse"
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Invalid request",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ErrorResponse"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "MetricPoint": {
                    "type": "object",
                    "properties": {
                        "timestamp": {
                            "type": "integer",
                            "example": 1717477200
                        },
                        "time_label": {
                            "type": "string",
                            "example": "13:00"
                        },
                        "value": {
                            "type": "number",
                            "format": "float",
                            "example": 8.42
                        },
                        "unit": {
                            "type": "string",
                            "example": "ms"
                        },
                        "pscheduler": {
                            "type": "object",
                            "description": "ข้อมูลสถานะ run จาก pScheduler เช่น task URL, run URL, state, time, errors และ participants"
                        },
                        "measurement": {
                            "type": "object",
                            "description": "รายละเอียดผลวัดที่ parse ได้จาก result-merged/result-full ของ pScheduler"
                        }
                    }
                },
                "MetricsResponse": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "example": "success"
                        },
                        "source": {
                            "type": "string",
                            "example": DEFAULT_NODES[0]
                        },
                        "destination": {
                            "type": "string",
                            "example": DEFAULT_NODES[1]
                        },
                        "metric": {
                            "type": "string",
                            "example": "throughput"
                        },
                        "data": {
                            "type": "array",
                            "items": {
                                "$ref": "#/components/schemas/MetricPoint"
                            }
                        }
                    }
                },
                "ErrorResponse": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "string",
                            "example": "Source and Destination cannot be the same"
                        }
                    }
                }
            }
        }
    })

@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    # รับค่าพารามิเตอร์จาก Frontend (ถ้าไม่ส่งมาจะใช้ค่าเริ่มต้น)
    source = request.args.get('source', DEFAULT_NODES[0])
    destination = request.args.get('destination', DEFAULT_NODES[1])
    metric_type = request.args.get('metric', 'throughput')
    duration = request.args.get('duration')

    if metric_type not in METRIC_TYPES:
        return jsonify({"error": "Invalid metric type"}), 400

    # ป้องกันการดึงข้อมูลโหนดเดียวกัน (Source และ Destination เดียวกัน)
    if source == destination:
        return jsonify({"error": "Source and Destination cannot be the same"}), 400

    try:
        throughput_duration_seconds = None
        if metric_type == "throughput":
            throughput_duration_seconds = parse_throughput_duration(duration)
        history_data = get_real_metric(source, destination, metric_type, throughput_duration_seconds)
    except (RuntimeError, TimeoutError, ValueError) as error:
        return jsonify({
            "error": str(error),
            "source": source,
            "destination": destination,
            "metric": metric_type
        }), 502

    # จัดรูปแบบ JSON สำหรับส่งกลับไปให้ Frontend
    response = {
        "status": "success",
        "source": source,
        "destination": destination,
        "metric": metric_type,
        "data": history_data
    }
    
    return jsonify(response)


@app.route('/api/health', methods=['GET'])
def health():
    pscheduler_api_available = False
    pscheduler_api_message = None

    try:
        pscheduler_api_message = http_json(f"{PSCHEDULER_API_URL}/")
        pscheduler_api_available = True
    except RuntimeError as error:
        pscheduler_api_message = str(error)

    return jsonify({
        "status": "ok",
        "pscheduler_api_url": PSCHEDULER_API_URL,
        "pscheduler_api_available": pscheduler_api_available,
        "pscheduler_api_message": pscheduler_api_message,
        "throughput_mode": THROUGHPUT_MODE,
        "throughput_tool": "iperf3" if THROUGHPUT_MODE == "iperf3_ssh" else THROUGHPUT_TOOL,
        "iperf3_runner_host": IPERF3_RUNNER_HOST or "source",
        "iperf3_runner_user": IPERF3_RUNNER_USER or "current SSH user",
        "iperf3_ssh_key_configured": bool(IPERF3_SSH_KEY),
        "iperf3_ssh_port": IPERF3_SSH_PORT,
        "nodes": NODES
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5013))
    print(f"🚀 Starting perfSONAR API on http://localhost:{port}")
    print(f"Swagger UI: http://localhost:{port}/docs")
    print(f"Test URL: http://localhost:{port}/api/metrics?source={DEFAULT_NODES[0]}&destination={DEFAULT_NODES[1]}&metric=throughput")
    app.run(debug=True, host='0.0.0.0', port=port)
