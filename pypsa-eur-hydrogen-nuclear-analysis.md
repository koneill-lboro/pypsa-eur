# PyPSA-EUR Hydrogen & Nuclear Technology Analysis

**Comprehensive Investigation Report for PyPSA-FES Integration**

Version: Based on PyPSA-EUR v2025.07.0 (main branch)
Analysis Date: December 2024

---

## Executive Summary

This report provides a comprehensive analysis of how PyPSA-EUR models hydrogen and nuclear technologies, with the objective of informing potential code incorporation into PyPSA-FES. The investigation covers technology representation, code architecture, data sources, and provides specific recommendations for FES integration.

### Key Findings

**Hydrogen Technologies:**
- PyPSA-EUR implements a **complete hydrogen supply chain** including production (electrolysis, SMR), storage (underground caverns, steel tanks), transport (pipelines, retrofit options), and end-use (fuel cells, turbines, industry, transport)
- Hydrogen is modeled as a **regional commodity** with explicit spatial resolution at each network node
- The system supports **myopic and perfect foresight** optimization modes
- Waste heat recovery from electrolysis and fuel cells is explicitly modeled

**Nuclear Technologies:**
- Nuclear is modeled as a **conventional generator** with country-specific capacity factors
- No SMR or microreactor implementations exist in the current codebase
- Nuclear capacity is treated as **brownfield** (existing) with limited expansion options
- Uranium is modeled as a single EU-wide commodity without regional constraints

**Integration Potential:**
- The hydrogen infrastructure code is **highly modular** and could be adapted to FES's 17-zone structure
- Nuclear implementation is simpler and could be directly incorporated
- Cost data comes from the PyPSA **technology-data** repository (versioned, well-documented)

---

## Table of Contents

