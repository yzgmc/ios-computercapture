"""生成本地自签名 HTTPS 证书。

iOS Safari 要求 getUserMedia 必须在 HTTPS（或 localhost）上下文中运行。
运行本脚本生成 cert.pem / key.pem，然后在 iPhone 上安装并信任 cert.pem，
最后以 HTTPS 模式启动信令服务器。

如果通过 IP 地址访问（如 https://192.168.1.5:8080），请把该 IP 也加入证书，
否则 iOS Safari 仍会提示“连接不安全”。
"""
import argparse
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def get_default_local_ip():
    """获取本机默认局域网 IP（用于访问 iPhone 等设备）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            # 连接一个公网地址，不会真的发送数据
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def generate_self_signed_cert(
    cert_path: str,
    key_path: str,
    hostnames=None,
    ip_addresses=None,
):
    if hostnames is None:
        hostnames = ["phonecam.local", "localhost"]
    if ip_addresses is None:
        ip_addresses = ["127.0.0.1"]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PhoneCam"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
    ])

    san_entries = []
    for h in hostnames:
        san_entries.append(x509.DNSName(h))
    for ip in ip_addresses:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Generated {cert_path} and {key_path}")
    print(f"  Hostnames: {', '.join(hostnames)}")
    print(f"  IP addresses: {', '.join(ip_addresses)}")


def main():
    parser = argparse.ArgumentParser(description="Generate self-signed HTTPS cert for PhoneCam")
    parser.add_argument("--host", action="append", default=None,
                        help="添加一个域名到证书（可多次使用，默认 phonecam.local, localhost）")
    parser.add_argument("--ip", action="append", default=None,
                        help="添加一个 IP 到证书（可多次使用，默认 127.0.0.1）")
    parser.add_argument("--no-default-ip", action="store_true",
                        help="不自动检测本机局域网 IP")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(base_dir, "cert.pem")
    key_path = os.path.join(base_dir, "key.pem")

    hostnames = args.host if args.host else ["phonecam.local", "localhost"]
    ip_addresses = args.ip if args.ip else ["127.0.0.1"]

    if not args.no_default_ip:
        local_ip = get_default_local_ip()
        if local_ip and local_ip not in ip_addresses:
            ip_addresses.append(local_ip)

    generate_self_signed_cert(cert_path, key_path, hostnames, ip_addresses)
    print("\niPhone 安装证书后，还需要到：")
    print("  设置 → 通用 → 关于本机 → 证书信任设置 → 启用 PhoneCam 完全信任")


if __name__ == "__main__":
    main()
