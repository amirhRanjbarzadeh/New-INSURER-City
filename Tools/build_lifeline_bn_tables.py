"""Build the lifeline tables used by RML_Files/BN_Parts/_BNPower.xml and _BNWater.xml.

Run from the repository root:

    py -I Tools/build_lifeline_bn_tables.py

Inputs  (Database/insurer_city, Database/bayesian_network/city_grid_180.csv)
Outputs (Database/bayesian_network), power first; water_* files follow the same layout,
see water() for its rules:

  power_asset_grids.csv    every power station and line with the hazard grid cell
                           whose centre is nearest to it (stations at their own
                           location, lines at their midpoint). The BN damage node of
                           the asset takes that cell's intensity node as its parent.

  power_service_chain.csv  one row per station, in topological order (every
                           station comes after the station feeding it). Serviceability
                           is traced over the radial feeder tree formed by the normal
                           power lines from the Slack buses; maneuver lines are
                           normally open and are not part of the tree.
                           `cpt` is the user-defined serviceability CPT of the node.

  power_zone_service.csv   one row per zone: the stations through which supply
                           enters the zone (feeders) and the zone's user-defined CPT.

  power_zone_feeders.csv   one row per (zone, feeder), used to draw the zone map.

CPT text format (RConditionalProbabilityTable.ValueTable): one row per state of the
node, rows separated by '/', one column per parent configuration, columns separated
by ';'. Parent configurations are enumerated with the FIRST parent in ParentList as
the most significant digit (RBayesianNetworkAnalyzer::computeParentConfigurationIndex).

State order of every two-state node used here:
  Serviceability : 0 NotServiceable, 1 Serviceable
  Functionality  : 0 NotFunctional,  1 Functional
  Line Damage    : 0 Undamaged,      1 Damaged

The default CPTs are deterministic:
  root station      serviceable  <=> it is functional
  junction          serviceable  <=> upstream serviceable AND feeding line undamaged
  other station     serviceable  <=> upstream serviceable AND feeding line undamaged
                                     AND the station itself functional
  zone              serviceable  <=> at least one feeder serviceable
Edit the `cpt` columns (or the rules below) to make them probabilistic.
"""

import csv
import itertools
import math
import os
import re
import sys
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CITY = os.path.join(ROOT, "Database", "insurer_city")
BN = os.path.join(ROOT, "Database", "bayesian_network")

JUNCTION_TYPE = "Junction"


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {os.path.relpath(path, ROOT)} ({len(rows)} rows)")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_grid(grids, lat, lon):
    best = min(grids, key=lambda g: haversine_km(lat, lon, g[1], g[2]))
    return best[0], haversine_km(lat, lon, best[1], best[2])


def cpt_text(parent_state_counts, serviceable):
    """Two-state CPT. serviceable(config) -> P(Serviceable | config)."""
    configs = list(itertools.product(*[range(n) for n in parent_state_counts]))
    p_serv = [float(serviceable(c)) for c in configs]
    fmt = lambda p: f"{p:g}"
    not_serv_row = ";".join(fmt(1.0 - p) for p in p_serv)
    serv_row = ";".join(fmt(p) for p in p_serv)
    return f"{not_serv_row}/{serv_row}"


