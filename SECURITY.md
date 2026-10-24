# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.1.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

If you discover a potential security vulnerability within **DriftMatrix**, please do **not** open a public issue.

Instead, please report vulnerabilities directly to the maintainer via GitHub Security Advisories or by emailing [dfidel@cpp.edu](mailto:dfidel@cpp.edu).

### Best Practices for DriftMatrix Users
* **Credentials & Wi-Fi Keys:** Always use environment variables or `.local.json` configuration files for API keys, weather tokens, and calendar feeds.
* **Network Security:** DriftMatrix communicates over your local area network (LAN) via HTTP. Do not expose the panel's HTTP REST endpoints directly to the public internet without an authenticated reverse proxy.
