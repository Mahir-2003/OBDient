# NHTSA fixtures: 2018 Volkswagen Golf GTI

Raw API responses, unmodified except that the VIN in `vpic_decode.json` was replaced with the
placeholder `WVWZZZAUZJW000000`. Fetched 2026-11-13 (the vPIC decode earlier, by hand).

| File | Request | Result |
| --- | --- | --- |
| `vpic_decode.json` | `vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/<VIN>?format=json` | Model `Golf GTI` |
| `vpic_models.json` | `vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/VOLKSWAGEN/modelyear/2018?format=json` | 14 models |
| `recalls_golf_gti.json` | `api.nhtsa.gov/recalls/recallsByVehicle?make=VOLKSWAGEN&model=Golf GTI&modelYear=2018` | 0 recalls |
| `recalls_gti.json` | same, `model=GTI` | 3 recalls |
| `complaints_golf_gti.json` | `api.nhtsa.gov/complaints/complaintsByVehicle?make=VOLKSWAGEN&model=Golf GTI&modelYear=2018` | 33 complaints |
| `complaints_gti.json` | same, `model=GTI` | 17 complaints |

The two complaint sets share no ODI number (50 distinct). Complaint records carry the first 11
characters of the complainant's VIN; that is how NHTSA publishes them.

`FixtureCache` maps a cache key to a file here: `<endpoint>_<model slug>.json` for recalls and
complaints, `vpic_decode.json` and `vpic_models.json` for the two vPIC endpoints.

`recalls_golf_gti.json` came back with **HTTP 400** and the zero-result body saved here. That is
how NHTSA answers a query that matches nothing; `NhtsaClient` treats it as an empty answer.
