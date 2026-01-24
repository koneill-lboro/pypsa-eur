
# PyPSA-GB approach

PyPSA-GB uses a **"copper-plate" hydrogen model** with a single GB-wide hydrogen bus. 

| Category                          | Implementation Status   | Granularity Level               |
| --------------------------------- | ----------------------- | ------------------------------- |
| **Electrolysis**                  | ✅ Implemented           | Simplified copper-plate GB-wide |
| **SMR (Steam Methane Reforming)** | ❌ Not implemented       | N/A                             |
| **Fuel Cells**                    | ✅ Partially implemented | Generator type only             |
| **Hydrogen Turbines**             | ✅ Implemented           | GB-wide H2 turbine              |
| **Hydrogen Storage**              | ✅ Implemented           | Single GB-wide store            |
| **Hydrogen Pipelines**            | ❌ Not implemented       | N/A                             |
### Summary of the Modelled Hydrogen System

| Aspect                    | Details                                                                                                                                 |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Primary Script**        | [add_hydrogen_system.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/scripts/hydrogen/add_hydrogen_system.py) |
| **Data Source**           | FES 2024 Building Blocks (Dem_BB009, Gen_BB007/008)                                                                                     |
| **Network Model**         | Copper-plate (single GB-wide H2 bus)                                                                                                    |
| **Round-trip Efficiency** | 34.3% (70% × 50% × 98%)                                                                                                                 |
| **Storage Duration**      | 168 hours (1 week)                                                                                                                      |
| **Current Limitations**   | No regional H2 network, no blue hydrogen (SMR), no sector coupling                                                                      |
**Key interconnections:**
- 22 electrolysis links connect electricity buses → H2 bus (EE50 scenario)
- 155 H2 turbine links connect H2 bus → electricity buses
- 1 H2 storage provides temporal arbitrage with cyclic constraint
- All H2 carriers defined with `co2_emissions=0` (green hydrogen assumption)
### Hydrogen Modelling Diagram

```mermaid
flowchart TB
    subgraph DataInputs["📥 DATA INPUTS"]
        FES_API["FES API<br/>(National Grid)"]
        FES_CSV["FES_2024_data.csv<br/>resources/FES/"]
        GenData["generator_data_by_fuel.csv<br/>data/generators/"]
        EmissionsData["emissions_intensity_by_types.csv<br/>data/generators/"]
        Config["config/defaults.yaml<br/>config/scenarios.yaml"]
        
        FES_API -->|"FES_data.py"| FES_CSV
    end

    subgraph FES_BB["FES Building Blocks"]
        BB009["Dem_BB009<br/>H2 Electrolysis Capacity (MW)"]
        BB007["Gen_BB007<br/>Fuel Cells (MW)"]
        BB008["Gen_BB008<br/>H2 Turbines/CCGT"]
        FES_CSV --> BB009 & BB007 & BB008
    end

    subgraph CodeModules["📂 CODE STRUCTURE"]
        direction TB
        H2Script["add_hydrogen_system.py<br/>scripts/hydrogen/"]
        ThermalInt["integrate_thermal_generators.py<br/>scripts/generators/"]
        CarrierDef["carrier_definitions.py<br/>scripts/utilities/"]
        H2Rules["hydrogen.smk<br/>rules/"]
        
        H2Rules -->|"orchestrates"| H2Script
        CarrierDef -->|"defines carriers"| H2Script
        ThermalInt -->|"maps H2 generators"| H2Script
    end

    subgraph Assumptions["⚙️ KEY ASSUMPTIONS"]
        direction LR
        Eff_Elec["Electrolysis η = 70%"]
        Eff_Turb["H2 Turbine η = 50%"]
        Eff_Stor["Storage η = 98%"]
        StorHrs["Storage = 168h<br/>(1 week)"]
        MC_Elec["MC_elec = £0/MWh"]
        MC_Turb["MC_turb = £5/MWh"]
        CO2["CO₂ = 0<br/>(green H2)"]
        CopperPlate["Single GB-wide<br/>H2 bus<br/>(copper-plate)"]
    end

    subgraph NetworkTopology["🔌 PYPSA NETWORK TOPOLOGY"]
        direction TB
        
        subgraph ElecLayer["ELECTRICITY LAYER"]
            ElecBus1["Electricity Bus 1"]
            ElecBus2["Electricity Bus 2"]
            ElecBusN["Electricity Bus N"]
            Wind["Wind"]
            Solar["Solar"]
            Nuclear["Nuclear"]
            CCGT["CCGT"]
            Demand["Demand"]
            
            Wind & Solar & Nuclear & CCGT --> ElecBus1
            ElecBus1 --> Demand
        end
        
        subgraph H2Layer["HYDROGEN LAYER"]
            H2Bus["external__GB_H2<br/>carrier='H2_gas'"]
            H2Storage["GB_H2_storage<br/>Store Component<br/>e_nom: 168h × capacity<br/>e_cyclic=True"]
            
            H2Bus <--> H2Storage
        end
        
        Elec1["electrolysis_1<br/>Link"]
        Elec2["electrolysis_2<br/>Link"]
        ElecN["electrolysis_N<br/>Link"]
        
        Turb1["H2_turbine_1<br/>Link"]
        Turb2["H2_turbine_2<br/>Link"]
        TurbN["H2_turbine_N<br/>Link"]
        
        ElecBus1 -->|"η=0.70"| Elec1 --> H2Bus
        ElecBus2 -->|"η=0.70"| Elec2 --> H2Bus
        ElecBusN -->|"η=0.70"| ElecN --> H2Bus
        
        H2Bus --> Turb1 -->|"η=0.50"| ElecBus1
        H2Bus --> Turb2 -->|"η=0.50"| ElecBus2
        H2Bus --> TurbN -->|"η=0.50"| ElecBusN
    end

    subgraph Optimization["📊 OPTIMIZATION MODEL"]
        direction TB
        ObjFunc["Objective Function:<br/>min Σ(costs)"]
        
        subgraph Constraints["Constraints"]
            EnergyBal["H2 Bus Balance:<br/>Σ(elec_out) - Σ(turb_in)<br/>± storage = 0"]
            StorDyn["Storage Dynamics:<br/>E_t = E_{t-1}(1-loss)<br/>+ charge - discharge"]
            CapLim["Capacity Limits:<br/>p ≤ p_nom (fixed)"]
            Cyclic["Cyclic Constraint:<br/>E_end = E_start"]
        end
        
        ObjFunc --> EnergyBal & StorDyn & CapLim & Cyclic
    end

    subgraph Outputs["📤 MODEL OUTPUTS"]
        LinkFlows["n.links_t.p0<br/>- electrolysis_* flows<br/>- H2_turbine_* flows"]
        StoreState["n.stores_t.e<br/>- GB_H2_storage level"]
        Statistics["n.statistics<br/>- Capacity factors<br/>- Energy throughput"]
        
        Tutorial["12-hydrogen.ipynb<br/>docs/source/tutorials/"]
    end

    %% Data Flow Connections
    GenData -->|"H2 technical params"| ThermalInt
    EmissionsData -->|"CO2=0 for H2"| CarrierDef
    Config -->|"scenario params"| H2Rules
    BB009 -->|"electrolysis_capacity_mw"| H2Script
    BB007 & BB008 -->|"h2_generation_capacity_mw"| H2Script
    
    H2Script -->|"adds components"| NetworkTopology
    Assumptions -->|"parameters"| H2Script
    
    NetworkTopology -->|"solve_network.py"| Optimization
    Optimization --> Outputs
    Outputs --> Tutorial

    %% Styling
    classDef dataNode fill:#e1f5fe,stroke:#01579b
    classDef codeNode fill:#f3e5f5,stroke:#4a148c
    classDef assumptionNode fill:#fff3e0,stroke:#e65100
    classDef networkNode fill:#e8f5e9,stroke:#1b5e20
    classDef optimNode fill:#fce4ec,stroke:#880e4f
    classDef outputNode fill:#f1f8e9,stroke:#33691e
    
    class FES_API,FES_CSV,GenData,EmissionsData,Config,BB009,BB007,BB008 dataNode
    class H2Script,ThermalInt,CarrierDef,H2Rules codeNode
    class Eff_Elec,Eff_Turb,Eff_Stor,StorHrs,MC_Elec,MC_Turb,CO2,CopperPlate assumptionNode
    class ElecBus1,ElecBus2,ElecBusN,H2Bus,H2Storage,Elec1,Elec2,ElecN,Turb1,Turb2,TurbN networkNode
    class ObjFunc,EnergyBal,StorDyn,CapLim,Cyclic optimNode
    class LinkFlows,StoreState,Statistics,Tutorial outputNode
```

