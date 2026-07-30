"""
SMART-DS (OpenDSS) -> TwinRepresentation loader.  [BUILD_PLAN Faz 1, adım 2]

This is the INVERSE of backends/opendss_adapter.py: it reads an existing
distribution model and lifts it into the CIM-grounded representation, so that
the ground truth and the reconstructed twin are expressed in the same terms
and can be compared element by element.

Critically it preserves full phase-impedance matrices (CIM PhaseImpedanceData).
Collapsing SMART-DS lines to sequence values makes the round-trip acceptance
criterion (voltage MAE < 1e-6 pu) unreachable on unbalanced laterals.

Scope note: covers Line / LineCode / Transformer / Load / Capacitor / PVSystem /
Vsource / LoadShape. Regulators, reactors, storage and switch control are not
yet lifted — `load_feeder()` returns them in `unsupported` so the caller (and
the critic agent) can see exactly what was dropped rather than silently losing it.
"""
import os
import opendssdirect as dss

from core.twin import (
    TwinRepresentation, ConnectivityNode, PerLengthPhaseImpedance,
    ACLineSegment, PowerTransformer, EnergyConsumer, EnergySource,
    LinearShuntCompensator, PowerElectronicsConnection, LoadShape,
    RegulatingControl, MultiWindingTransformer,
)

# OpenDSS length-unit codes
_UNITS = {0: "none", 1: "mi", 2: "kft", 3: "km", 4: "m", 5: "ft", 6: "in", 7: "cm"}


def _split_bus(busname: str):
    """'bus1.1.3' -> ('bus1', '.1.3');  'bus1' -> ('bus1', '')."""
    if "." in busname:
        head, rest = busname.split(".", 1)
        return head.lower(), "." + rest
    return busname.lower(), ""


def _lower_triangle(flat, n):
    """OpenDSS returns an n*n flat matrix; keep the lower triangle row-major."""
    if not flat or n <= 0:
        return None
    out = []
    for i in range(n):
        for j in range(i + 1):
            out.append(flat[i * n + j])
    return out


def _phases_from_conn(conn: str, nphases: int) -> str:
    if not conn:
        return "ABC"[:max(1, nphases)]
    m = {"1": "A", "2": "B", "3": "C"}
    ph = "".join(m[c] for c in conn.strip(".").split(".") if c in m)
    return ph or "ABC"[:max(1, nphases)]


