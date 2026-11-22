"""Build the hospital node table used by RML_Files/BN_Parts/_BNHospital.xml.

Run from the repository root, after build_lifeline_bn_tables.py (the zone tables are read):

    py -I Tools/build_hospital_bn_tables.py

Inputs
  Database/insurer_city/HospitalBuilding.csv    hospitals, their sub-block and zone (HStar skipped)
  Database/insurer_city/HospitalEquipment.csv   equipment inventory (used by Main/_Hospital.xml)
  Database/insurer_city/HospitalService.csv     clinical services of each hospital
  Database/bayesian_network/subblock_grids.csv  hazard grid cell of each sub-block
  Database/bayesian_network/power_zone_service.csv, water_zone_service.csv
  Dependencies/StateBounds.csv                  state bounds shared with the other BN parts

Output
  Database/bayesian_network/hospital_nodes.csv  one row per hospital BN node.

    node     node name; its response model is <node>_Val, its CPT <node>CPT
    shape    which expression _BNHospital.xml builds from a, b, c:
               pass       a
               zeroFlag   1 while a (a sum of equipment damage states) is 0
               twoOf      0 neither, 1 a only, 2 b only, 3 both (a, b are services)
               bothBelow  1 while both damage ratios a, b stay below Extensive (0.16)
               either     1 while a or b is up (own backup or the zone supply)
               both       1 while a and b are up
               allThree   1 while a, b and c are up
               treatment  0 closed (a down), else 1 + full treatment (b) + full examination (c)
    states   state names, bounds the matching StateBoundsTable, parents the ParentList.

Equipment is not in the network: the medical, gas and backup nodes take the intensity
nodes of the hospital's grid cell as parents and the learner absorbs the equipment.
"""

import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CITY = os.path.join(ROOT, "Database", "insurer_city")
BN = os.path.join(ROOT, "Database", "bayesian_network")
DEP = os.path.join(ROOT, "Dependencies")

SERVICES = ("Laboratory", "Operation", "Radiology", "Resuscitation")
UTILITY_ITEMS = ("OxygenPipe", "OxygenGenerator", "EmergencyPowerGenerator", "EmergencyWaterTank")

FOUR_LEVEL_BOUNDS = "0.5/0.5;1.5/1.5;2.5/2.5"


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {os.path.relpath(path, ROOT)} ({len(rows)} rows)")


