"""
Mock test for download_smartds.list_prefix — XML ayrıştırma + sayfalama.

Bu ortamdan OEDI'ye ağ erişimi YOK, dolayısıyla canlı uç test edilemez.
Bu test, S3'ün gerçek REST yanıt biçimini taklit eden sahte yanıtlarla
ayrıştırıcının ve continuation-token sayfalamasının doğru çalıştığını
doğrular. Canlı doğrulama için:  python3 download_smartds.py explore
"""
import os, sys
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import os
import sys


from download_smartds import list_prefix

PAGE1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>oedi-data-lake</Name>
  <Prefix>SMART-DS/v1.0/2017/SFO/P27U/</Prefix>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>TOKEN123</NextContinuationToken>
  <CommonPrefixes><Prefix>SMART-DS/v1.0/2017/SFO/P27U/scenarios/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>SMART-DS/v1.0/2017/SFO/P27U/profiles/</Prefix></CommonPrefixes>
  <Contents>
    <Key>SMART-DS/v1.0/2017/SFO/P27U/metrics.csv</Key>
    <Size>20480</Size>
  </Contents>
  <Contents>
    <Key>SMART-DS/v1.0/2017/SFO/P27U/</Key>
    <Size>0</Size>
  </Contents>
</ListBucketResult>"""

PAGE2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>oedi-data-lake</Name>
  <IsTruncated>false</IsTruncated>
  <CommonPrefixes><Prefix>SMART-DS/v1.0/2017/SFO/P27U/analysis/</Prefix></CommonPrefixes>
  <Contents>
    <Key>SMART-DS/v1.0/2017/SFO/P27U/Master.dss</Key>
    <Size>4096</Size>
  </Contents>
</ListBucketResult>"""


def main():
    calls = []

    def fake_get(url):
        calls.append(url)
        return PAGE2 if "continuation-token" in url else PAGE1

    folders, files = list_prefix("SMART-DS/v1.0/2017/SFO/P27U/", opener=fake_get)

    print("=" * 66)
    print("download_smartds.list_prefix — mock test (ağ yok)")
    print("=" * 66)
    print(f"  istek sayısı : {len(calls)}  (sayfalama çalıştıysa 2 olmalı)")
    print(f"  klasörler    : {[f.rstrip('/').split('/')[-1] for f in folders]}")
    print(f"  dosyalar     : {[k.split('/')[-1] for k, _ in files]}")

    checks = {
        "sayfalama (2 istek)": len(calls) == 2,
        "3 klasör bulundu": len(folders) == 3,
        "profiles klasörü görüldü": any(f.endswith("/profiles/") for f in folders),
        "2 dosya (klasör anahtarı elendi)": len(files) == 2,
        "boyut ayrıştırıldı": any(s == 4096 for _, s in files),
        "prefix URL-kodlandı": "SMART-DS%2Fv1.0" in calls[0],
        "token 2. istekte geçti": "TOKEN123" in calls[1],
    }
    print("-" * 66)
    ok = True
    for name, passed in checks.items():
        print(f"  [{'OK ' if passed else 'FAIL'}] {name}")
        ok &= passed
    print("-" * 66)
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    print("  NOT: canlı OEDI ucu bu ortamdan test EDİLEMEDİ.")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
