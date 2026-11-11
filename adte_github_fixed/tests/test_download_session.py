"""
smartds_download.py için sahte (mock) testler.

Bu ortamdan OEDI'ye ağ erişimi YOK (403). Testler gerçek S3 XML biçimini
taklit eden bir ağaçla çalışır ve şunları doğrular:
  A) sor()  — numaralı menü: geçerli/geçersiz seçim, 'g' (geri), 'q' (çık)
  B) rehberli() — klasörler arası gezinme, indirme, klasör yapısının korunması,
     profiles'ın üst seviyede otomatik bulunması, devam ettirme (resume)
"""
import os, sys
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import os
import sys
import shutil
import tempfile
import io
import contextlib


import smartds_download as sd

AGAC = {
    "SMART-DS/v1.0/2017/SFO/P27U/scenarios/base_timeseries/opendss/"
    "p27uhs0_1247/Master.dss": 4096,
    "SMART-DS/v1.0/2017/SFO/P27U/scenarios/base_timeseries/opendss/"
    "p27uhs0_1247/Lines.dss": 20480,
    "SMART-DS/v1.0/2017/SFO/P27U/profiles/res_1.csv": 10240,
    "SMART-DS/v1.0/2017/SFO/P27U/profiles/com_1.csv": 10240,
    "SMART-DS/v1.0/2017/GSO/P1U/scenarios/base_timeseries/opendss/x/Master.dss": 512,
}


def sahte_acici(url):
    import urllib.parse as up
    q = up.parse_qs(up.urlparse(url).query)
    prefix = q.get("prefix", [""])[0]
    ayrac = q.get("delimiter", [""])[0]

    klasorler, dosyalar = set(), []
    for k, s in AGAC.items():
        if not k.startswith(prefix):
            continue
        kalan = k[len(prefix):]
        if ayrac and ayrac in kalan:
            klasorler.add(prefix + kalan.split(ayrac)[0] + ayrac)
        else:
            dosyalar.append((k, s))

    p = ["<?xml version='1.0' encoding='UTF-8'?>",
         "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>",
         "<IsTruncated>false</IsTruncated>"]
    for f in sorted(klasorler):
        p.append(f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>")
    for k, s in dosyalar:
        p.append(f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>")
    p.append("</ListBucketResult>")
    return "".join(p).encode()


def sahte_indir(url, zaman_asimi=300):
    import urllib.parse as up
    anahtar = up.unquote(url[len(sd.S3) + 1:])
    return b"x" * AGAC.get(anahtar, 0)


# ----------------------------------------------------------------- A) sor()
def test_sor():
    print("\n  A) sor() — menü davranışı")

    def dizi(cevaplar):
        it = iter(cevaplar)
        return lambda soru="": next(it)

    r = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r["gecerli"] = sd.sor("t", ["a", "b", "c"], girdi=dizi(["2"]))
        r["gecersiz"] = sd.sor("t", ["a", "b"], girdi=dizi(["9", "sacma", "1"]))
        r["geri"] = sd.sor("t", ["a"], geri=True, girdi=dizi(["g"]))
        r["cikis"] = sd.sor("t", ["a"], girdi=dizi(["q"]))
        r["geri_kapali"] = sd.sor("t", ["a"], geri=False, girdi=dizi(["g", "1"]))

    return {
        "numara ile seçim ('2' -> 'b')": r["gecerli"] == "b",
        "geçersiz girdi reddedilip tekrar sorulur": r["gecersiz"] == "a",
        "'g' geri döndürür": r["geri"] == "GERI",
        "'q' çıkış (None)": r["cikis"] is None,
        "geri kapalıyken 'g' yok sayılır": r["geri_kapali"] == "a",
    }


# ------------------------------------------------------------ B) rehberli()
def test_rehberli(hedef):
    print("\n  B) rehberli() — gezinme + indirme")

    yol = ["2017", "GSO", "GERI", "SFO", "P27U", "scenarios",
           "base_timeseries", "opendss", "p27uhs0_1247", "INDIR"]
    it = iter(yol)
    menuler = []

    def sahte_sor(soru, secenekler, geri=True, girdi=None):
        menuler.append(list(secenekler))
        istenen = next(it)
        if istenen == "GERI":
            return "GERI"
        if istenen == "INDIR":
            for s in secenekler:
                if s.startswith("[BU KLASÖRÜ İNDİR]"):
                    return s
            raise AssertionError(f"indirme seçeneği yok: {secenekler}")
        for s in secenekler:
            if s == istenen:
                return s
        raise AssertionError(f"{istenen!r} menüde yok: {secenekler}")

    orij_sor, orij_evet = sd.sor, sd.evet_mi
    sd.sor = sahte_sor
    sd.evet_mi = lambda soru, girdi=None: True
    try:
        kod = sd.rehberli(hedef, max_gb=8.0, acici=sahte_acici)
    finally:
        sd.sor, sd.evet_mi = orij_sor, orij_evet

    inen = []
    for k, _, ds in os.walk(hedef):
        for d in ds:
            inen.append(os.path.relpath(os.path.join(k, d), hedef))

    print("     inen dosyalar:")
    for f in sorted(inen):
        print(f"       {f}")

    tekrar = sd.klasor_indir(
        "SMART-DS/v1.0/2017/SFO/P27U/scenarios/base_timeseries/opendss/"
        "p27uhs0_1247/", hedef, acici=sahte_acici)

    return {
        "çıkış kodu 0": kod == 0,
        "kök menüde yıl göründü": any("2017" in m for m in menuler),
        "'geri' bir üst seviyeye döndü": any("SFO" in m for m in menuler),
        "Master.dss indi": any(f.endswith("Master.dss") for f in inen),
        "klasör yapısı korundu":
            any(os.path.join("2017", "SFO", "P27U") in f for f in inen),
        "profiles otomatik bulunup indi": any("profiles" in f for f in inen),
        "seçilmeyen bölge (GSO) inmedi": not any("GSO" in f for f in inen),
        "ikinci koşumda yeniden indirmedi": tekrar == 0,
    }


def main():
    print("=" * 68)
    print("smartds_download — mock testler (ağ yok)")
    print("=" * 68)

    hedef = tempfile.mkdtemp(prefix="sd_test_")
    orij = sd.indir_bayt
    sd.indir_bayt = sahte_indir
    try:
        kontroller = {}
        kontroller.update(test_sor())
        kontroller.update(test_rehberli(hedef))

        print("\n" + "-" * 68)
        ok = True
        for adi, gecti in kontroller.items():
            print(f"  [{'OK ' if gecti else 'FAIL'}] {adi}")
            ok &= gecti
        print("-" * 68)
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}   ({len(kontroller)} kontrol)")
        print("  NOT: canlı OEDI ucu bu ortamdan test EDİLEMEDİ (HTTP 403).")
        print("=" * 68)
        return 0 if ok else 1
    finally:
        sd.indir_bayt = orij
        shutil.rmtree(hedef, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