def main():
    stations = read_csv(os.path.join(CITY, "PowerStation.csv"))
    dist = read_csv(os.path.join(CITY, "PowerStation_Dist.csv"))
    gen = read_csv(os.path.join(CITY, "PowerStation_GenORTrans.csv"))
    lines = read_csv(os.path.join(CITY, "PowerLine.csv"))
    maneuver = read_csv(os.path.join(CITY, "PowerLine_Maneuver.csv"))
    grid_rows = read_csv(os.path.join(BN, "city_grid_180.csv"))
    grids = [(g["name"], float(g["lat"]), float(g["lon"])) for g in grid_rows]

    station_type = {r["Power Station Name"]: r["Power Station Type"] for r in dist + gen}
    bus_type = {r["Power Station Name"]: r["Bus Type"] for r in dist + gen}
    names = [s["Power Station Name"] for s in stations]

    missing = [n for n in names if n not in station_type]
    if missing:
        sys.exit(f"stations without a type in PowerStation_Dist/GenORTrans: {missing}")

    has_fragility = {n: station_type[n] != JUNCTION_TYPE for n in names}

    # ---------------------------------------------------------------- asset grids
    asset_rows = []
    for s in stations:
        name = s["Power Station Name"]
        g, d = nearest_grid(grids, float(s["Latitude"]), float(s["Longitude"]))
        asset_rows.append({"asset_class": "PowerStation", "asset_name": name,
                           "has_fragility": str(has_fragility[name]).upper(),
                           "grid_name": g, "distance_km": f"{d:.4f}"})
    for cls, table in (("PowerLine", lines), ("PowerLineManeuver", maneuver)):
        for l in table:
            g, d = nearest_grid(grids, float(l["Middle Point Latitude"]), float(l["Middle Point Longitude"]))
            asset_rows.append({"asset_class": cls, "asset_name": l["Power Line Name"],
                               "has_fragility": "TRUE", "grid_name": g, "distance_km": f"{d:.4f}"})
    write_csv(os.path.join(BN, "power_asset_grids.csv"),
              ["asset_class", "asset_name", "has_fragility", "grid_name", "distance_km"], asset_rows)

    # ------------------------------------------------------------- feeder tree
    adjacency = {n: [] for n in names}
    for l in lines:
        a, b = l["Origin Node"], l["Destination Node"]
        for x in (a, b):
            if x not in adjacency:
                sys.exit(f"line {l['Power Line Name']} ends at unknown station {x}")
        adjacency[a].append((b, l["Power Line Name"]))
        adjacency[b].append((a, l["Power Line Name"]))

    roots = [n for n in names if bus_type[n] == "Slack"]
    if not roots:
        sys.exit("no Slack bus found")
    for r in roots:
        if not has_fragility[r]:
            sys.exit(f"root {r} has no fragility; the root CPT below assumes it does")

    upstream, link, order = {}, {}, []
    queue = deque()
    for r in roots:
        upstream[r], link[r] = "", ""
        queue.append(r)
    while queue:
        n = queue.popleft()
        order.append(n)
        for m, line_name in adjacency[n]:
            if m in upstream:
                if upstream[n] != m and upstream[m] != n:
                    sys.exit(f"normal lines form a loop at {line_name}; the chain needs a radial tree")
                continue
            upstream[m], link[m] = n, line_name
            queue.append(m)

    unreached = [n for n in names if n not in upstream]
    if unreached:
        sys.exit(f"stations not fed from any Slack bus over normal lines: {unreached}")

    chain_rows = []
    for n in order:
        is_root = upstream[n] == ""
        if is_root:
            parents = [f"{n}Functionality"]
            cpt = cpt_text([2], lambda c: c[0] == 1)
        elif not has_fragility[n]:
            parents = [f"{upstream[n]}Serviceability", f"{link[n]}Damage"]
            cpt = cpt_text([2, 2], lambda c: c[0] == 1 and c[1] == 0)
        else:
            parents = [f"{upstream[n]}Serviceability", f"{link[n]}Damage", f"{n}Functionality"]
            cpt = cpt_text([2, 2, 2], lambda c: c[0] == 1 and c[1] == 0 and c[2] == 1)
        chain_rows.append({"node": n, "upstream": upstream[n], "link": link[n],
                           "is_root": str(is_root).upper(),
                           "has_fragility": str(has_fragility[n]).upper(),
                           "parents": ";".join(parents), "cpt": cpt})
    write_csv(os.path.join(BN, "power_service_chain.csv"),
              ["node", "upstream", "link", "is_root", "has_fragility", "parents", "cpt"], chain_rows)

    # ------------------------------------------------------------------- zones
    # A zone's feeders are its stations fed from outside the zone. Stations with a
    # blank ZoneID (the transmission backbone) belong to no zone.
    zone_of = {s["Power Station Name"]: s["ZoneID"].strip() for s in stations}
    feeders = {}
    for n in order:
        z = zone_of[n]
        if not z:
            continue
        up = upstream[n]
        if up == "" or zone_of[up] != z:
            feeders.setdefault(z, []).append(n)

    zone_rows, feeder_rows = [], []
    for z in sorted(feeders):
        zone = "PNZone" + z.lstrip("Z")
        fs = sorted(feeders[z])
        cpt = cpt_text([2] * len(fs), lambda c: any(v == 1 for v in c))
        zone_rows.append({"zone": zone, "feeders": ";".join(fs),
                          "parents": ";".join(f"{f}Serviceability" for f in fs),
                          "responses": ";".join(f"{f}Serv_R_" for f in fs),
                          "cpt": cpt})
        feeder_rows.extend({"zone": zone, "feeder": f} for f in fs)
    write_csv(os.path.join(BN, "power_zone_service.csv"),
              ["zone", "feeders", "parents", "responses", "cpt"], zone_rows)
    write_csv(os.path.join(BN, "power_zone_feeders.csv"), ["zone", "feeder"], feeder_rows)


