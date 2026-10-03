"""网页模式 HTTPS 自签证书管理。

浏览器的 getUserMedia 只在"安全上下文"下可用，局域网内访问 http://192.168.x.x
不算安全上下文，所以网页端必须走 HTTPS。局域网没有公共 CA 可签发，只能自签：

1. 首次启动时生成一张自签证书（RSA 2048 / SHA-256，含 serverAuth 扩展用途）
2. SAN 里写入本机所有局域网 IPv4 + localhost，手机用 IP 访问时才能匹配
3. 证书缓存在本地目录复用，避免每次启动都要重新信任
4. 本机 IP 变化后旧证书的 SAN 不再匹配，自动重新生成

手机首次访问时浏览器会提示"证书不受信任"，需要手动确认继续访问
（iOS Safari 点"访问此网站"，Chrome 点"高级 → 继续前往"）。这是自签证书的
固有代价，局域网场景下无法避免。
"""
from __future__ import annotations

import datetime
import logging
import os
import socket
import subprocess
import sys

logger = logging.getLogger(__name__)

# 证书缓存目录（相对 desktop/）
CERT_DIRNAME = ".phonecam"
CERT_FILENAME = "web-cert.pem"
KEY_FILENAME = "web-key.pem"
# 有效期天数。iOS 13+ 拒绝有效期超过 825 天的 TLS 证书，取 365 天安全值。
VALIDITY_DAYS = 365
COMMON_NAME = "PhoneCam Local"


class CertError(RuntimeError):
    """证书生成/加载失败。"""


def get_local_ips() -> list[str]:
    """枚举本机所有局域网 IPv4 地址（含 127.0.0.1）。

    用 getaddrinfo(hostname) 拿到多网卡地址，再补一个 UDP connect 拿默认出口 IP
    （getaddrinfo 在某些情况下只返回单一地址）。
    """
    ips: set[str] = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception as e:
        logger.debug("getaddrinfo for local IPs failed: %s", e)
    # 默认出口 IP（不发包，只查路由表）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(("10.254.254.254", 1))
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    return sorted(ips)


def _default_cert_dir() -> str:
    """默认证书目录：desktop/.phonecam/

    打包运行时（PyInstaller）源码在临时目录 _MEIPASS，每次启动都会解压出
    一份新的空目录。若把证书存在那里，手机每次都要重新信任，所以打包后
    改存到用户数据目录。
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            return os.path.join(base, "PhoneCam", CERT_DIRNAME)
        if sys.platform == "darwin":
            return os.path.join(os.path.expanduser("~"), "Library",
                                "Application Support", "PhoneCam", CERT_DIRNAME)
        return os.path.join(os.path.expanduser("~"), ".config",
                            "phonecam", CERT_DIRNAME)

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, CERT_DIRNAME)


def _load_san_ips(cert_path: str) -> set[str] | None:
    """读取已有证书 SAN 中的 IP 列表；无法解析（无 cryptography）时返回 None。"""
    try:
        from cryptography import x509
    except ImportError:
        return None
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        return set(str(v) for v in san.value.get_values_for_type(x509.IPAddress))
    except Exception as e:
        logger.debug("Failed to read SAN from existing cert: %s", e)
        return None


def _is_expiring(cert_path: str) -> bool:
    """证书是否已过期或即将（7 天内）过期。无 cryptography 时保守返回 True。"""
    try:
        from cryptography import x509
    except ImportError:
        return True
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        # cryptography 37+ 用 *_utc 属性
        try:
            not_after = cert.not_valid_after_utc
        except AttributeError:  # pragma: no cover - 旧版本
            not_after = cert.not_valid_after
        return not_after < datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    except Exception as e:
        logger.debug("Failed to read validity from existing cert: %s", e)
        return True


def _generate_with_cryptography(cert_path: str, key_path: str, ips: list[str]) -> None:
    """用 cryptography 库生成自签证书（首选路径）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PhoneCam"),
        x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME),
    ])

    san_entries: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for ip in ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))     # 容忍时钟偏差
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    # 私钥只给当前用户读权限（Windows 上 chmod 效果有限，但聊胜于无）
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass


def _generate_with_openssl(cert_path: str, key_path: str, ips: list[str]) -> None:
    """回退路径：调用系统 openssl 生成自签证书。"""
    san = ",".join(["DNS:localhost"] + [f"IP:{ip}" for ip in ips])
    # openssl req 无法直接写 SAN 到临时配置文件之外，用 -addext（1.1.1+ 支持）
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-nodes", "-keyout", key_path, "-out", cert_path,
        "-days", str(VALIDITY_DAYS),
        "-subj", f"/CN={COMMON_NAME}/O=PhoneCam/C=CN",
        "-addext", f"subjectAltName={san}",
        "-addext", "extendedKeyUsage=serverAuth",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise CertError(
            "未找到 openssl，也无法使用 cryptography。"
            "请安装依赖：pip install cryptography")
    except subprocess.CalledProcessError as e:
        raise CertError(f"openssl 生成证书失败: {e.stderr.decode('utf-8', 'replace')}")


def ensure_cert(cert_dir: str | None = None) -> tuple[str, str]:
    """确保证书存在且可用，返回 (cert_path, key_path)。

    复用条件：文件存在 + 未过期 + SAN 覆盖当前所有本机 IP。
    任一条件不满足就重新生成（重新生成后手机需要再次确认信任）。
    """
    cert_dir = cert_dir or _default_cert_dir()
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, CERT_FILENAME)
    key_path = os.path.join(cert_dir, KEY_FILENAME)

    ips = get_local_ips()

    if os.path.exists(cert_path) and os.path.exists(key_path):
        if _is_expiring(cert_path):
            logger.info("Web cert expired or expiring soon, regenerating")
        else:
            san_ips = _load_san_ips(cert_path)
            if san_ips is None:
                logger.info("Cannot verify cert SAN, reusing existing cert")
                return cert_path, key_path
            missing = set(ips) - san_ips
            if not missing:
                return cert_path, key_path
            logger.info("Local IP changed (missing SAN: %s), regenerating cert",
                        sorted(missing))

    try:
        _generate_with_cryptography(cert_path, key_path, ips)
        logger.info("Generated self-signed cert via cryptography: %s", cert_path)
    except ImportError:
        logger.info("cryptography not installed, falling back to openssl")
        _generate_with_openssl(cert_path, key_path, ips)
        logger.info("Generated self-signed cert via openssl: %s", cert_path)

    return cert_path, key_path


def fingerprint(cert_path: str) -> str | None:
    """返回证书 SHA-256 指纹（冒号分隔），供用户核对，防中间人。

    自签证书本来就没有 CA 背书，指纹是唯一能让用户确认"连的确实是本机"的手段。
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        digest = cert.fingerprint(hashes.SHA256())
        return ":".join(f"{b:02X}" for b in digest)
    except Exception:
        return None
