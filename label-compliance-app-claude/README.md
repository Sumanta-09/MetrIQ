---
title: MetrIQ
emoji: 🏷️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# MetrIQ — Legal Metrology Label Scanner

Open-source label compliance checker built on the Legal Metrology
(Packaged Commodities) Rules, 2011. Deployed here via the HF Spaces
Docker SDK.

Set the `ADMIN_PASSCODE` secret in this Space's Settings to protect
`/admin` (defaults to `labelcheck` if unset — change it before sharing
the Space publicly).