1. [Technology-by-Technology Analysis](#technology-by-technology-analysis)
   - [Hydrogen Production](#hydrogen-production)
   - [Hydrogen Storage](#hydrogen-storage)
   - [Hydrogen Transport](#hydrogen-transport)
   - [Hydrogen End-Use](#hydrogen-end-use)
   - [Nuclear Technologies](#nuclear-technologies)
2. [Code Architecture Map](#code-architecture-map)
3. [Parameter Reference](#parameter-reference)
4. [Hydrogen Supply Chain Logic](#hydrogen-supply-chain-logic)
5. [Incorporation Recommendations](#incorporation-recommendations)
6. [Gap Analysis](#gap-analysis)

---

## Technology-by-Technology Analysis

### Hydrogen Production

#### 1. Electrolysis

**Implementation Location:** `scripts/prepare_sector_network.py:1817-1828`

**Technology Representation:**
- Single generic electrolysis technology (no differentiation between alkaline/PEM/SOEC)
- Modeled as a PyPSA `Link` component connecting electricity bus to H2 bus

**Code Pattern:**
```python
n.add(
    "Link",
    nodes + " H2 Electrolysis",
    bus1=nodes + " H2",
    bus0=nodes,  # electricity bus
    p_nom_extendable=True,
    carrier="H2 Electrolysis",
    efficiency=costs.at["electrolysis", "efficiency"],
    capital_cost=costs.at["electrolysis", "capital_cost"],
    p_min_pu=options["min_part_load_electrolysis"],
    lifetime=costs.at["electrolysis", "lifetime"],
)
```

**Key Parameters (from config):**
| Parameter | Config Location | Default |
|-----------|-----------------|---------|
| Min part load | `sector.min_part_load_electrolysis` | 0 |
| Waste heat utilization | `sector.use_electrolysis_waste_heat` | 0.25 |

**Dispatch Logic:**
- Continuous operation (no unit commitment)
- Part-load constraint via `p_min_pu` parameter
- Investment optimization determines optimal capacity

**Waste Heat Integration:**
```python
# From add_waste_heat() function - line 5490-5500
if options["use_electrolysis_waste_heat"]:
    n.links.loc[urban_central + " H2 Electrolysis", "bus2"] = (
        urban_central + " urban central heat"
    )
    n.links.loc[urban_central + " H2 Electrolysis", "efficiency2"] = (
        0.84 - n.links.loc[urban_central + " H2 Electrolysis", "efficiency"]
    ) * options["use_electrolysis_waste_heat"]
```

**Simplifying Assumptions:**
- No differentiation between electrolyzer types
- No degradation modeling
- No ramp rate constraints
- No water consumption tracking

---

#### 2. Steam Methane Reforming (SMR)

**Implementation Location:** `scripts/prepare_sector_network.py:2161-2191`

**Two Variants Implemented:**

**SMR without CCS:**
```python
n.add(
    "Link",
    nodes + " SMR",
    bus0=spatial.gas.nodes,
    bus1=nodes + " H2",
    bus2="co2 atmosphere",
    p_nom_extendable=True,
    carrier="SMR",
    efficiency=costs.at["SMR", "efficiency"],
    efficiency2=costs.at["gas", "CO2 intensity"],
    capital_cost=costs.at["SMR", "capital_cost"],
    lifetime=costs.at["SMR", "lifetime"],
)
```

**SMR with CCS:**
```python
n.add(
    "Link",
    spatial.nodes,
    suffix=" SMR CC",
    bus0=spatial.gas.nodes,
    bus1=nodes + " H2",
    bus2="co2 atmosphere",
    bus3=spatial.co2.nodes,
    p_nom_extendable=True,
    carrier="SMR CC",
    efficiency=costs.at["SMR CC", "efficiency"],
    efficiency2=costs.at["gas", "CO2 intensity"] * (1 - options["cc_fraction"]),
    efficiency3=costs.at["gas", "CO2 intensity"] * options["cc_fraction"],
    capital_cost=costs.at["SMR CC", "capital_cost"],
    lifetime=costs.at["SMR CC", "lifetime"],
)
```

**Key Configuration:**
| Parameter | Config Location | Default |
|-----------|-----------------|---------|
| SMR enabled | `sector.SMR` | true |
| SMR CC enabled | `sector.SMR_cc` | true |
| CC capture fraction | `sector.cc_fraction` | 0.9 |

**CO2 Tracking:**
- CO2 emissions tracked via `bus2` (atmosphere) and `bus3` (stored)
- Partial capture modeled through efficiency coefficients

---

### Hydrogen Storage

#### 1. Underground Cavern Storage (Salt Caverns)

**Implementation Location:** `scripts/prepare_sector_network.py:1866-1898`

**Potential Calculation:** `scripts/build_salt_cavern_potentials.py`

The salt cavern potential is calculated from a geospatial dataset:
```python
# From build_salt_cavern_potentials.py
caverns = gpd.read_file(snakemake.input.salt_caverns)  # GWh/sqkm
caverns_regions = salt_cavern_potential_by_region(caverns, regions)
```

**Storage Implementation:**
```python
h2_caverns = pd.read_csv(h2_cavern_file, index_col=0)

if options["hydrogen_underground_storage"]:
    h2_caverns = h2_caverns[cavern_types].sum(axis=1)
    h2_caverns = h2_caverns[h2_caverns > 2]  # Minimum 2 TWh potential
    h2_caverns = h2_caverns * 1e6  # Convert TWh to MWh
    h2_caverns.clip(upper=1e9, inplace=True)  # Max 1000 TWh per location

    n.add(
        "Store",
        h2_caverns.index + " H2 Store",
        bus=h2_caverns.index + " H2",
        e_nom_extendable=True,
        e_nom_max=h2_caverns.values,
        e_cyclic=True,
        carrier="H2 Store",
        capital_cost=h2_capital_cost,
        lifetime=costs.at["hydrogen storage underground", "lifetime"],
    )
```

**Configuration Options:**
| Parameter | Config Location | Default |
|-----------|-----------------|---------|
| Underground storage enabled | `sector.hydrogen_underground_storage` | true |
| Storage locations | `sector.hydrogen_underground_storage_locations` | [onshore, nearshore] |

**Data Source:**
- Caglayan et al. (2020): "Technical Potential of Salt Caverns for Hydrogen Storage in Europe"
- DOI: 10.1016/j.ijhydene.2019.12.161
- Archived at Zenodo: `h2_salt_caverns` dataset

---

#### 2. Above-Ground Tank Storage

**Implementation Location:** `scripts/prepare_sector_network.py:1900-1913`

```python
tech = "hydrogen storage tank type 1 including compressor"
nodes_overground = h2_caverns.index.symmetric_difference(nodes)

n.add(
    "Store",
    nodes_overground + " H2 Store",
    bus=nodes_overground + " H2",
    e_nom_extendable=True,
    e_cyclic=True,
    carrier="H2 Store",
    capital_cost=costs.at[tech, "capital_cost"],
    lifetime=costs.at[tech, "lifetime"],
)
```

**Logic:** Nodes without underground cavern potential get above-ground storage option with higher capital costs.

---

### Hydrogen Transport

#### 1. New Hydrogen Pipelines

**Implementation Location:** `scripts/prepare_sector_network.py:2055-2077`

```python
if options["H2_network"]:
    h2_pipes = create_network_topology(
        n, "H2 pipeline ", carriers=["DC", "gas pipeline"]
    )

    n.add(
        "Link",
        h2_pipes.index,
        bus0=h2_pipes.bus0.values + " H2",
        bus1=h2_pipes.bus1.values + " H2",
        p_min_pu=-1,  # Bidirectional
        p_nom_extendable=True,
        length=h2_pipes.length.values,
        capital_cost=costs.at["H2 (g) pipeline", "capital_cost"] * h2_pipes.length.values,
        carrier="H2 pipeline",
        lifetime=costs.at["H2 (g) pipeline", "lifetime"],
    )
```

**Topology Generation:**
The `create_network_topology()` function (line 368-420) creates pipeline routes based on existing DC links and gas pipeline corridors.

---

#### 2. Retrofitted Gas Pipelines

**Implementation Location:** `scripts/prepare_sector_network.py:2032-2053`

```python
if options["H2_retrofit"]:
    h2_pipes = gas_pipes.rename(index=lambda x: x.replace(fr, to))

    n.add(
        "Link",
        h2_pipes.index,
        bus0=h2_pipes.bus0 + " H2",
        bus1=h2_pipes.bus1 + " H2",
        p_min_pu=-1.0,
        p_nom_max=h2_pipes.p_nom * options["H2_retrofit_capacity_per_CH4"],
        p_nom_extendable=True,
        length=h2_pipes.length,
        capital_cost=costs.at["H2 (g) pipeline repurposed", "capital_cost"] * h2_pipes.length,
        carrier="H2 pipeline retrofitted",
        lifetime=costs.at["H2 (g) pipeline repurposed", "lifetime"],
    )
```

**Key Configuration:**
| Parameter | Config Location | Default |
|-----------|-----------------|---------|
| H2 network enabled | `sector.H2_network` | true |
| H2 retrofit enabled | `sector.H2_retrofit` | false |
| Retrofit capacity ratio | `sector.H2_retrofit_capacity_per_CH4` | 0.6 |

---

#### 3. Pipeline Losses

**Implementation Location:** `scripts/prepare_sector_network.py:5836-5883`

```python
def lossy_bidirectional_links(n, carrier, efficiencies={}):
    efficiency_static = efficiencies.get("efficiency_static", 1)
    efficiency_per_1000km = efficiencies.get("efficiency_per_1000km", 1)
    compression_per_1000km = efficiencies.get("compression_per_1000km", 0)

    n.links.loc[carrier_i, "efficiency"] = (
        efficiency_static
        * efficiency_per_1000km ** (n.links.loc[carrier_i, "length"] / 1e3)
    )

    # Compression electricity consumption
    if compression_per_1000km > 0:
        n.links.loc[carrier_i, "bus2"] = n.links.loc[carrier_i, "bus0"].map(
            n.buses.location
        )
        n.links.loc[carrier_i, "efficiency2"] = (
            -compression_per_1000km * n.links.loc[carrier_i, "length_original"] / 1e3
        )
```

**Default Efficiency Parameters:**
| Parameter | Value |
|-----------|-------|
| H2 pipeline efficiency/1000km | 1.0 (lossless) |
| H2 compression/1000km | 0.018 |

---

### Hydrogen End-Use

#### 1. Fuel Cells (Stationary Re-electrification)

**Implementation Location:** `scripts/prepare_sector_network.py:1830-1844`

```python
if options["hydrogen_fuel_cell"]:
    n.add(
        "Link",
        nodes + " H2 Fuel Cell",
        bus0=nodes + " H2",
        bus1=nodes,  # electricity bus
        p_nom_extendable=True,
        carrier="H2 Fuel Cell",
        efficiency=costs.at["fuel cell", "efficiency"],
        capital_cost=costs.at["fuel cell", "capital_cost"]
            * costs.at["fuel cell", "efficiency"],
        lifetime=costs.at["fuel cell", "lifetime"],
    )
```

**Waste Heat Recovery:**
```python
if options["use_fuel_cell_waste_heat"]:
    n.links.loc[urban_central + " H2 Fuel Cell", "efficiency2"] = (
        0.95 - n.links.loc[urban_central + " H2 Fuel Cell", "efficiency"]
    ) * options["use_fuel_cell_waste_heat"]
```

---

#### 2. Hydrogen Turbines

**Implementation Location:** `scripts/prepare_sector_network.py:1846-1864`

```python
if options["hydrogen_turbine"]:
    n.add(
        "Link",
        nodes + " H2 turbine",
        bus0=nodes + " H2",
        bus1=nodes,
        p_nom_extendable=True,
        carrier="H2 turbine",
        efficiency=costs.at["OCGT", "efficiency"],  # Uses OCGT costs
        capital_cost=costs.at["OCGT", "capital_cost"]
            * costs.at["OCGT", "efficiency"],
        marginal_cost=costs.at["OCGT", "VOM"],
        lifetime=costs.at["OCGT", "lifetime"],
    )
```

**Note:** H2 turbines currently use OCGT cost assumptions. The code includes a TODO comment suggesting hydrogen-specific data.

---

#### 3. Fuel Cell Vehicles

**Implementation Location:** `scripts/prepare_sector_network.py:2392-2469`

```python
def add_fuel_cell_cars(n, p_set, fuel_cell_share, temperature, options, spatial):
    car_efficiency = options["transport_fuel_cell_efficiency"]

    # Temperature-dependent efficiency
    efficiency = get_temp_efficency(
        car_efficiency,
        temperature,
        options["transport_heating_deadband_lower"],
        options["transport_heating_deadband_upper"],
        options["ICE_lower_degree_factor"],
        options["ICE_upper_degree_factor"],
    )

    # Calculate hydrogen demand profile
    profile = fuel_cell_share * p_set.div(efficiency)

    n.add(
        "Load",
        spatial.nodes,
        suffix=" land transport fuel cell",
        bus=spatial.h2.nodes,
        carrier="land transport fuel cell",
        p_set=profile.loc[n.snapshots],
    )
```

**Configuration:**
| Parameter | Config Location | Default 2050 |
|-----------|-----------------|--------------|
| Fuel cell share | `sector.land_transport_fuel_cell_share` | 0 |
| Efficiency | `sector.transport_fuel_cell_efficiency` | 30.003 (100km/MWh_H2) |

---

#### 4. Industrial Hydrogen Demand

**Implementation Location:** `scripts/prepare_sector_network.py:4692-4699`

```python
n.add(
    "Load",
    nodes,
    suffix=" H2 for industry",
    bus=nodes + " H2",
    carrier="H2 for industry",
    p_set=industrial_demand.loc[nodes, "hydrogen"] / nhours,
)
```

Industrial hydrogen demand is calculated in `scripts/build_industry_sector_ratios.py` based on JRC-IDEES data for:
- DRI steelmaking (H2-based direct reduced iron)
- Chemical feedstock
- High-temperature process heat

**DRI Implementation (from industry sector ratios):**
```python
# DRI + Electric arc pathway
# H2 consumption for DRI: 1.7 MWh_H2/t_steel (config: industry.H2_DRI)
# Electricity for DRI: 0.322 MWh_el/t_steel (config: industry.elec_DRI)
```

---

### Nuclear Technologies

#### Implementation Overview

Nuclear is implemented as a **conventional generator** through the `attach_conventional_generators()` function in `scripts/add_electricity.py:512-617`.

**Key Files:**
- `scripts/add_electricity.py` - Generator attachment
- `scripts/build_powerplants.py` - Power plant database processing
- `scripts/add_existing_baseyear.py` - Existing capacity handling
- `data/nuclear_p_max_pu.csv` - Country-specific capacity factors

**Generator Addition:**
```python
# From attach_conventional_generators()
n.add(
    "Generator",
    ppl.index,
    carrier=ppl.carrier,  # "nuclear"
    bus=ppl.bus,
    p_nom_min=ppl.p_nom.where(ppl.carrier.isin(conventional_carriers), 0),
    p_nom=ppl.p_nom.where(ppl.carrier.isin(conventional_carriers), 0),
    p_nom_extendable=ppl.carrier.isin(extendable_carriers["Generator"]),
    efficiency=ppl.efficiency,
    marginal_cost=marginal_cost,
    capital_cost=ppl.capital_cost,
    build_year=ppl.build_year,
    lifetime=ppl.lifetime,
)
```

**Country-Specific Capacity Factors:**
```python
# From add_electricity.py:598-617
for carrier in set(conventional_params) & set(carriers):
    idx = n.generators.query("carrier == @carrier").index
    for attr in list(set(conventional_params[carrier]) & set(n.generators)):
        values = conventional_params[carrier][attr]
        if f"conventional_{carrier}_{attr}" in conventional_inputs:
            values = pd.read_csv(
                conventional_inputs[f"conventional_{carrier}_{attr}"], index_col=0
            ).iloc[:, 0]
            bus_values = n.buses.country.map(values)
            n.generators.update(
                {attr: n.generators.loc[idx].bus.map(bus_values).dropna()}
            )
```

**Nuclear Capacity Factors (`data/nuclear_p_max_pu.csv`):**
| Country | p_max_pu |
|---------|----------|
| BE | 0.883 |
| FR | 0.616 |
| DE | 0.926 |
| GB | 0.684 |
| ... | ... |

**Configuration:**
```yaml
# config.default.yaml
conventional:
  unit_commitment: false
  dynamic_fuel_price: false
  nuclear:
    p_max_pu: data/nuclear_p_max_pu.csv

electricity:
  conventional_carriers: [nuclear, oil, OCGT, CCGT, coal, lignite, geothermal, biomass]
  extendable_carriers:
    Generator: [solar, solar-hsat, onwind, offwind-ac, offwind-dc, offwind-float, OCGT, CCGT]
```

**Key Observations:**
1. Nuclear is **NOT** in extendable carriers by default
2. No SMR or microreactor implementations
3. Uses uranium as single EU-wide fuel source
4. No ramping constraints modeled

**Uranium Spatial Handling:**
```python
# From prepare_sector_network.py:196-199
spatial.uranium = SimpleNamespace()
spatial.uranium.nodes = ["EU uranium"]
spatial.uranium.locations = ["EU"]
```

---

## Code Architecture Map

### Key Files and Functions

```
pypsa-eur/
├── config/
│   └── config.default.yaml          # All technology parameters
├── scripts/
│   ├── prepare_sector_network.py    # Main sector coupling (232KB)
│   │   ├── define_spatial()         # Spatial bus definitions
│   │   ├── add_storage_and_grids()  # H2/battery/gas infrastructure
│   │   ├── add_ammonia()            # NH3 synthesis/cracking
│   │   ├── add_industry()           # Industrial H2 demand
│   │   ├── add_fuel_cell_cars()     # Transport FCEV
│   │   ├── add_waste_heat()         # Waste heat recovery
│   │   └── lossy_bidirectional_links() # Pipeline losses
│   ├── add_electricity.py           # Conventional generators (nuclear)
│   │   └── attach_conventional_generators()
│   ├── add_existing_baseyear.py     # Brownfield capacities
│   ├── build_salt_cavern_potentials.py  # H2 storage potentials
│   ├── build_industry_sector_ratios.py  # Industrial H2 demand calc
│   └── process_cost_data.py         # Cost data processing
├── rules/
│   ├── build_sector.smk             # Sector coupling workflow
│   └── retrieve.smk                 # Data retrieval rules
└── data/
    ├── nuclear_p_max_pu.csv         # Nuclear capacity factors
    └── custom_costs.csv             # Cost overrides
```

### Data Flow Diagram

```
                    ┌─────────────────────┐
                    │ technology-data     │
                    │ (GitHub/Zenodo)     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ process_cost_data.py│
                    │ - Unit conversion   │
                    │ - Annuity calc      │
                    │ - Custom overrides  │
                    └──────────┬──────────┘
                               │
                               ▼
    ┌──────────────────────────────────────────────────────┐
    │              prepare_sector_network.py               │
    │                                                      │
    │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
    │  │ H2 Production│  │ H2 Storage   │  │ H2 Demand  │ │
    │  │ - Electrolysis│  │ - Caverns    │  │ - Industry │ │
    │  │ - SMR/SMR CC │  │ - Tanks      │  │ - Transport│ │
    │  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
    │         │                 │                │        │
    │         └─────────────────┼────────────────┘        │
    │                           │                          │
    │              ┌────────────┴────────────┐             │
    │              │     H2 Network          │             │
    │              │  - New pipelines        │             │
    │              │  - Retrofitted pipes    │             │
    │              └─────────────────────────┘             │
    └──────────────────────────────────────────────────────┘
```

---

## Parameter Reference

### Hydrogen Technology Costs

**Source:** PyPSA technology-data v0.13.3

| Technology | Capital Cost (€/kW) | Efficiency | Lifetime (years) |
|------------|---------------------|------------|------------------|
| Electrolysis | ~450-800 (year-dependent) | 0.67-0.80 | 25 |
| Fuel Cell | ~600-1200 | 0.50-0.58 | 15 |
| SMR | ~500-700 | 0.76 | 25 |
| SMR CC | ~900-1100 | 0.69 | 25 |
| H2 pipeline (new) | €/MW/km | - | 40 |
| H2 pipeline (repurposed) | €/MW/km | - | 40 |
| H2 storage underground | €/MWh | - | 100 |
| H2 storage tank | €/MWh | - | 20 |

### Nuclear Costs

| Parameter | Value | Notes |
|-----------|-------|-------|
| Capital Cost | ~3000-5000 €/kW | Year-dependent |
| Fuel Cost | ~3 €/MWh | Uranium |
| Efficiency | ~0.33 | Thermal to electric |
| Lifetime | 40-60 years | |

### Configuration Parameters Summary

```yaml
# Key sector config options for hydrogen
sector:
  hydrogen_fuel_cell: true
  hydrogen_turbine: true
  SMR: true
  SMR_cc: true
  hydrogen_underground_storage: true
  hydrogen_underground_storage_locations: [onshore, nearshore]
  H2_network: true
  H2_retrofit: false
  H2_retrofit_capacity_per_CH4: 0.6
  min_part_load_electrolysis: 0
  use_electrolysis_waste_heat: 0.25
  use_fuel_cell_waste_heat: 1
  land_transport_fuel_cell_share:
    2020: 0
    2050: 0  # Default is zero
  transport_fuel_cell_efficiency: 30.003

# Transmission efficiency settings
transmission_efficiency:
  H2 pipeline:
    efficiency_per_1000km: 1
    compression_per_1000km: 0.018
```

---

## Hydrogen Supply Chain Logic

### Demand Determination

**1. Industrial Demand:**
```
JRC-IDEES Data → build_industry_sector_ratios.py → sector ratios
                           ↓
industrial_production_per_node.py × sector_ratios = industrial_demand.csv
                           ↓
prepare_sector_network.py → add_industry() → H2 for industry Load
```

**Key Industrial H2 Uses:**
- DRI steelmaking: 1.7 MWh_H2/t_steel
- Chemical processes
- High-temp heat (future electrification scenarios)

**2. Transport Demand:**
```
build_transport_demand.py → transport_demand.csv
                                    ↓
land_transport_fuel_cell_share × transport_demand / efficiency = FCEV H2 Load
```

**3. Shipping (if enabled):**
```
shipping_hydrogen_share × shipping_demand → H2 for shipping Load
(Currently default is 0 - methanol preferred)
```

### Supply Optimization

The model optimizes:
1. **Production mix:** Electrolysis vs SMR vs SMR-CC
2. **Storage deployment:** Underground vs above-ground
3. **Network topology:** New pipelines vs retrofitted
4. **Re-electrification:** Fuel cells vs turbines

All subject to:
- CO2 budget constraints
- Technology availability
- Regional potentials (caverns)
- Cost minimization

---

## Incorporation Recommendations

### Recommended for Direct Adoption

| Feature | Rationale | Effort |
|---------|-----------|--------|
| Electrolysis modeling | Clean Link-based implementation | Low |
| SMR/SMR-CC | Well-structured with CO2 tracking | Low |
| Fuel cell re-electrification | Simple Link component | Low |
| H2 turbine | Uses standard OCGT pattern | Low |
| Nuclear generator pattern | Standard conventional gen | Low |

### Recommended for Adaptation

| Feature | Required Modifications | Effort |
|---------|----------------------|--------|
| H2 pipeline network | Adapt topology generation for 17 zones | Medium |
| Salt cavern storage | Use UK-specific cavern data | Medium |
| Industrial H2 demand | Integrate FES industry scenarios | Medium |
| Transport FCEV | Align with FES transport projections | Low |

### Features to Avoid/Skip

| Feature | Reason |
|---------|--------|
| EU-wide commodity buses | FES is GB-only |
| Complex gas network topology | Simpler for 17 zones |
| Perfect foresight mode | FES uses myopic |
| Some waste heat options | May overcomplicate initial implementation |

### Integration Code Snippets

**Minimal H2 Electrolysis for FES:**
```python
def add_hydrogen_electrolysis(n, costs, nodes):
    """Add electrolysis to FES network."""
    n.add("Carrier", "H2")
    n.add("Bus", nodes + " H2", location=nodes, carrier="H2", unit="MWh_LHV")

    n.add(
        "Link",
        nodes + " H2 Electrolysis",
        bus0=nodes,  # Electricity bus
        bus1=nodes + " H2",
        p_nom_extendable=True,
        carrier="H2 Electrolysis",
        efficiency=costs.at["electrolysis", "efficiency"],
        capital_cost=costs.at["electrolysis", "capital_cost"],
        lifetime=costs.at["electrolysis", "lifetime"],
    )
```

**Minimal H2 Storage for FES:**
```python
def add_hydrogen_storage(n, costs, nodes, h2_potentials):
    """Add H2 storage with regional potentials."""
    # Underground where available
    for node in h2_potentials.index:
        n.add(
            "Store",
            node + " H2 Store",
            bus=node + " H2",
            e_nom_extendable=True,
            e_nom_max=h2_potentials.loc[node] * 1e6,  # TWh to MWh
            e_cyclic=True,
            carrier="H2 Store",
            capital_cost=costs.at["hydrogen storage underground", "capital_cost"],
        )

    # Tank storage for other nodes
    tank_nodes = nodes.difference(h2_potentials.index)
    n.add(
        "Store",
        tank_nodes + " H2 Store",
        bus=tank_nodes + " H2",
        e_nom_extendable=True,
        e_cyclic=True,
        carrier="H2 Store",
        capital_cost=costs.at["hydrogen storage tank type 1 including compressor", "capital_cost"],
    )
```

---

## Gap Analysis

### Features PyPSA-EUR Has That FES Needs

| Feature | Priority | Notes |
|---------|----------|-------|
| Electrolysis | High | Core H2 production |
| H2 storage (caverns + tanks) | High | Flexibility provision |
| H2 pipelines | Medium | Inter-zonal transport |
| Fuel cells | Medium | Re-electrification |
| SMR/SMR-CC | Medium | Blue hydrogen option |
| Industrial H2 demand | High | Steel, chemicals |
| Transport FCEV | Low | Minor role in FES |

### Features Missing for NHES-WEN Research

| Missing Feature | Impact | Workaround |
|-----------------|--------|------------|
| Water constraints | High | Custom constraint needed |
| Electrolyzer types (PEM/SOEC/Alkaline) | Medium | Use efficiency ranges |
| SMR specific reactors | Low | Generic SMR sufficient |
| Microreactors | Low | Not common in FES scenarios |
| Ramping constraints | Medium | Add if unit commitment needed |
| Degradation modeling | Low | Use capacity factors |

### PyPSA-EUR Features Unnecessary for FES

| Feature | Reason |
|---------|--------|
| European gas network | GB-only focus |
| Cross-border H2 trade | Single-country model |
| EU biomass transport | GB biomass assumptions |
| Multi-country CO2 network | Simpler GB CO2 handling |

---

## Appendix: Data Sources

### Technology Costs
- **Repository:** PyPSA/technology-data (GitHub)
- **Version:** v0.13.3
- **URL:** https://github.com/PyPSA/technology-data
- **License:** CC-BY-4.0

### Salt Cavern Potentials
- **Source:** Caglayan et al. (2020)
- **DOI:** 10.1016/j.ijhydene.2019.12.161
- **Archive:** Zenodo (h2_salt_caverns dataset)

### Industrial Demand
- **Source:** JRC-IDEES 2021
- **URL:** https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/JRC-IDEES/
- **Archive:** Zenodo (jrc_idees dataset)

### Nuclear Capacity Factors
- **Source:** Historical EIA/IAEA data
- **File:** `data/nuclear_p_max_pu.csv`
- **GB Value:** 0.684

### Power Plants
- **Source:** powerplantmatching package v0.7.1
- **URL:** https://github.com/PyPSA/powerplantmatching

---

## Version Information

- **PyPSA-EUR Version:** v2025.07.0
- **PyPSA Version:** Compatible with latest
- **Technology-data Version:** v0.13.3
- **Analysis Date:** December 2024
- **License Considerations:** MIT license for code, CC-BY-4.0 for technology-data