def load_feeder(master_dss_path: str, name: str = "smartds_feeder") -> tuple:
    """
    Compile an OpenDSS model and lift it into a TwinRepresentation.

    Returns (twin, report) where report lists what was skipped/approximated.
    """
    if not os.path.exists(master_dss_path):
        raise FileNotFoundError(master_dss_path)

    dss.Command("Clear")
    dss.Command(f'Redirect "{master_dss_path}"')

    t = TwinRepresentation(name=name)
    report = {"unsupported": [], "warnings": []}

    # ---- source -------------------------------------------------------------
    if dss.Vsources.Count() > 0:
        dss.Vsources.First()
        dss.Circuit.SetActiveElement(f"Vsource.{dss.Vsources.Name()}")
        bus, _ = _split_bus(dss.CktElement.BusNames()[0])
        kv = dss.Vsources.BasekV()
        t.add(ConnectivityNode(bus, kv), "smartds_loader")
        # Thevenin empedansı yalnızca Properties API'sinden okunabiliyor
        # (opendssdirect'te Vsources.R1/X1 üyeleri yok).
        dss.Circuit.SetActiveElement(f"Vsource.{dss.Vsources.Name()}")

        def _prop(ad):
            try:
                v = dss.Properties.Value(ad)
                return float(v) if v not in (None, "") else None
            except Exception:
                return None

        r1, x1 = _prop("R1"), _prop("X1")
        r0, x0 = _prop("R0"), _prop("X0")
        if r1 is None or x1 is None:
            report["warnings"].append(
                "kaynak empedansı okunamadı; varsayılan Isc3 kullanılacak "
                "(round-trip hatası ~1e-4 pu olabilir)")
        t.add(EnergySource("src", bus, kv, pu=dss.Vsources.PU(),
                           r1=r1, x1=x1, r0=r0, x0=x0), "smartds_loader")
    else:
        report["warnings"].append("no Vsource found")

    # ---- buses --------------------------------------------------------------
    for b in dss.Circuit.AllBusNames():
        dss.Circuit.SetActiveBus(b)
        kv = dss.Bus.kVBase()
        bl = b.lower()
        if bl not in t.nodes:
            t.add(ConnectivityNode(bl, kv), "smartds_loader")

    # ---- line codes --------------------------------------------------------
    # Always preserve the full matrix form. OpenDSS's reported R1/X1 for these
    # codes is an unreliable derived value (it reads as a constant 0.058 across
    # every code in the feeder, regardless of conductor), so any sequence-vs-
    # matrix heuristic based on R1 is meaningless. The matrix returned by
    # Rmatrix()/Xmatrix() is the authoritative definition and round-trips
    # correctly (verified in isolation).
    if dss.LineCodes.Count() > 0:
        i = dss.LineCodes.First()
        while i:
            nm = dss.LineCodes.Name()
            n = dss.LineCodes.Phases()
            units = _UNITS.get(dss.LineCodes.Units(), "km")
            t.add(PerLengthPhaseImpedance(
                mrid=nm, n_phases=n,
                rmatrix=_lower_triangle(list(dss.LineCodes.Rmatrix()), n),
                xmatrix=_lower_triangle(list(dss.LineCodes.Xmatrix()), n),
                cmatrix=_lower_triangle(list(dss.LineCodes.Cmatrix()), n),
                length_units=units,
            ), "smartds_loader")
            i = dss.LineCodes.Next()

    # ---- lines --------------------------------------------------------------
    i = dss.Lines.First()
    while i:
        nm = dss.Lines.Name()
        b1, c1 = _split_bus(dss.Lines.Bus1())
        b2, c2 = _split_bus(dss.Lines.Bus2())
        nph = dss.Lines.Phases()
        code = dss.Lines.LineCode()

        # a switch/fuse/recloser Line carries switch=Yes; its impedance must NOT
        # be applied as a normal segment (OpenDSS treats it as a closed switch)
        dss.Circuit.SetActiveElement(f"Line.{nm}")
        is_switch = (dss.Properties.Value("switch") or "").strip().lower() == "yes"

        if is_switch:
            t.add(ACLineSegment(
                mrid=nm, node_a=b1, node_b=b2,
                length_km=dss.Lines.Length(),
                impedance_mrid="__switch__",
                phases=_phases_from_conn(c1, nph),
                conn_a=c1, conn_b=c2,
                length_units=_UNITS.get(dss.Lines.Units(), "km"),
            ), "smartds_loader")
            i = dss.Lines.Next()
            continue

        if not code:
            # geometry- or matrix-defined line: lift its own matrices
            code = f"{nm}__z"
            t.add(PerLengthPhaseImpedance(
                mrid=code,
                n_phases=nph,
                rmatrix=_lower_triangle(dss.Lines.RMatrix(), nph),
                xmatrix=_lower_triangle(dss.Lines.XMatrix(), nph),
                cmatrix=_lower_triangle(dss.Lines.CMatrix(), nph),
                length_units=_UNITS.get(dss.Lines.Units(), "km"),
            ), "smartds_loader")

        t.add(ACLineSegment(
            mrid=nm, node_a=b1, node_b=b2,
            length_km=dss.Lines.Length(),
            impedance_mrid=code,
            phases=_phases_from_conn(c1, nph),
            conn_a=c1, conn_b=c2,
            length_units=_UNITS.get(dss.Lines.Units(), "km"),
        ), "smartds_loader")
        i = dss.Lines.Next()

    # ---- transformers -------------------------------------------------------
    i = dss.Transformers.First()
    reg_transformers = set()
    if dss.RegControls.Count() > 0:
        j = dss.RegControls.First()
        while j:
            reg_transformers.add(dss.RegControls.Transformer().lower())
            j = dss.RegControls.Next()
    while i:
        nm = dss.Transformers.Name()
        dss.Circuit.SetActiveElement(f"Transformer.{nm}")
        buses = dss.CktElement.BusNames()
        nph = dss.CktElement.NumPhases()
        nwind = dss.Transformers.NumWindings()

        def _p(ad, d=None):
            try:
                v = dss.Properties.Value(ad)
                return v if v not in (None, "") else d
            except Exception:
                return d

        if nwind != 2:
            # general N-winding unit (SMART-DS split-phase service transformers)
            def _numlist(raw):
                return [x for x in (raw or "").strip("[]").replace(" ", "").split(",") if x]
            conns = _numlist(_p("conns"))
            kvs = _numlist(_p("kvs"))
            kvas = _numlist(_p("kvas"))
            wdgs = []
            for w in range(1, nwind + 1):
                dss.Transformers.Wdg(w)
                wdgs.append({
                    "bus": _split_bus(buses[w - 1])[0],
                    "conn_str": buses[w - 1].split(".", 1)[1] if "." in buses[w - 1] else "",
                    "conn": conns[w - 1] if w - 1 < len(conns) else ("delta" if w == 1 else "wye"),
                    "kv": float(kvs[w - 1]) if w - 1 < len(kvs) else dss.Transformers.kV(),
                    "kva": float(kvas[w - 1]) if w - 1 < len(kvas) else dss.Transformers.kVA(),
                    "tap": dss.Transformers.Tap(),
                })
            try:
                ll = float(_p("%loadloss")) if _p("%loadloss") else None
                nl = float(_p("%noloadloss")) if _p("%noloadloss") else None
            except Exception:
                ll = nl = None
            rs_raw = _numlist(_p("%Rs"))
            rs = [float(x) for x in rs_raw] if rs_raw else None
            t.add(MultiWindingTransformer(
                mrid=nm, phases=nph, windings=wdgs,
                xhl=float(_p("xhl", dss.Transformers.Xhl())),
                xht=float(_p("xht")) if _p("xht") else None,
                xlt=float(_p("xlt")) if _p("xlt") else None,
                loadloss_pct=ll, noloadloss_pct=nl, rs_pct=rs,
            ), "smartds_loader")
            i = dss.Transformers.Next()
            continue

        conns = (_p("conns", "[wye, wye]") or "").strip("[]").replace(" ", "")
        conn_list = [c for c in conns.split(",") if c] or ["delta", "wye"]
        conn_hv = conn_list[0] if len(conn_list) > 0 else "delta"
        conn_lv = conn_list[1] if len(conn_list) > 1 else "wye"

        dss.Transformers.Wdg(1); hv_kv = dss.Transformers.kV(); tap_hv = dss.Transformers.Tap()
        kva = dss.Transformers.kVA()
        dss.Transformers.Wdg(2); lv_kv = dss.Transformers.kV(); tap_lv = dss.Transformers.Tap()
        max_tap = dss.Transformers.MaxTap(); min_tap = dss.Transformers.MinTap()
        num_taps = dss.Transformers.NumTaps()

        try:
            loadloss = float(_p("%loadloss")) if _p("%loadloss") else None
            noloadloss = float(_p("%noloadloss")) if _p("%noloadloss") else None
        except Exception:
            loadloss = noloadloss = None

        hv, _ = _split_bus(buses[0]); lv, _ = _split_bus(buses[1])
        t.add(PowerTransformer(nm, hv, lv, rated_mva=kva / 1000.0,
                               hv_kv=hv_kv, lv_kv=lv_kv,
                               resistance_pct=dss.Transformers.R(),
                               reactance_pct=dss.Transformers.Xhl(),
                               conn_hv=conn_hv, conn_lv=conn_lv,
                               loadloss_pct=loadloss, noloadloss_pct=noloadloss,
                               tap_hv=tap_hv, tap_lv=tap_lv,
                               num_taps=int(num_taps) if num_taps else 32,
                               max_tap=max_tap or 1.1, min_tap=min_tap or 0.9,
                               phases=nph,
                               is_regulator=(nm.lower() in reg_transformers)),
              "smartds_loader")
        i = dss.Transformers.Next()

    # ---- load shapes --------------------------------------------------------
    if dss.LoadShape.Count() > 0:
        i = dss.LoadShape.First()
        while i:
            nm = dss.LoadShape.Name()
            if nm.lower() != "default":
                t.add(LoadShape(nm, dss.LoadShape.HrInterval(),
                                list(dss.LoadShape.PMult())), "smartds_loader")
            i = dss.LoadShape.Next()

    # ---- loads --------------------------------------------------------------
    i = dss.Loads.First()
    while i:
        nm = dss.Loads.Name()
        dss.Circuit.SetActiveElement(f"Load.{nm}")
        bus, conn = _split_bus(dss.CktElement.BusNames()[0])
        shape = (dss.Loads.Daily() or dss.Loads.Yearly()
                 or dss.Loads.Duty() or None)
        t.add(EnergyConsumer(nm, bus, dss.Loads.kW(), dss.Loads.kvar(),
                             shape if shape and shape.lower() != "" else None,
                             phases=_phases_from_conn(conn, dss.Loads.Phases()),
                             kv=dss.Loads.kV(), conn=conn,
                             is_delta=(dss.Loads.IsDelta())),
              "smartds_loader")
        i = dss.Loads.Next()

    # ---- capacitors ---------------------------------------------------------
    i = dss.Capacitors.First()
    while i:
        nm = dss.Capacitors.Name()
        dss.Circuit.SetActiveElement(f"Capacitor.{nm}")
        bus, cconn = _split_bus(dss.CktElement.BusNames()[0])
        t.add(LinearShuntCompensator(nm, bus, dss.Capacitors.kvar(),
                                     kv=dss.Capacitors.kV(), conn=cconn,
                                     phases=_phases_from_conn(
                                         cconn, dss.CktElement.NumPhases())),
              "smartds_loader")
        i = dss.Capacitors.Next()

    # ---- PV -----------------------------------------------------------------
    if dss.PVsystems.Count() > 0:
        i = dss.PVsystems.First()
        while i:
            nm = dss.PVsystems.Name()
            dss.Circuit.SetActiveElement(f"PVSystem.{nm}")
            bus, _ = _split_bus(dss.CktElement.BusNames()[0])
            t.add(PowerElectronicsConnection(nm, bus, dss.PVsystems.Pmpp()),
                  "smartds_loader")
            i = dss.PVsystems.Next()

    # ---- voltage regulators (RegControl over a controlled transformer) ------
    if dss.RegControls.Count() > 0:
        i = dss.RegControls.First()
        while i:
            nm = dss.RegControls.Name()
            t.add(RegulatingControl(
                mrid=nm,
                transformer_mrid=dss.RegControls.Transformer(),
                winding=dss.RegControls.Winding(),
                target_v=dss.RegControls.ForwardVreg(),
                band=dss.RegControls.ForwardBand(),
                pt_ratio=dss.RegControls.PTRatio(),
                delay=dss.RegControls.Delay(),
                ct_primary=dss.RegControls.CTPrimary() or None,
                r=dss.RegControls.ForwardR(),
                x=dss.RegControls.ForwardX(),
            ), "smartds_loader")
            i = dss.RegControls.Next()

    # ---- what we did NOT lift ----------------------------------------------
    for cls, label in [("Reactor", "reactor"),
                       ("Storage", "storage"),
                       ("Recloser", "recloser")]:
        names = dss.Circuit.AllElementNames()
        found = [n for n in names if n.lower().startswith(cls.lower() + ".")]
        if found:
            report["unsupported"].append(f"{label}: {len(found)} element(s) not lifted")

    return t, report
