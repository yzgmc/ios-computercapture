"""解析 .mobileprovision 文件，提取 Team ID、Bundle ID、证书类型等信息。"""
import plistlib
import sys

from cryptography.hazmat.primitives import serialization


def parse_mobileprovision(path: str):
    with open(path, "rb") as f:
        data = f.read()

    # mobileprovision 是 PKCS#7/CMS 签名数据，中间夹着 plist
    start_marker = b"<?xml version"
    end_marker = b"</plist>"
    start = data.find(start_marker)
    end = data.find(end_marker)
    if start == -1 or end == -1:
        raise ValueError("无法找到 plist 内容")

    plist_data = data[start:end + len(end_marker)]
    provision = plistlib.loads(plist_data)

    print("Name:", provision.get("Name"))
    print("App ID Name:", provision.get("AppIDName"))
    print("Team ID:", provision.get("TeamIdentifier", [None])[0])
    print("Team Name:", provision.get("TeamName"))
    print("UUID:", provision.get("UUID"))
    print("Creation Date:", provision.get("CreationDate"))
    print("Expiration Date:", provision.get("ExpirationDate"))
    print("Provisioned Devices:", len(provision.get("ProvisionedDevices", [])))

    app_id = provision.get("ApplicationIdentifierPrefix", [""])[0] + "."
    bundle_id = provision.get("Entitlements", {}).get("application-identifier", "").replace(app_id, "", 1)
    print("Bundle ID:", bundle_id)

    # 判断类型
    if provision.get("ProvisionsAllDevices"):
        print("Type: Enterprise / In-House")
    elif "ProvisionedDevices" in provision:
        print("Type: Ad Hoc / Development")
    else:
        print("Type: App Store")


if __name__ == "__main__":
    parse_mobileprovision(sys.argv[1])