def zone_tables(order, upstream, zone_of, prefix, response_suffix="Serv_R_"):
    """Zone rows and (zone, feeder) rows. A zone's feeders are its nodes fed from outside it."""
    feeders = {}
    for n in order:
        z = zone_of.get(n, "")
        if not z:
            continue
        up = upstream[n]
        if up == "" or zone_of.get(up, "") != z:
            feeders.setdefault(z, []).append(n)

    zone_rows, feeder_rows = [], []
    for z in sorted(feeders, key=int):
        zone = f"{prefix}{int(z):02d}"
        fs = sorted(feeders[z])
        zone_rows.append({"zone": zone, "feeders": ";".join(fs),
                          "parents": ";".join(f"{f}Serviceability" for f in fs),
                          "responses": ";".join(f"{f}{response_suffix}" for f in fs),
                          "cpt": cpt_text([2] * len(fs), lambda c: any(v == 1 for v in c))})
        feeder_rows.extend({"zone": zone, "feeder": f} for f in fs)
    return zone_rows, feeder_rows


def bfs_tree(names, edges, roots, what):
    """Spanning tree from the roots over undirected edges (a, b, link)."""
    adjacency = {n: [] for n in names}
    for a, b, link in edges:
        for x in (a, b):
            if x not in adjacency:
                sys.exit(f"{what}: {link} ends at unknown node {x}")
        adjacency[a].append((b, link))
        adjacency[b].append((a, link))

    upstream, via, order = {}, {}, []
    queue = deque()
    for r in roots:
        upstream[r], via[r] = "", ""
        queue.append(r)
    loops = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for m, link in adjacency[n]:
            if m in upstream:
                if upstream[n] != m and upstream[m] != n and link not in loops:
                    loops.append(link)
                continue
            upstream[m], via[m] = n, link
            queue.append(m)

    unreached = [n for n in names if n not in upstream]
    if unreached:
        sys.exit(f"{what}: nodes not fed from any source: {unreached}")
    used = set(via.values())
    dropped = sorted({l for _, _, l in edges if l not in used})
    return upstream, via, order, dropped