def main():
    hospitals = [h for h in read_csv(os.path.join(CITY, "HospitalBuilding.csv"))
                 if h["isHStar"].strip().lower() != "true"]
    equipment = read_csv(os.path.join(CITY, "HospitalEquipment.csv"))
    services = read_csv(os.path.join(CITY, "HospitalService.csv"))
    grid_of = {r["subblock"]: r["grid_name"] for r in read_csv(os.path.join(BN, "subblock_grids.csv"))}
    power_zones = {r["zone"] for r in read_csv(os.path.join(BN, "power_zone_service.csv"))}
    water_zones = {r["zone"] for r in read_csv(os.path.join(BN, "water_zone_service.csv"))}
    bounds = read_csv(os.path.join(DEP, "StateBounds.csv"))[0]

    damage_bounds = bounds["building_damage_states"]
    flag_bounds = bounds["functionality_states"]
    serv_bounds = bounds["serviceability_states"]

    items = {(e["hospital"], e["item"]) for e in equipment}
    has_service = {(s["hospital"], s["service"]) for s in services}

    rows = []
    for h in hospitals:
        name, sb = h["Hospital Name"], h["Sub Block Name"]
        if sb not in grid_of:
            sys.exit(f"{name}: sub-block {sb} is not in subblock_grids.csv")
        missing = [s for s in SERVICES if (name, s) not in has_service] + \
                  [i for i in UTILITY_ITEMS if (name, i) not in items]
        if missing:
            sys.exit(f"{name}: missing in the hospital inventory: {missing}")

        zone = int(h["Zone_ID"].strip().lstrip("Zz"))
        pzone, wzone = f"PNZone{zone:02d}", f"WNZone{zone:02d}"
        for z, known in ((pzone, power_zones), (wzone, water_zones)):
            if z not in known:
                sys.exit(f"{name}: zone {z} has no serviceability node")

        g = grid_of[sb]
        spectral = f"IM{g}SaShortPeriod;IM{g}Sa1"
        pga = f"IM{g}Sa0"
        str_dmg = f"{sb}Dmg_StructuralCostRatio_"
        nsa_dmg = f"{sb}Dmg_NonstructuralAccelerationSensitiveDamageRatio_"
        val = lambda n: f"{name}{n}_Val_R_"
        state = lambda i: f"{name}_{i}_State_"
        service = lambda s: f"{name}_{s}_Service_R_"

        def node(n, shape, a, b="", c="", states="NotFunctional;Functional", bnds=flag_bounds, parents=""):
            rows.append({"hospital": name, "node": f"{name}{n}", "shape": shape, "a": a, "b": b, "c": c,
                         "states": states, "bounds": bnds, "parents": parents})

        damage_states = "None;Slight;Moderate;Extensive;Complete;Collapse"
        node("StructuralDamage", "pass", str_dmg, states=damage_states, bnds=damage_bounds, parents=spectral)
        node("NonStructuralDamage", "pass", nsa_dmg, states=damage_states, bnds=damage_bounds, parents=spectral)
        node("MedicalTreatmentFunctionality", "twoOf", service("Resuscitation"), service("Operation"),
             states="None;ResuscitationOnly;OperationOnly;Both", bnds=FOUR_LEVEL_BOUNDS, parents=spectral)
        node("MedicalExaminationFunctionality", "twoOf", service("Radiology"), service("Laboratory"),
             states="None;RadiologyOnly;LaboratoryOnly;Both", bnds=FOUR_LEVEL_BOUNDS, parents=spectral)
        node("MedicalGasFunctionality", "zeroFlag", f"{state('OxygenPipe')} + {state('OxygenGenerator')}",
             parents=spectral)
        node("EmergencyPowerFunctionality", "zeroFlag", state("EmergencyPowerGenerator"), parents=pga)
        node("EmergencyWaterFunctionality", "zeroFlag", state("EmergencyWaterTank"), parents=pga)
        node("BasicFunctionality", "bothBelow", str_dmg, nsa_dmg,
             parents=f"{name}StructuralDamage;{name}NonStructuralDamage")
        node("WaterAccessServiceability", "either", val("EmergencyWaterFunctionality"), f"{wzone}Serv_R_",
             states="NotServiceable;Serviceable", bnds=serv_bounds,
             parents=f"{name}EmergencyWaterFunctionality;{wzone}Serviceability")
        node("PowerAccessServiceability", "either", val("EmergencyPowerFunctionality"), f"{pzone}Serv_R_",
             states="NotServiceable;Serviceable", bnds=serv_bounds,
             parents=f"{name}EmergencyPowerFunctionality;{pzone}Serviceability")
        node("EssentialUtilityService", "allThree", val("MedicalGasFunctionality"),
             val("PowerAccessServiceability"), val("WaterAccessServiceability"),
             states="NotServiceable;Serviceable", bnds=serv_bounds,
             parents=f"{name}MedicalGasFunctionality;{name}PowerAccessServiceability;{name}WaterAccessServiceability")
        node("BasicServiceability", "both", val("BasicFunctionality"), val("EssentialUtilityService"),
             states="NotServiceable;Serviceable", bnds=serv_bounds,
             parents=f"{name}BasicFunctionality;{name}EssentialUtilityService")
        node("TreatmentServiceability", "treatment", val("BasicServiceability"),
             val("MedicalTreatmentFunctionality"), val("MedicalExaminationFunctionality"),
             states="Closed;Basic;Partial;Full", bnds=FOUR_LEVEL_BOUNDS,
             parents=f"{name}MedicalTreatmentFunctionality;{name}MedicalExaminationFunctionality;{name}BasicServiceability")

    write_csv(os.path.join(BN, "hospital_nodes.csv"),
              ["hospital", "node", "shape", "a", "b", "c", "states", "bounds", "parents"], rows)


if __name__ == "__main__":
    main()
