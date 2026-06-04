module.exports = {
  apps: [
    {
      name: "narit-perfsonar-api",
      script: "./perfsonar.py",
      interpreter: "./.venv/bin/python",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "300M",
      env: {
        PORT: "5013",
        PSCHEDULER_API_URL: "https://192.168.200.222/pscheduler",
        PSCHEDULER_VERIFY_TLS: "false",
        PERFSONAR_NODES: "192.168.200.222,iperf3.narit.or.th",
        PERFSONAR_TIMEOUT: "90",
        PERFSONAR_THROUGHPUT_DURATION: "PT5S",
        PERFSONAR_THROUGHPUT_MODE: "iperf3_ssh",
        IPERF3_RUNNER_HOST: "localhost"
      }
    }
  ]
};