def water():
    """Water tables. Pipe damage states: 0 None, 1 Leak, 2 Break. Distribution node damage
    states: 0 Undamaged (repair rate <= 0.2), 1 Damaged. As in the old water network model,
    a pipe stops supply only when it breaks and a node fails above a 0.2 repair rate."""
    stations = read_csv(os.path.join(CITY, "WaterStation.csv"))
    locs = read_csv(os.path.join(CITY, "WSDNLocsAndTypes.csv"))
    dns = read_csv(os.path.join(CITY, "WaterStationDistributionNodes.csv"))
    pipes = read_csv(os.path.join(CITY, "WaterPipeline.csv"))
    grids = [(g["name"], float(g["lat"]), float(g["lon"]))
             for g in read_csv(os.path.join(BN, "city_grid_180.csv"))]

    kind = {}
    for s in stations:
        kind[s["Water Station Name"]] = "Root" if s["isSource"].strip().lower() == "true" else "Station"
    dn_names = {"WS" + d["Distribution Node Name"] for d in dns}
    for l in locs:
        n = l["Water Station Name"]
        kind[n] = "Node" if n in dn_names else "Junction"
    names = [s["Water Station Name"] for s in stations] + [l["Water Station Name"] for l in locs]

    # ------------------------------------------------------------ asset grids
    rows = []
    for s in stations:
        g, d = nearest_grid(grids, float(s["Latitude"]), float(s["Longitude"]))
        rows.append({"asset_class": "WaterStation", "asset_name": s["Water Station Name"],
                     "grid_name": g, "distance_km": f"{d:.4f}"})
    for l in locs:
        if kind[l["Water Station Name"]] != "Node":
            continue
        g, d = nearest_grid(grids, float(l["Latitude"]), float(l["Longitude"]))
        rows.append({"asset_class": "WaterDistributionNode", "asset_name": l["Water Station Name"],
                     "grid_name": g, "distance_km": f"{d:.4f}"})
    for p in pipes:
        g, d = nearest_grid(grids, float(p["Pipeline Center Latitude"]), float(p["Pipeline Center Longitude"]))
        rows.append({"asset_class": "WaterPipeline", "asset_name": "W" + p["Pipe Line Name"],
                     "grid_name": g, "distance_km": f"{d:.4f}"})
    write_csv(os.path.join(BN, "water_asset_grids.csv"),
              ["asset_class", "asset_name", "grid_name", "distance_km"], rows)

    # ------------------------------------------------------------ supply tree
    roots = [n for n in names if kind[n] == "Root"]
    edges = [(p["Origin Node"], p["Destination Node"], "W" + p["Pipe Line Name"]) for p in pipes]
    upstream, via, order, dropped = bfs_tree(names, edges, roots, "water")
    if dropped:
        print(f"  water: loop pipes left out of the supply tree (redundancy ignored): {', '.join(dropped)}")

    pipe_ok = lambda s: s != 2          # None or Leak still delivers water
    chain = []
    for n in order:
        k, up, link = kind[n], upstream[n], via[n]
        if k == "Root":
            parents = [f"{n}Functionality"]
            cpt = cpt_text([2], lambda c: c[0] == 1)
        elif k == "Station":
            parents = [f"{up}Serviceability", f"{link}Damage", f"{n}Functionality"]
            cpt = cpt_text([2, 3, 2], lambda c: c[0] == 1 and pipe_ok(c[1]) and c[2] == 1)
        elif k == "Node":
            parents = [f"{up}Serviceability", f"{link}Damage", f"{n}Damage"]
            cpt = cpt_text([2, 3, 2], lambda c: c[0] == 1 and pipe_ok(c[1]) and c[2] == 0)
        else:
            parents = [f"{up}Serviceability", f"{link}Damage"]
            cpt = cpt_text([2, 3], lambda c: c[0] == 1 and pipe_ok(c[1]))
        chain.append({"node": n, "kind": k, "upstream": up, "link": link,
                      "parents": ";".join(parents), "cpt": cpt})
    write_csv(os.path.join(BN, "water_service_chain.csv"),
              ["node", "kind", "upstream", "link", "parents", "cpt"], chain)

    # ------------------------------------------------------------------ zones
    # Zone of a distribution node from its Description ("Zone1-2", "zone 11-2", ...).
    zone_of = {}
    for d in dns:
        m = re.search(r"zone\s*(\d+)", d["Description"], re.IGNORECASE)
        if m:
            zone_of["WS" + d["Distribution Node Name"]] = m.group(1)
    zone_rows, feeder_rows = zone_tables(order, upstream, zone_of, "WNZone")
    write_csv(os.path.join(BN, "water_zone_service.csv"),
              ["zone", "feeders", "parents", "responses", "cpt"], zone_rows)
    write_csv(os.path.join(BN, "water_zone_feeders.csv"), ["zone", "feeder"], feeder_rows)


if __name__ == "__main__":
    main()
    water()
