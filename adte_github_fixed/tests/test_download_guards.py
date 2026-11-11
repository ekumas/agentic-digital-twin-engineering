"""
smartds_download güvenlik korumaları testi.

Kullanıcının karşılaştığı gerçek hatayı yeniden üretir:
kök seviyede '[BU KLASÖRÜ İNDİR]' seçilince tüm veri seti listelenmeye
çalışılıyor ve bağlantı zaman aşımına uğruyordu.

Doğrulananlar:
  A) çok büyük klasör -> listeleme sınırda kesilir, indirme REDDEDİLİR
  B) geçici zaman aşımı -> yeniden denenir ve başarılı olur
  C) sığ seviyelerde (yıl/bölge/alt-bölge) indirme seçeneği HİÇ sunulmaz
"""
import os
import sys
import io
import contextlib
import tempfile
import shutil

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import smartds_download as sd


def _xml(dosyalar, klasorler=(), kesik=False, jeton="T"):
    p = ["<?xml version='1.0' encoding='UTF-8'?>",
         "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>",
         f"<IsTruncated>{'true' if kesik else 'false'}</IsTruncated>"]
    if kesik:
        p.append(f"<NextContinuationToken>{jeton}</NextContinuationToken>")
    for f in klasorler:
        p.append(f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>")
    for k, s in dosyalar:
        p.append(f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>")
    p.append("</ListBucketResult>")
    return "".join(p).encode()


# --------------------------------------------- A) devasa klasör reddedilmeli
def test_devasa_klasor():
    sayac = {"n": 0}

    def devasa(url):
        """Her sayfada 1000 dosya, hiç bitmiyor — tüm veri setini taklit eder."""
        sayac["n"] += 1
        i = sayac["n"]
        return _xml([(f"SMART-DS/v1.0/y/{i}/{j}.dss", 1024)
                     for j in range(1000)], kesik=True)

    hedef = tempfile.mkdtemp(prefix="sd_gd_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            inen = sd.klasor_indir("SMART-DS/v1.0/", hedef, acici=devasa)
        cikti = buf.getvalue()
        return {
            "devasa klasör indirilmedi": inen == 0,
            "kullanıcıya 'çok büyük' uyarısı verildi":
                "ÇOK BÜYÜK" in cikti,
            "sayfa sınırında durdu (sonsuz döngü yok)":
                sayac["n"] <= 30,
            "ne yapması gerektiği söylendi":
                "ALTMERKEZ" in cikti or "altmerkez" in cikti.lower(),
        }
    finally:
        shutil.rmtree(hedef, ignore_errors=True)


# ----------------------------------------- B) zaman aşımı yeniden denenmeli
def test_yeniden_deneme():
    durum = {"n": 0}
    orij_sleep = sd.time.sleep
    sd.time.sleep = lambda s: None  # testi hızlandır

    def bazen_zaman_asimi(url, zaman_asimi=90, deneme=3):
        return sd.indir_bayt.__wrapped__(url) if False else None

    def sahte_urlopen(istek, timeout=None):
        durum["n"] += 1
        if durum["n"] == 1:
            raise TimeoutError("The read operation timed out")

        class R:
            def read(self_inner):
                return b"OK"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()

    orij = sd.urllib.request.urlopen
    sd.urllib.request.urlopen = sahte_urlopen
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sonuc = sd.indir_bayt("https://x/y")
        return {
            "zaman aşımından sonra yeniden denedi": durum["n"] == 2,
            "ikinci denemede başarılı oldu": sonuc == b"OK",
            "kullanıcı bilgilendirildi":
                "yeniden" in buf.getvalue().lower(),
        }
    finally:
        sd.urllib.request.urlopen = orij
        sd.time.sleep = orij_sleep


# ------------------------------- C) sığ seviyede indirme seçeneği olmamalı
def test_sig_seviye():
    AGAC_KLASOR = {
        "SMART-DS/v1.0/": ["SMART-DS/v1.0/2017/", "SMART-DS/v1.0/2018/"],
        "SMART-DS/v1.0/2017/": ["SMART-DS/v1.0/2017/SFO/"],
        "SMART-DS/v1.0/2017/SFO/": ["SMART-DS/v1.0/2017/SFO/P27U/"],
        "SMART-DS/v1.0/2017/SFO/P27U/": ["SMART-DS/v1.0/2017/SFO/P27U/scenarios/"],
    }

    def acici(url):
        import urllib.parse as up
        q = up.parse_qs(up.urlparse(url).query)
        prefix = q.get("prefix", [""])[0]
        return _xml([], AGAC_KLASOR.get(prefix, []))

    gorulen = {}

    def sahte_sor(soru, secenekler, geri=True, girdi=None):
        prefix = soru.replace("Konum: ", "")
        gorulen[prefix] = list(secenekler)
        return None  # hemen çık

    orij = sd.sor
    sd.sor = sahte_sor
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sd.rehberli(tempfile.mkdtemp(prefix="sd_sig_"), 8.0, acici=acici)
        kok_menu = gorulen.get("SMART-DS/v1.0/", [])
        return {
            "kök seviyede indirme seçeneği YOK":
                not any("İNDİR" in s for s in kok_menu),
            "kök seviyede yıllar listelendi":
                any("2017" in s for s in kok_menu),
            "kullanıcıya seviye uyarısı verildi":
                "büyük" in buf.getvalue().lower(),
        }
    finally:
        sd.sor = orij


def main():
    print("=" * 68)
    print("smartds_download — güvenlik korumaları testi")
    print("=" * 68)
    k = {}
    print("\n  A) devasa klasör koruması")
    k.update(test_devasa_klasor())
    print("  B) zaman aşımı / yeniden deneme")
    k.update(test_yeniden_deneme())
    print("  C) sığ seviyede indirme engeli")
    k.update(test_sig_seviye())

    print("\n" + "-" * 68)
    ok = True
    for adi, gecti in k.items():
        print(f"  [{'OK ' if gecti else 'FAIL'}] {adi}")
        ok &= gecti
    print("-" * 68)
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}   ({len(k)} kontrol)")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
