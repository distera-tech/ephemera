"""Outbound TCP check for real mode.

Brev instances are usually reached over SSH on a high, provider-assigned port (e.g.
216.81.248.28:44689). Some networks (corporate/school Wi-Fi, VPNs, strict routers)
only allow ports like 80/443, which makes every SSH attempt time out. portquiz.net
answers on every TCP port, so it tells the two cases apart without creating a GPU.

    make net-check                      # from the worker container
    make net-check TARGET=1.2.3.4:44689 # also test a specific endpoint
    python3 scripts/net_check.py        # from the host
"""

import socket
import sys

targets = ["portquiz.net:443", "portquiz.net:22", "portquiz.net:44689", "portquiz.net:30022"]
targets += sys.argv[1:]
blocked = 0
for target in targets:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=10).close()
        print(f"  OK       {target}")
    except OSError as exc:
        blocked += 1
        print(f"  BLOCKED  {target}  ({exc})")
if blocked:
    print(
        "\nSome outbound ports are blocked. If portquiz.net:443 is OK but the high ports are"
        "\nBLOCKED, this network filters outbound traffic: SSH to Brev instances cannot work"
        "\nfrom here. Try another network (e.g. a phone hotspot) or disable the VPN/firewall."
    )
else:
    print("\nOutbound TCP looks fine.")
