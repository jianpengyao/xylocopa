# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's private vulnerability reporting instead: [Report a vulnerability](https://github.com/jianpengyao/xylocopa/security/advisories/new) (repository **Security** tab → **Report a vulnerability**). Include reproduction steps and the version you tested (`git describe --tags` or the version shown in Monitor). You will get a response within a week.

## Deployment model

Xylocopa is designed to run on a private network — LAN or a VPN such as Tailscale, as described in the README's [Remote access](README.md#remote-access) section. Reports that assume the orchestrator is exposed directly to the public internet are still welcome, but hardening for that deployment model is not a current goal.

## Supported versions

Only the latest release receives security fixes.