## Limitations for My Research


## FES Hydrogen Data Provided

| Data Provided                                                                                   | Workbook Location  |
| ----------------------------------------------------------------------------------------------- | ------------------ |
| Hydrogen production volumes by pathway                                                          | F.67, WS1          |
| Hydrogen supply by pathway and technology                                                       | F.69, WS1          |
| Hydrogen demand by pathway and sector (Industrial Sector, Heating, Transport, Power Generation) | ED1, ED7, ED5, WS1 |
| Hydrogen Production: Natural Gas Demand                                                         | ED3                |
| Hydrogen Storage Capacities by pathway                                                          | WS1                |
| Low-Carbon Hydrogen Production Projects By Development Stage - MW of initial targeted capacity  | F.68               |

# PyPSA-Eur Approach

**Hydrogen Technologies:**
- PyPSA-EUR implements a **complete hydrogen supply chain** including production (electrolysis, SMR), storage (underground caverns, steel tanks), transport (pipelines, retrofit options), and end-use (fuel cells, turbines, industry, transport)
- Hydrogen is modeled as a **regional commodity** with explicit spatial resolution at each network node
- The system supports **myopic and perfect foresight** optimization modes
- Waste heat recovery from electrolysis and fuel cells is explicitly modeled

| Category                          | Implementation Status | Granularity Level |
| --------------------------------- | --------------------- | ----------------- |
| **Electrolysis**                  |                       |                   |
| **SMR (Steam Methane Reforming)** |                       |                   |
| **Fuel Cells**                    |                       |                   |
| **Hydrogen Turbines**             |                       |                   |
| **Hydrogen Storage**              |                       |                   |
| **Hydrogen Pipelines**            |                       |                   |
### Summary of the Modelled Hydrogen System

| Aspect                    | Details |
| ------------------------- | ------- |
| **Primary Script(s)**     |         |
| **Data Source(s)**        |         |
| **Key Assumptions**       |         |
| **Network Model**         |         |
| **Round-trip Efficiency** |         |
| **Storage Duration**      |         |
| **Current Limitations**   |         |
**Key interconnections:**

### Hydrogen Modelling Diagram


# describe issue briefly 




![](Pasted%20image%2020260123230638.png)

# simplifying assumptions choices 

1. Assume all hydrogen converted to power 
2. Assume all hydrogen used to meet zonal hydrogen demand 
3. Other 

## outline impact of simplifying assumptions & potential mitigations
