"""共享测试夹具。"""
from __future__ import annotations

VALID_WG = """[Interface]
PrivateKey = aGVsbG8ta2V5LWJhc2U2NA==
Address = 10.7.0.2/32, fd00::2/128
DNS = 1.1.1.1, 8.8.8.8
MTU = 1420

[Peer]
PublicKey = d29ybGQtcHVibGljLWtleQ==
PresharedKey = cHJlc2hhcmVkLWtleQ==
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:51820
PersistentKeepalive = 25
"""
